# -*- coding: utf-8 -*-
"""provider 级附加请求头（T13：headers / session_header）回归测试。

背景：OpenCode Go 网关（https://opencode.ai/zen/go/v1）对订阅 key 开放直连，
但官方客户端准入要求两条中转原本不发的东西：
① 自带 User-Agent——网关拒 python-httpx 默认 UA（流量滥用识别）；
② x-opencode-session 会话头——路由/提示缓存优化 + 滥用识别。
config 选配 providers.<name>.headers（静态附加头）与 session_header
（每请求生成 sess_<hex> 会话 ID 填入该头名）满足之。

覆盖：
- parse：headers 只收 str→str（坏条目忽略）；session_header 空串/非 str 不启用
- 缺省 provider 无两键时 prov 带空值（行为与升级前逐字节一致）
- 转发：Authorization/Content-Type/Accept 基础头不被覆盖；
  User-Agent 附带；每请求会话 ID 唯一；未启用会话头的 provider 不发该头
- 别名转发：go/xxx 请求体改写为上游真实 ID 再发（zynk 先例同款）

单例隔离：import routes.proxy 前把 settings 路径常量指到临时目录
（与 test_opencode_channel / test_provider_toggle 同约定）。
全零网络：monkeypatch routes.proxy 的 httpx.Client，记录 post kwargs。
"""

import json
import pathlib
import sys
import tempfile

import pytest

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

# ─── 路径重定向（必须在 import routes.proxy 之前）──────────────────────────
_TMP = pathlib.Path(tempfile.mkdtemp(prefix="qidi-relay-hdrs-"))
(_TMP / "config.json").write_text(json.dumps({"providers": {}}), encoding="utf-8")
(_TMP / "accounts.json").write_text(json.dumps({"accounts": []}), encoding="utf-8")

import proxy_core.settings as S          # noqa: E402

if "proxy_core.state" not in sys.modules:
    S.BASE_DIR = _TMP
    S.CONFIG_FILE = _TMP / "config.json"
    S.ACCOUNTS_FILE = _TMP / "accounts.json"
    S.STATS_FILE = _TMP / "token_stats.json"
    S.WATER_FILE = _TMP / "water_check.json"
    S.LOG_FILE = _TMP / "proxy.log"

import proxy_core.providers as PV        # noqa: E402

if "proxy_core.state" not in sys.modules:
    PV.CONFIG_FILE = S.CONFIG_FILE

import routes.proxy as RP                # noqa: E402
from flask import Flask                  # noqa: E402

GO_PROVIDER = {
    "base_url": "https://opencode.ai/zen/go/v1",
    "api_keys": [{"key": "sk-go-test", "label": "go-main"}],
    "headers": {"User-Agent": "qidi-relay/1.0",
                "Authorization": "Bearer 恶意覆盖尝试",
                "": "空头名忽略",
                "X-Bad": 123},
    "session_header": "x-opencode-session",
    "models": ["go/step-5-preview-free"],
    "model_aliases": {"go/step-5-preview-free": "step-5-preview-free"},
}


# ─── 假上游（全零网络）───────────────────────────────────────────────────

class _Resp:
    def __init__(self, payload):
        self.status_code = 200
        self._payload = payload
        self.headers = {"content-type": "application/json"}
        self.content = json.dumps(payload).encode("utf-8")
        self.text = self.content.decode("utf-8")

    def json(self):
        return self._payload


