# -*- coding: utf-8 -*-
"""OpenCode serve 桥通道（T12）本地回归测试。

全零网络：mock httpx.Client 模拟桥的 session/message 响应——响应形状照
2026-10-10 实测的 /doc schema（AssistantMessage{finish,modelID,tokens} +
Part[]{type:text|reasoning}）+ 真机 message JSON 构造。

覆盖规格书 8 项：①多轮→单 prompt 压缩 ②text/reasoning/usage 解析 ③桥超时
判负 ④ConnectionRefused 判负 ⑤伪流式 SSE 形状 ⑥模型名斜杠解析 ⑦match/守卫
白名单 ⑧finish 映射；另加 info.error 判负 / usage.total 回填 / 路由级非流式
OpenAI 形 + 伪流式端到端 + 桥掉线 503 且不冷却（两次实打桥 = 无冷却短路）。

运行：py -3.12 -m pytest tests/test_opencode_channel.py -v
"""

import json
import pathlib
import sys
import tempfile

import httpx
import pytest

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

# ─── 路径重定向（必须在 import providers / routes.proxy 之前）───────────────
# 与 test_provider_toggle / test_stream_sniff 同款：把 settings 路径常量指到
# 临时目录，绝不触碰仓库根数据文件。单文件运行时生效；全量收集时
# proxy_core.state 已被别的测试实例化，改路径对其内存无效——此时仅在
# fixture 里按需覆写（用法同 test_provider_toggle）。
_TMP = pathlib.Path(tempfile.mkdtemp(prefix="qidi-relay-opencode-"))
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
from proxy_core import opencode          # noqa: E402

if "proxy_core.state" not in sys.modules:
    PV.CONFIG_FILE = S.CONFIG_FILE

import routes.proxy as RP                # noqa: E402
from flask import Flask                  # noqa: E402


# ─── 桥响应样例（形状照 /doc + 真机实测）──────────────────────────────────

def _bridge_json(text="你好，世界。", reasoning="先想一想…", finish="stop",
                 include_total=True, total=18):
    tokens = {"input": 11, "output": 7, "reasoning": 3,
              "cache": {"read": 0, "write": 0}}
    if include_total:
        tokens["total"] = total
    return {
        "info": {"id": "msg_1", "role": "assistant", "sessionID": "ses_1",
                 "modelID": "ling-3.1-flash-free", "tokens": tokens,
                 "finish": finish},
        "parts": [
            {"type": "step-start"},
            {"type": "reasoning", "text": reasoning},
            {"type": "text", "text": text},
            {"type": "step-finish"},
        ],
    }


class _Resp:
    def __init__(self, status, payload=None, text=""):
        self.status_code = status
        self._payload = payload
        self.text = text if text else json.dumps(payload or {})

    def json(self):
        return self._payload


def _install_client(monkeypatch, message_payload=None, *,
                    raise_on_post=None, session_status=200,
                    message_status=200, session_payload=None):
    """装一个假 httpx.Client：记录构造 kwargs + post 调用。"""
    class _C:
        calls = []
        kwargs = {}

        def __init__(self, *a, **k):
            _C.kwargs = k
            _C.calls = []

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, json=None, headers=None, timeout=None):
            _C.calls.append((url, json))
            if raise_on_post is not None:
                raise raise_on_post
            if url.endswith("/session"):
                return _Resp(session_status, session_payload or {"id": "ses_test"})
            return _Resp(message_status, message_payload)

    monkeypatch.setattr(httpx, "Client", _C)
    return _C


# ─────────────────────────── ①② 压缩 + 解析 ───────────────────────────────

def test_compress_multiturn_to_single_prompt():
    """① 多轮 messages → 单条 prompt：system 置顶、历史按 role 标注、末条 user 落点。"""
    msgs = [
        {"role": "system", "content": "你是助手"},
        {"role": "user", "content": "第一问"},
        {"role": "assistant", "content": "第一答"},
        {"role": "user", "content": "第二问"},
    ]
    p = opencode.compress_messages(msgs)
    assert p.startswith("你是助手")                       # system 置顶
    assert "[历史·用户] 第一问" in p
    assert "[历史·助手] 第一答" in p
    assert "[最后一条用户] 第二问" in p
    # 末条 user 只作落点，不出现在历史段（顺序对）
    assert p.index("[历史·用户] 第一问") < p.index("[最后一条用户] 第二问")
    assert "第二问" not in p.split("[最后一条用户]")[0]
    # assistant.tool_calls / tool 结果也压缩进历史
    msgs2 = [
        {"role": "user", "content": "调工具"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "read_file", "arguments": '{"path":"a"}'}}]},
        {"role": "tool", "content": "文件内容"},
        {"role": "user", "content": "总结"},
    ]
    p2 = opencode.compress_messages(msgs2)
    assert "[调用工具 read_file]" in p2
    assert "[历史·工具结果] 文件内容" in p2
    assert "[最后一条用户] 总结" in p2


def test_send_chat_parses_text_reasoning_usage(monkeypatch):
    """② parts 解析：text→content、reasoning→reasoning、其余忽略；usage 三项。"""
    _C = _install_client(monkeypatch, _bridge_json())
    r = opencode.send_chat([{"role": "user", "content": "hi"}],
                           "ling-3.1-flash-free")
    assert r["content"] == "你好，世界。"
    assert r["reasoning"] == "先想一想…"
    assert r["usage"] == {"input": 11, "output": 7, "total": 18}
    assert r["finish"] == "stop"
    assert r["model_served"] == "ling-3.1-flash-free"
    # 桥请求流：先建 session，再发单条 text part 到该 session
    urls = [u for u, _ in _C.calls]
    assert urls[0].endswith("/session")
    assert urls[1].endswith("/ses_test/message")
    body = _C.calls[1][1]
    assert body["model"] == {"providerID": "opencode",
                             "modelID": "ling-3.1-flash-free"}
    # part 文本 = 会话压缩后的单 prompt（末条 user 落点带 [最后一条用户] 标记）
    assert body["parts"] == [{"type": "text",
                              "text": opencode.compress_messages(
                                  [{"role": "user", "content": "hi"}])}]
    # 桥是本机端点：客户端必须禁 env 代理（否则 loopback 被塞进系统代理）
    assert _C.kwargs.get("trust_env") is False


def test_send_chat_usage_total_fallback(monkeypatch):
    """②b tokens.total 缺失时由 input+output 回填。"""
    _install_client(monkeypatch, _bridge_json(include_total=False))
    r = opencode.send_chat([{"role": "user", "content": "hi"}], "m")
    assert r["usage"] == {"input": 11, "output": 7, "total": 18}


# ─────────────────────────── ③④ 判负 ───────────────────────────────────────

def test_send_chat_timeout_judged_negative(monkeypatch):
    """③ 桥超时 → OpenCodeBridgeError（可判负异常，不冷却）。"""
    _install_client(monkeypatch, None, raise_on_post=httpx.ReadTimeout("read timed out"))
    with pytest.raises(opencode.OpenCodeBridgeError):
        opencode.send_chat([{"role": "user", "content": "hi"}], "m", timeout=0.01)


def test_send_chat_connection_refused_judged_negative(monkeypatch):
    """④ 连接被拒（ConnectionRefused）→ OpenCodeBridgeError。"""
    _install_client(monkeypatch, None,
                    raise_on_post=httpx.ConnectError("[WinError 10061] 由于目标计算机积极拒绝"))
    with pytest.raises(opencode.OpenCodeBridgeError):
        opencode.send_chat([{"role": "user", "content": "hi"}], "m")


def test_send_chat_info_error_judged_negative(monkeypatch):
    """桥返回 200 但 info.error 非空（上游错误）→ 判负。"""
    payload = _bridge_json()
    payload["info"]["error"] = {"name": "ProviderAuthError", "data": {"message": "401"}}
    _install_client(monkeypatch, payload)
    with pytest.raises(opencode.OpenCodeBridgeError):
        opencode.send_chat([{"role": "user", "content": "hi"}], "m")


def test_send_chat_non200_judged_negative(monkeypatch):
    """桥 /session 非 200 → 判负。"""
    _install_client(monkeypatch, None, session_status=500, session_payload={})
    with pytest.raises(opencode.OpenCodeBridgeError):
        opencode.send_chat([{"role": "user", "content": "hi"}], "m")


# ─────────────────────────── ⑤ 伪流式 ─────────────────────────────────────