class _RecordingClient:
    """记录请求头的假 httpx.Client。

    非流式走 post(url, json=..., headers=...)；流式走
    build_request(...) + send(req, stream=True)——两条路径都记录，
    统一以 {"url", "headers", "json"} 形状落 calls。
    """

    calls = []

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def close(self):
        pass

    def _resp(self):
        return _Resp({
            "id": "chatcmpl-go", "object": "chat.completion",
            "model": "step-5-preview-free",
            "choices": [{"index": 0, "message": {"role": "assistant",
                                                 "content": "pong"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2,
                      "total_tokens": 7},
        })

    def post(self, url, **kwargs):
        _RecordingClient.calls.append({"url": url, **kwargs})
        return self._resp()

    def build_request(self, method, url, **kwargs):
        return {"method": method, "url": url, **kwargs}

    def send(self, req, stream=False):
        _RecordingClient.calls.append(req)
        return self._resp()


def _parsed_prov(cfg=None):
    """_load_providers 解析后的运行时 prov 字典（转发层消费的形状）。

    与 GO_PROVIDER（config 段原文）分开：parse 测试喂 config 原文走
    _load_providers；转发测试直接用解析产物，避免把 config 当运行时用。
    """
    base = {
        "name": "opencode-go",
        "enabled": True,
        "base_url": GO_PROVIDER["base_url"],
        "api_key": "sk-go-test",
        "keys": [{"key": "sk-go-test", "label": "go-main"}],
        "models": list(GO_PROVIDER["models"]),
        "model_aliases": dict(GO_PROVIDER["model_aliases"]),
        "headers": {"User-Agent": "qidi-relay/1.0",
                    "Authorization": "Bearer 恶意覆盖尝试"},
        "session_header": GO_PROVIDER["session_header"],
    }
    base.update(cfg or {})
    return base


def _prov(cfg=None):
    return _parsed_prov(cfg)


@pytest.fixture()
def client(monkeypatch):
    _RecordingClient.calls = []
    monkeypatch.setattr(RP.httpx, "Client", _RecordingClient)
    app = Flask(__name__)
    return app.test_request_context("/")


# ─── parse 层 ────────────────────────────────────────────────────────────

def test_parse_headers_filters_non_str_and_session_header(tmp_path, monkeypatch):
    """① headers 只收 str→str；Authorization 静态头保留在 config 层（转发层才防覆盖）；
    session_header 原样透出。"""
    cfg = {"providers": {"opencode-go": GO_PROVIDER}}
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(S, "CONFIG_FILE", cfg_file)
    monkeypatch.setattr(PV, "CONFIG_FILE", cfg_file)
    PV._cache = None
    try:
        prov = PV.match("go/step-5-preview-free")
        assert prov is not None
        # 空头名与非字符串值被剔除，合法头原样保留
        assert prov["headers"] == {"User-Agent": "qidi-relay/1.0",
                                   "Authorization": "Bearer 恶意覆盖尝试"}
        assert prov["session_header"] == "x-opencode-session"
        assert prov["model_aliases"] == {"go/step-5-preview-free": "step-5-preview-free"}
    finally:
        PV._cache = None


def test_parse_defaults_empty_when_not_configured(tmp_path, monkeypatch):
    """② 未配两键的存量 provider：headers={} / session_header=''
    （pick_key 与转发行为与升级前逐字节一致）。"""
    cfg = {"providers": {"agnes": {
        "base_url": "https://api.agnes-ai.cn/v1",
        "api_keys": ["sk-aaa"],
        "models": ["agnes-2.0-flash"],
    }}}
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(S, "CONFIG_FILE", cfg_file)
    monkeypatch.setattr(PV, "CONFIG_FILE", cfg_file)
    PV._cache = None
    try:
        prov = PV.match("agnes-2.0-flash")
        assert prov["headers"] == {}
        assert prov["session_header"] == ""
    finally:
        PV._cache = None


def test_parse_session_header_blank_means_off(tmp_path, monkeypatch):
    """③ session_header 空串/非字符串按不启用处理（绝不生成无头名会话 ID）。"""
    for bad in ("", "   ", 123, None):
        cfg = {"providers": {"p": {**GO_PROVIDER, "session_header": bad}}}
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        monkeypatch.setattr(S, "CONFIG_FILE", cfg_file)
        monkeypatch.setattr(PV, "CONFIG_FILE", cfg_file)
        PV._cache = None
        try:
            prov = PV.match("go/step-5-preview-free")
            assert prov["session_header"] == "", bad
        finally:
            PV._cache = None


# ─── 转发层 ──────────────────────────────────────────────────────────────

def test_forward_sends_static_and_session_headers(client):
    """④ 转发带 UA + 会话头；Authorization 用真 key（静态头恶意覆盖被挡）。
    每请求会话 ID 唯一（两次调用两条不同值）。"""
    prov = _prov()
    with client:
        for _ in range(2):
            r = RP._forward_external(
                {"model": "go/step-5-preview-free",
                 "messages": [{"role": "user", "content": "ping"}]},
                "go/step-5-preview-free", prov)
            assert r.status_code == 200, r.get_data(as_text=True)
    assert len(_RecordingClient.calls) == 2
    sids = set()
    for call in _RecordingClient.calls:
        h = call["headers"]
        # 基础头：真 key 生效，静态头里的同名 Authorization 覆盖被忽略
        assert h["Authorization"] == "Bearer sk-go-test"
        assert h["Content-Type"] == "application/json"
        assert h["Accept"] == "application/json"
        # 静态附加头
        assert h["User-Agent"] == "qidi-relay/1.0"
        # 会话头：sess_<hex> 形态
        sid = h["x-opencode-session"]
        assert sid.startswith("sess_") and len(sid) == 21, sid
        sids.add(sid)
        # 别名改写：上游收到真实模型 ID，URL 拼 base + /chat/completions
        assert call["url"] == "https://opencode.ai/zen/go/v1/chat/completions"
        assert call["json"]["model"] == "step-5-preview-free"
    assert len(sids) == 2, "每请求会话 ID 必须唯一"


def test_forward_without_session_header_sends_none(client):
    """⑤ 未启用会话頭的 provider：请求头集合与升级前逐字节一致
    （不许多出任何头）。"""
    prov = _prov({"session_header": "", "headers": {}})
    with client:
        r = RP._forward_external(
            {"model": "go/step-5-preview-free",
             "messages": [{"role": "user", "content": "ping"}]},
            "go/step-5-preview-free", prov)
        assert r.status_code == 200
    h = _RecordingClient.calls[-1]["headers"]
    assert set(h) == {"Authorization", "Content-Type", "Accept"}
    assert "User-Agent" not in h and "x-opencode-session" not in h


def test_stream_forward_carries_headers(client, monkeypatch):
    """⑥ 流式路径同样带附加头与会话头（_headers_for 是流式/非流式共用，
    流式只换 Accept 值；此处只验头集合，不断言吐流内容）。
    关掉流内嗅探门（monkeypatch _feature_stream_gate）——门判定要真实
    上游字节流，与"头发得对不对"无关，留给 test_channel_gate 专项。"""
    monkeypatch.setattr(RP, "_feature_stream_gate", lambda: False)
    prov = _prov()
    with client:
        resp = RP._forward_external(
            {"model": "go/step-5-preview-free", "stream": True,
             "messages": [{"role": "user", "content": "ping"}]},
            "go/step-5-preview-free", prov)
        assert resp.status_code == 200
        resp.close()
    h = _RecordingClient.calls[-1]["headers"]
    assert h["Accept"] == "text/event-stream"
    assert h["User-Agent"] == "qidi-relay/1.0"
    assert h["x-opencode-session"].startswith("sess_")
    assert h["Authorization"] == "Bearer sk-go-test"