def test_pseudo_stream_sse_shape():
    """⑤ 伪流式 SSE 形状：role 帧 → 逐行 content 帧 → finish 帧 → usage 帧 → [DONE]。"""
    resp_obj = {
        "id": "chatcmpl-opencode-1", "object": "chat.completion",
        "created": 1700000000, "model": "opencode/ling-3.1-flash-free",
        "choices": [{"index": 0,
                     "message": {"role": "assistant",
                                 "content": "第一行\n第二行",
                                 "reasoning": "想"},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 4, "total_tokens": 5},
    }
    lines = RP._opencode_sse_lines(resp_obj)
    assert lines[-1] == "data: [DONE]\n\n"
    parsed = [json.loads(l[6:]) for l in lines[:-1]]
    # 每条都是合法 chunk、带 id/object/model
    for o in parsed:
        assert o["object"] == "chat.completion.chunk"
        assert o["model"] == "opencode/ling-3.1-flash-free"
        assert o["id"] == "chatcmpl-opencode-1"
    # 首帧给 role，末帧（usage 帧前）带 finish_reason=stop
    assert parsed[0]["choices"][0]["delta"]["role"] == "assistant"
    finish_frames = [o for o in parsed if o["choices"]
                     and o["choices"][0]["finish_reason"]]
    assert finish_frames[0]["choices"][0]["finish_reason"] == "stop"
    # usage 帧：choices 空 + 顶层 usage
    assert parsed[-1]["choices"] == []
    assert parsed[-1]["usage"]["total_tokens"] == 5
    # 逐行重组 content == 原文（不丢不多）
    recon = "".join(ch["delta"].get("content", "")
                    for o in parsed for ch in o["choices"])
    assert recon == "第一行\n第二行"


# ─────────────────────────── ⑥ 斜杠解析 / ⑧ finish ─────────────────────────

def test_model_slash_parse():
    """⑥ `opencode/<id>` → `<id>`；裸名回落原样。"""
    assert RP._parse_opencode_model_id("opencode/ling-3.1-flash-free") == "ling-3.1-flash-free"
    assert RP._parse_opencode_model_id("ling-3.1-flash-free") == "ling-3.1-flash-free"
    assert RP._parse_opencode_model_id("grok-code") == "grok-code"
    assert RP._parse_opencode_model_id("  opencode/glm-5-free  ") == "glm-5-free"
    # 非 opencode 前缀不剥（防误伤别家带斜杠模型名）
    assert RP._parse_opencode_model_id("nvidia/nemotron:free") == "nvidia/nemotron:free"


def test_finish_mapping():
    """⑧ finish 映射：stop→stop，其余如实（空→stop 兜底）。"""
    assert opencode._map_finish("stop") == "stop"
    assert opencode._map_finish("length") == "length"
    assert opencode._map_finish("tool-calls") == "tool-calls"
    assert opencode._map_finish(None) == "stop"
    assert opencode._map_finish("") == "stop"


# ─────────────────────────── ⑦ match / 守卫白名单 ─────────────────────────

def test_match_resolves_opencode_provider(tmp_path, monkeypatch):
    """⑦ providers.match 命中 opencode 模型（带前缀整串匹配，沿用 openrouter 先例）。"""
    cfg = {"providers": {"opencode": {
        "type": "opencode-serve", "base_url": "http://127.0.0.1:4096",
        "models": ["opencode/ling-3.1-flash-free", "opencode/glm-5-free"]}}}
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    # load_config() 读的是 settings.CONFIG_FILE；providers 的 mtime 缓存看
    # providers.CONFIG_FILE——两者都要指到临时配置（同 test_provider_toggle）。
    monkeypatch.setattr(S, "CONFIG_FILE", cfg_file)
    monkeypatch.setattr(PV, "CONFIG_FILE", cfg_file)
    PV._cache = None
    prov = PV.match("opencode/ling-3.1-flash-free")
    assert prov is not None and prov["name"] == "opencode"
    assert prov["type"] == "opencode-serve"
    assert "opencode/ling-3.1-flash-free" in [m["id"] for m in PV.all_models()]
    assert PV.is_opencode(prov) is True


# ─────────────────────────── 路由级端到端 ─────────────────────────────────

@pytest.fixture()
def route_client(tmp_path, monkeypatch):
    cfg = {
        "pool_models": ["GLM-5.3"], "model_groups": {}, "roles": {},
        "vision_model": "GLM-5.3", "vision_capable_models": [],
        "pricing": {}, "features": {},
        "providers": {"opencode": {
            "type": "opencode-serve", "base_url": "http://127.0.0.1:4096",
            "models": ["opencode/ling-3.1-flash-free", "opencode/glm-5-free"]}},
    }
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    monkeypatch.setattr(S, "CONFIG_FILE", cfg_file)
    monkeypatch.setattr(PV, "CONFIG_FILE", cfg_file)
    PV._cache = None
    app = Flask(__name__)
    app.register_blueprint(RP.bp)
    app.config["TESTING"] = True
    return app.test_client()


def test_route_nonstream_openai_shape_and_guard(route_client, monkeypatch):
    """⑥⑦ 端到端：守卫白名单放行（非 404）+ 斜杠解析 + OpenAI 非流式形。"""
    seen = {}

    def fake_send_chat(messages, model_id, timeout=None, provider_name=""):
        seen["model_id"] = model_id
        seen["provider_name"] = provider_name
        seen["messages"] = messages
        return {"content": "桥答", "reasoning": "桥想",
                "usage": {"input": 3, "output": 5, "total": 8},
                "finish": "stop", "model_served": "ling-3.1-flash-free"}

    monkeypatch.setattr(opencode, "send_chat", fake_send_chat)
    r = route_client.post("/v1/chat/completions", json={
        "model": "opencode/ling-3.1-flash-free", "stream": False,
        "messages": [{"role": "user", "content": "你好"}]})
    assert r.status_code == 200, r.get_data(as_text=True)
    j = r.get_json()
    assert j["object"] == "chat.completion"
    assert j["model"] == "opencode/ling-3.1-flash-free"
    assert j["choices"][0]["message"]["content"] == "桥答"
    assert j["choices"][0]["message"]["reasoning_content"] == "桥想"
    assert j["choices"][0]["finish_reason"] == "stop"
    assert j["usage"] == {"prompt_tokens": 3, "completion_tokens": 5,
                          "total_tokens": 8}
    assert seen["model_id"] == "ling-3.1-flash-free"     # ⑥ 斜杠已剥
    assert seen["provider_name"] == "opencode"


def test_route_stream_pseudo_sse(route_client, monkeypatch):
    """⑤ 端到端：流式请求走伪流式（真 SSE + [DONE]，逐行重组==原文）。"""
    monkeypatch.setattr(opencode, "send_chat", lambda *a, **k: {
        "content": "第一行\n第二行", "reasoning": "",
        "usage": {"input": 1, "output": 4, "total": 5},
        "finish": "stop", "model_served": "ling-3.1-flash-free"})
    r = route_client.post("/v1/chat/completions", json={
        "model": "opencode/ling-3.1-flash-free", "stream": True,
        "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    assert "text/event-stream" in r.headers["Content-Type"]
    body = r.get_data(as_text=True)
    assert body.rstrip().endswith("data: [DONE]")
    recon = ""
    for line in body.splitlines():
        if not line.startswith("data: "):
            continue
        p = line[6:]
        if p == "[DONE]":
            continue
        for ch in json.loads(p).get("choices", []):
            recon += ch["delta"].get("content", "")
    assert recon == "第一行\n第二行"


def test_route_bridge_down_503_no_cooldown(route_client, monkeypatch):
    """③ 端到端：桥掉线 503 判负且不冷却（连打两次仍实打桥=无冷却短路）。"""
    calls = {"n": 0}

    def boom(*a, **k):
        calls["n"] += 1
        raise opencode.OpenCodeBridgeError("拒绝连接")

    monkeypatch.setattr(opencode, "send_chat", boom)
    payload = {"model": "opencode/ling-3.1-flash-free", "stream": False,
               "messages": [{"role": "user", "content": "hi"}]}
    r1 = route_client.post("/v1/chat/completions", json=payload)
    r2 = route_client.post("/v1/chat/completions", json=payload)
    assert r1.status_code == 503 and r2.status_code == 503
    assert r1.get_json()["error"]["code"] == "opencode_bridge_unreachable"
    assert calls["n"] == 2   # 第二次没被任何冷却短路，仍实打桥
