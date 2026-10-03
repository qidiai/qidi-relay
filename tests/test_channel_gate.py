# -*- coding: utf-8 -*-
"""外部三通道流内嗅探门（T3 / 方案 v2 §3.2）回归：全测试对本地
mock 服务器发请求（127.0.0.1，零真实网络）。

勘察结论（决定本文件测试形状，见派工报告）：
- cline 上游原生 OpenAI 形（routes/proxy.py:1405-1406 docstring）；
- external 上游原生 OpenAI 兼容形（routes/proxy.py:1972）；
- minicode 上游原生 Anthropic Messages 形，但翻译 wrapper
  （MiniCodeStreamWrap）挂在 opened.resp 层——嗅探门 peek 的
  是翻译后的 OpenAI 形字节（error 事件已被翻译成 OpenAI 错误块
  proxy_core/minicode.py:826-831；纯 thinking→reasoning_content、
  tool_use→tool_calls 都是合法内容），故三条通道在门层均为
  OpenAI 形，_gate_classify 无需扩展（规格："若某通道上游是
  OpenAI 形则无需扩展——以勘察为准"）。

覆盖：每通道 判负→换下一通道/键且冷却函数零调用（spy）；
判健康→字节零丢失 + wrapper 输出形状不回归；features off
→ 与升级前逐字节一致（垃圾流原样透传、不换通道）；
全部判负→既有 503 路径；/api/gate 与 _gate_waiters 计数契约。

运行：py -3.12 -m pytest tests/test_channel_gate.py -v
"""

import json
import os
import pathlib
import socket
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
os.environ.setdefault("CODELY_CLI_VERSION", "0.0.0-test")  # 供 subprocess 探 CLI 版本

TMP = pathlib.Path(tempfile.mkdtemp(prefix="qidi-relay-gate-"))
(TMP / "config.json").write_text(json.dumps({
    "port": 0,
    "features": {"affinity": False},
    "pool_models": ["GLM-5.3", "KIMI-K3"],
    "model_groups": {},
    "providers": {},
    "roles": {},
    "vision_model": "GLM-5.3",
    "vision_capable_models": [],
    "pricing": {},
}), encoding="utf-8")
(TMP / "accounts.json").write_text(json.dumps({"accounts": [
    {"user_id": uid, "username": f"t{uid}", "org_id": "0", "access_token": "a.b.c",
     "enabled": True, "has_glm53": False, "budget_exceeded": False, "group": ""}
    for uid in ("9001", "9002", "9003")]}), encoding="utf-8")

# 路径重定向（条件化，与 test_stream_sniff.py 同思路）：
# proxy_core.state 的 am 单例在首次 import 时按 accounts.py
# 的 ACCOUNTS_FILE 快照加载账号——water/providers/accounts/
# stats/keystore/router/db 都不 import state（已逐一核实），
# 故可用 sys.modules 判定 state 是否已被实例化。
# · 全量收集时本文件按文件名最先被收集，state 尚未实例化
#   → import 时覆写路径，am 加载本文件的 9001-9003 账号；
# · 若未来有更早导入 state 的文件，条件不成立，靠下方
#   fixture 切换，两文件互不污染。
if "proxy_core.state" not in sys.modules:
    import proxy_core.settings as S  # noqa: E402
    import proxy_core.accounts as A  # noqa: E402
    import proxy_core.stats as ST  # noqa: E402
    import proxy_core.water as WA  # noqa: E402
    import proxy_core.providers as PV  # noqa: E402
    S.BASE_DIR = TMP
    S.CONFIG_FILE = TMP / "config.json"
    S.ACCOUNTS_FILE = TMP / "accounts.json"
    S.STATS_FILE = TMP / "token_stats.json"
    S.WATER_FILE = TMP / "water_check.json"
    S.LOG_FILE = TMP / "proxy.log"
    A.ACCOUNTS_FILE = S.ACCOUNTS_FILE
    ST.STATS_FILE = S.STATS_FILE
    WA.WATER_FILE = S.WATER_FILE
    PV.CONFIG_FILE = S.CONFIG_FILE

import proxy_core.settings as S  # noqa: E402
import proxy_core.accounts as A  # noqa: E402
import proxy_core.stats as ST  # noqa: E402
import proxy_core.water as WA  # noqa: E402
import proxy_core.providers as PV  # noqa: E402
from proxy_core import cline  # noqa: E402
from proxy_core import minicode  # noqa: E402

# ─── mock 上游：HITS 记录每次请求的 Authorization ────────────────

HITS = []          # 每次请求的 Authorization 头
BEHAVIOR = {}      # "Bearer cl-1" -> "ok" | "gate_err" | "mc_ok" | ...


def _anthropic_sse(*events):
    """Anthropic Messages 形事件流（event: 行 + data: 行）。"""
    out = b""
    for event, data in events:
        out += f"event: {event}\n".encode("utf-8")
        out += ("data: " + json.dumps(data, ensure_ascii=False)
                + "\n\n").encode("utf-8")
    return out


# minicode 上游（Anthropic 形）各模式事件流
MC_OK_EVENTS = _anthropic_sse(
    ("message_start", {"type": "message_start",
                       "message": {"id": "msg-gate-ok",
                                   "usage": {"input_tokens": 3}}}),
    ("content_block_delta", {"type": "content_block_delta", "index": 0,
                             "delta": {"type": "text_delta", "text": "he"}}),
    ("content_block_delta", {"type": "content_block_delta", "index": 0,
                             "delta": {"type": "text_delta", "text": "llo"}}),
    ("message_delta", {"type": "message_delta",
                       "delta": {"stop_reason": "end_turn"},
                       "usage": {"output_tokens": 2}}),
    ("message_stop", {"type": "message_stop"}),
)
MC_ERR_EVENTS = _anthropic_sse(
    ("message_start", {"type": "message_start",
                       "message": {"id": "msg-gate-err",
                                   "usage": {"input_tokens": 3}}}),
    ("error", {"type": "error",
               "error": {"type": "api_error",
                         "message": "minicode upstream exploded"}}),
    ("message_stop", {"type": "message_stop"}),
)
MC_EMPTY_EVENTS = _anthropic_sse(
    ("message_start", {"type": "message_start",
                       "message": {"id": "msg-gate-empty",
                                   "usage": {"input_tokens": 3}}}),
    ("message_stop", {"type": "message_stop"}),
)
MC_THINK_EVENTS = _anthropic_sse(
    ("message_start", {"type": "message_start",
                       "message": {"id": "msg-gate-think",
                                   "usage": {"input_tokens": 3}}}),
    ("content_block_delta", {"type": "content_block_delta", "index": 0,
                             "delta": {"type": "thinking_delta",
                                       "thinking": "暗中思考"}}),
    ("message_delta", {"type": "message_delta",
                       "delta": {"stop_reason": "end_turn"},
                       "usage": {"output_tokens": 2}}),
    ("message_stop", {"type": "message_stop"}),
)
MC_TOOL_EVENTS = _anthropic_sse(
    ("message_start", {"type": "message_start",
                       "message": {"id": "msg-gate-tool",
                                   "usage": {"input_tokens": 3}}}),
    ("content_block_start", {"type": "content_block_start", "index": 0,
                             "content_block": {"type": "tool_use",
                                               "id": "toolu_gate_1",
                                               "name": "get_weather",
                                               "input": {}}}),
    ("content_block_delta", {"type": "content_block_delta", "index": 0,
                             "delta": {"type": "input_json_delta",
                                       "partial_json": "{\"city\":\"合肥\"}"}}),
    ("message_delta", {"type": "message_delta",
                       "delta": {"stop_reason": "tool_use"},
                       "usage": {"output_tokens": 2}}),
    ("message_stop", {"type": "message_stop"}),
)

# cline/external 上游（OpenAI 形）各模式事件流
OPENAI_OK_LINES = (
    'data: {"model":"upstream-gate","choices":[{"delta":{"content":"he"}}]}\n\n',
    'data: {"model":"upstream-gate","choices":[{"delta":{"content":"llo"}}],'
    '"usage":{"prompt_tokens":11,"completion_tokens":2,"total_tokens":13}}\n\n',
    "data: [DONE]\n\n",
)
OPENAI_ERR_FRAME = (
    'data: {"error":{"message":"upstream exploded",'
    '"type":"server_error","code":"boom"}}\n\n'
)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *args):
        pass

    def do_POST(self):  # noqa: N802
        self.rfile.read(int(self.headers.get("content-length") or 0))
        auth = self.headers.get("authorization", "")
        HITS.append(auth)
        mode = BEHAVIOR.get(auth, "402")
        if self.path.endswith("/messages"):
            self._serve_anthropic(mode)
        else:
            self._serve_openai(mode)

    def _serve_openai(self, mode):
        if mode == "ok":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for line in OPENAI_OK_LINES:
                self.wfile.write(line.encode("utf-8"))
                self.wfile.flush()
            return
        if mode == "gate_err":
            # 200 之后流内才报错（网关层包装失败形态）
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(OPENAI_ERR_FRAME.encode("utf-8"))
            self.wfile.flush()
            return
        if mode == "gate_empty":
            # 空流：只有终止符，无任何内容事件
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            return
        if mode == "gate_then_break":
            # 门判健康后上游才坏：先发健康内容帧（门判
            # ok、prefetched 交接），再谎报 Content-Length
            # 掐断写半侧 → live 续读时 httpx 抛
            # RemoteProtocolError（与 test_stream_sniff
            # 的 gate_raise 同机制）
            payload = (b'data: {"model":"upstream-gate","choices":'
                       b'[{"delta":{"content":"he"}}]}\n\n')
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(payload) + 500))
            self.end_headers()
            self.wfile.write(payload)
            self.wfile.flush()
            self.connection.shutdown(socket.SHUT_WR)
            return
        self._serve_plain_error(402)

    def _serve_anthropic(self, mode):
        if mode == "mc_ok":
            self._sse_bytes(MC_OK_EVENTS)
        elif mode == "mc_err":
            self._sse_bytes(MC_ERR_EVENTS)
        elif mode == "mc_empty":
            self._sse_bytes(MC_EMPTY_EVENTS)
        elif mode == "mc_think":
            self._sse_bytes(MC_THINK_EVENTS)
        elif mode == "mc_tool":
            self._sse_bytes(MC_TOOL_EVENTS)
        else:
            self._serve_plain_error(402)

    def _sse_bytes(self, payload):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self.wfile.write(payload)
        self.wfile.flush()

    def _serve_plain_error(self, code):
        body = json.dumps({"error": {"message": f"upstream rejects ({code})",
                                     "type": "invalid_request_error",
                                     "param": None, "code": str(code)}}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


_srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
_srv.daemon_threads = True
threading.Thread(target=_srv.serve_forever, daemon=True).start()
MOCK = f"http://127.0.0.1:{_srv.server_address[1]}"

import routes.proxy as RP  # noqa: E402
from routes.admin import bp as admin_bp  # noqa: E402
from proxy_core.state import am, api_cache  # noqa: E402

# 注意：与 test_stream_sniff.py 同理——不在 import 时覆写
# 全局路径/上游，靠下方 module 级 autouse fixture 切换并还原。

from flask import Flask  # noqa: E402

_app = Flask(__name__)
_app.register_blueprint(RP.bp)
_app.register_blueprint(admin_bp)
client = _app.test_client()

api_cache.get = lambda acc: f"key-{acc['user_id']}"      # 免向上游换 key


@pytest.fixture(scope="module", autouse=True)
def _use_channel_gate_mock():
    """把上游与磁盘路径切到本文件的 mock/临时目录（用例后还原）。

    与 test_stream_sniff.py 的 fixture 同构：pytest 按文件名
    收集时 import 阶段的覆写"后到者赢"，且全量收集时 am 等
    单例可能已由更早的文件实例化——fixture 保证本文件用例
    期间全局指向本文件 mock，用例结束还原，互不污染。
    """
    saved = (S.BASE_DIR, S.CONFIG_FILE, S.ACCOUNTS_FILE,
             S.STATS_FILE, S.WATER_FILE, S.LOG_FILE,
             A.ACCOUNTS_FILE, ST.STATS_FILE, WA.WATER_FILE,
             PV.CONFIG_FILE, RP.LITELLM_API_BASE, WA.LITELLM_API_BASE)
    S.BASE_DIR = TMP
    S.CONFIG_FILE = TMP / "config.json"
    S.ACCOUNTS_FILE = TMP / "accounts.json"
    S.STATS_FILE = TMP / "token_stats.json"
    S.WATER_FILE = TMP / "water_check.json"
    S.LOG_FILE = TMP / "proxy.log"
    A.ACCOUNTS_FILE = S.ACCOUNTS_FILE
    ST.STATS_FILE = S.STATS_FILE
    WA.WATER_FILE = S.WATER_FILE
    PV.CONFIG_FILE = S.CONFIG_FILE
    PV._cache = None          # providers 配置热重载：切目录即清缓存
    RP.LITELLM_API_BASE = MOCK
    WA.LITELLM_API_BASE = MOCK
    yield
    (S.BASE_DIR, S.CONFIG_FILE, S.ACCOUNTS_FILE,
     S.STATS_FILE, S.WATER_FILE, S.LOG_FILE,
     A.ACCOUNTS_FILE, ST.STATS_FILE, WA.WATER_FILE,
     PV.CONFIG_FILE, RP.LITELLM_API_BASE, WA.LITELLM_API_BASE) = saved
    PV._cache = None


# ─── 通道测试替身（绕开真实凭证文件；只实现被测链路用到的成员）───

class _FakeClineChannel:
    """Cline 通道测试替身。

    cooldown_calls 是 spy 列表——嗅探门判负红线（set_cooldown
    零调用）靠它取证。
    """

    def __init__(self, label, token):
        self.label = label
        self._token = token
        self.cooldown_until = 0.0
        self.cooldown_note = ""
        self.cooldown_calls = []

    def get_token(self):
        return self._token

    def invalidate(self):
        pass

    def set_cooldown(self, seconds, note=""):
        self.cooldown_calls.append((round(seconds, 3), note))
        self.cooldown_until = time.time() + max(0, seconds)
        self.cooldown_note = (note or "")[:160]

    @property
    def available(self):
        return time.time() >= self.cooldown_until

    def request_headers(self, token, session_id):
        return {"Authorization": f"Bearer {token}",
                "Content-Type": "application/json"}


class _FakeMiniChannel:
    """MiniCode 通道测试替身（Anthropic 协议，api_base 指向本地 mock）。"""

    def __init__(self, label, token, api_base):
        self.label = label
        self._token = token
        self.api_base = api_base
        self.max_tokens_default = 4096
        self.cooldown_until = 0.0
        self.cooldown_note = ""
        self.cooldown_calls = []

    def get_token(self):
        return self._token

    def invalidate(self):
        pass

    def set_cooldown(self, seconds, note=""):
        self.cooldown_calls.append((round(seconds, 3), note))
        self.cooldown_until = time.time() + max(1, seconds)
        self.cooldown_note = (note or "")[:120]

    @property
    def available(self):
        return time.time() >= self.cooldown_until

    def request_headers(self, token, session_id=None):
        return {"Authorization": f"Bearer {token}",
                "Content-Type": "application/json"}


def _install_cline_channels(monkeypatch, *channels):
    """把 cline 通道注册表换成测试替身。

    只替换 _forward_cline 用到的两个入口（channels_for/
    pick_channel）与发请求用的 API_BASE——被测代码
    （门接线本身）一动不动。
    """
    chs = list(channels)

    def _pick(name, exclude=None):
        return next((c for c in chs
                     if c.available
                     and (not exclude or c.label not in exclude)),
                    None)

    monkeypatch.setattr(cline, "channels_for", lambda name: list(chs))
    monkeypatch.setattr(cline, "pick_channel", _pick)
    monkeypatch.setattr(cline, "API_BASE", MOCK)


def _install_minicode_channels(monkeypatch, *channels):
    """把 minicode 通道注册表换成测试替身（同上）。"""
    chs = list(channels)

    def _pick(name, exclude=None):
        return next((c for c in chs
                     if c.available
                     and (not exclude or c.label not in exclude)),
                    None)

    monkeypatch.setattr(minicode, "channels_for", lambda name: list(chs))
    monkeypatch.setattr(minicode, "pick_channel", _pick)


def _use_channel_providers(features=None):
    """把 TMP/config.json 的 providers 段指向本地 mock
    （providers 按 mtime 热重载，写入即生效）。"""
    cfg = json.loads((TMP / "config.json").read_text(encoding="utf-8"))
    cfg["providers"] = {
        "clgate": {"type": "cline", "models": ["cl-gate-model"]},
        "mcgate": {"type": "minicode", "base_url": MOCK,
                   "models": ["mc-gate-model"]},
        "extgate": {"base_url": MOCK,
                    "api_keys": [{"key": "ek-1", "label": "k1"},
                                 {"key": "ek-2", "label": "k2"}],
                    "models": ["ext-gate-model"]},
    }
    if features is None:
        cfg.pop("features", None)
    else:
        cfg["features"] = features
    (TMP / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
    PV._cache = None


def _reset(behavior):
    HITS.clear()
    BEHAVIOR.clear()
    BEHAVIOR.update(behavior)
    am.index = 0
    api_cache.cache.clear()
    RP._prov_quota_cooldown.clear()   # provider 冷却是模块级状态，必须清
    PV._key_rr.clear()                # pick_key 轮转计数同为模块级
                                       # （proxy_core/providers.py:215-221），
                                       # 清零否则跨用例轮转使 external
                                       # 用例的首个 key 不确定
    for a in am.accounts:
        a["enabled"] = True
        a["budget_exceeded"] = False
        a["budget_cooldown_until"] = 0
        a["cooldown_reason"] = ""
        a["quota_team"] = ""
        a["group"] = ""


def _post(model, stream=True):
    return client.post("/v1/chat/completions", json={
        "model": model, "stream": stream,
        "messages": [{"role": "user", "content": "hi"}]})


def _spy_failover_bans(monkeypatch):
    """给账号级冷却/惩罚函数装 spy，返回记录列表。

    嗅探门判负红线：绝不 _penalize_account、绝不
    am.mark_cooldown——spy 零调用即证明（与主池门同款断言）。
    """
    banned = []

    def _spy_penalize(uid, opened, model):
        banned.append(("penalize", uid))

    def _spy_mark(uid, secs, reason):
        banned.append(("mark_cooldown", uid))

    monkeypatch.setattr(RP, "_penalize_account", _spy_penalize)
    monkeypatch.setattr(am, "mark_cooldown", _spy_mark)
    return banned


def _sse_data_lines(body):
    return [l[5:].strip() for l in body.split("\n") if l.startswith("data:")]


def _assert_mc_ok_body(body):
    """minicode 判健康：wrapper 输出形状不回归 + 零丢失。

    role + he + llo + 末块(finish_reason+usage) + [DONE] 共
    5 条 data 行——一条不多一条不少（peek 预读的字节经
    prefetched 也在里面，一个都不丢）。id/created 由 wrapper
    生成（created 取时间戳，非确定），故按结构断言而非逐字节。
    """
    lines = _sse_data_lines(body)
    assert len(lines) == 5, lines
    role = json.loads(lines[0])
    assert role["model"] == "mc-gate-model"
    assert role["choices"][0]["delta"] == {"role": "assistant"}
    assert json.loads(lines[1])["choices"][0]["delta"]["content"] == "he"
    assert json.loads(lines[2])["choices"][0]["delta"]["content"] == "llo"
    final = json.loads(lines[3])
    assert final["choices"][0]["finish_reason"] == "stop"
    assert final["usage"] == {"prompt_tokens": 3, "completion_tokens": 2,
                              "total_tokens": 5}
    assert lines[4] == "[DONE]"


# ─── 判健康/判负的逐字节期望（与上游所发逐字节对应）──────────────

# cline：上游字节经 ClineStreamWrap 的 model 改名 +
# json.dumps 重序列化（proxy_core/cline.py:688，
# ensure_ascii=False 默认分隔符带空格）——与升级前
# wrapper 输出逐字节一致
EXPECTED_CLINE_SSE = (
    'data: {"model": "cl-gate-model", "choices": [{"delta": {"content": "he"}}]}\n\n'
    'data: {"model": "cl-gate-model", "choices": [{"delta": {"content": "llo"}}], '
    '"usage": {"prompt_tokens": 11, "completion_tokens": 2, "total_tokens": 13}}\n\n'
    "data: [DONE]\n\n"
)
# external：无 wrapper，客户端收到上游原始字节
EXPECTED_EXT_SSE = (
    'data: {"model":"upstream-gate","choices":[{"delta":{"content":"he"}}]}\n\n'
    'data: {"model":"upstream-gate","choices":[{"delta":{"content":"llo"}}],'
    '"usage":{"prompt_tokens":11,"completion_tokens":2,"total_tokens":13}}\n\n'
    "data: [DONE]\n\n"
)
# features off 时 cline 垃圾流的逐字节透传：上游 error 帧经
# ClineStreamWrap 的 model 改名——与升级前行为逐字节一致
CLINE_GATE_ERR_PASSTHROUGH = (
    'data: {"error": {"message": "upstream exploded", '
    '"type": "server_error", "code": "boom"}, '
    '"model": "cl-gate-model"}\n\n'
)
# features off 时 external 垃圾流的逐字节透传：无 wrapper，原样透传
EXT_GATE_ERR_PASSTHROUGH = OPENAI_ERR_FRAME


# ─── cline 通道（上游原生 OpenAI 形）───────────────────────────

def test_cline_gate_healthy_zero_byte_loss(monkeypatch):
    """cline 判健康：prefetched + 续读逐字节等于升级前透传结果。"""
    ch1 = _FakeClineChannel("cl-a", "cl-1")
    _install_cline_channels(monkeypatch, ch1)
    _use_channel_providers()
    _reset({"Bearer cl-1": "ok"})
    banned = _spy_failover_bans(monkeypatch)
    r = _post("cl-gate-model")
    assert r.status_code == 200, r.status_code
    assert r.get_data(as_text=True) == EXPECTED_CLINE_SSE
    assert HITS == ["Bearer cl-1"]          # 健康首通道一次命中，无换通道
    assert "text/event-stream" in r.headers["Content-Type"]
    assert ch1.cooldown_calls == []
    assert banned == []


def test_cline_gate_inband_error_fails_over_without_cooldown(monkeypatch):
    """cline 判负→换下一通道：首通道 200+流内 error 帧，
    由第二通道正常服务；冷却函数零调用（审计红线）。

    判负只耗 attempt 预算——ch.set_cooldown / am.mark_cooldown /
    _penalize_account 均零调用（误判慢通道不能把它冷却掉）。
    """
    ch1 = _FakeClineChannel("cl-a", "cl-1")
    ch2 = _FakeClineChannel("cl-b", "cl-2")
    _install_cline_channels(monkeypatch, ch1, ch2)
    _use_channel_providers()
    _reset({"Bearer cl-1": "gate_err", "Bearer cl-2": "ok"})
    banned = _spy_failover_bans(monkeypatch)
    r = _post("cl-gate-model")
    assert r.status_code == 200, r.status_code
    assert r.get_data(as_text=True) == EXPECTED_CLINE_SSE
    assert HITS == ["Bearer cl-1", "Bearer cl-2"]
    assert ch1.cooldown_calls == []
    assert ch2.cooldown_calls == []
    assert banned == []


def test_cline_gate_empty_stream_fails_over_without_cooldown(monkeypatch):
    """cline 空流（[DONE] 无内容事件）→ 判负换通道，冷却零调用。"""
    ch1 = _FakeClineChannel("cl-a", "cl-1")
    ch2 = _FakeClineChannel("cl-b", "cl-2")
    _install_cline_channels(monkeypatch, ch1, ch2)
    _use_channel_providers()
    _reset({"Bearer cl-1": "gate_empty", "Bearer cl-2": "ok"})
    banned = _spy_failover_bans(monkeypatch)
    r = _post("cl-gate-model")
    assert r.status_code == 200, r.status_code
    assert r.get_data(as_text=True) == EXPECTED_CLINE_SSE
    assert HITS == ["Bearer cl-1", "Bearer cl-2"]
    assert ch1.cooldown_calls == []
    assert ch2.cooldown_calls == []
    assert banned == []


def test_cline_all_channels_gate_bad_returns_503(monkeypatch):
    """全部通道被判负：attempts 预算耗尽 → 既有 503 路径
    （external_credentials_unavailable），不冷却任何通道。"""
    ch1 = _FakeClineChannel("cl-a", "cl-1")
    ch2 = _FakeClineChannel("cl-b", "cl-2")
    _install_cline_channels(monkeypatch, ch1, ch2)
    _use_channel_providers()
    _reset({"Bearer cl-1": "gate_err", "Bearer cl-2": "gate_err"})
    banned = _spy_failover_bans(monkeypatch)
    r = _post("cl-gate-model")
    assert r.status_code == 503
    assert r.get_json()["error"]["code"] == "external_credentials_unavailable"
    assert HITS == ["Bearer cl-1", "Bearer cl-2"]
    assert ch1.cooldown_calls == []
    assert ch2.cooldown_calls == []
    assert banned == []


def test_cline_gate_off_passthrough_byte_identical(monkeypatch):
    """features.stream_gate=False：垃圾流逐字节透传给客户端
    （经 ClineStreamWrap 的 model 改名——与升级前完全一致），
    且绝不换通道重试、冷却零调用。"""
    ch1 = _FakeClineChannel("cl-a", "cl-1")
    _install_cline_channels(monkeypatch, ch1)
    _use_channel_providers(features={"stream_gate": False})
    _reset({"Bearer cl-1": "gate_err"})
    banned = _spy_failover_bans(monkeypatch)
    r = _post("cl-gate-model")
    assert r.status_code == 200
    assert r.get_data(as_text=True) == CLINE_GATE_ERR_PASSTHROUGH
    assert HITS == ["Bearer cl-1"]      # 门关：不判负、不换通道
    assert ch1.cooldown_calls == []
    assert banned == []


# ─── minicode 通道（上游原生 Anthropic 形，门层见翻译后字节）─────

def test_minicode_gate_healthy_zero_byte_loss(monkeypatch):
    """minicode 判健康：wrapper 输出形状不回归 + 字节零丢失。"""
    ch1 = _FakeMiniChannel("mc-a", "mc-1", MOCK)
    _install_minicode_channels(monkeypatch, ch1)
    _use_channel_providers()
    _reset({"Bearer mc-1": "mc_ok"})
    banned = _spy_failover_bans(monkeypatch)
    r = _post("mc-gate-model")
    assert r.status_code == 200, r.status_code
    _assert_mc_ok_body(r.get_data(as_text=True))
    assert HITS == ["Bearer mc-1"]
    assert ch1.cooldown_calls == []
    assert banned == []


def test_minicode_gate_error_event_fails_over_without_cooldown(monkeypatch):
    """minicode 判负→换下一通道：首通道 200+Anthropic error 事件
    （经 wrapper 翻译成 OpenAI 错误块 → 门判负），由第二通道
    正常服务；冷却函数零调用（审计红线）。"""
    ch1 = _FakeMiniChannel("mc-a", "mc-1", MOCK)
    ch2 = _FakeMiniChannel("mc-b", "mc-2", MOCK)
    _install_minicode_channels(monkeypatch, ch1, ch2)
    _use_channel_providers()
    _reset({"Bearer mc-1": "mc_err", "Bearer mc-2": "mc_ok"})
    banned = _spy_failover_bans(monkeypatch)
    r = _post("mc-gate-model")
    assert r.status_code == 200, r.status_code
    _assert_mc_ok_body(r.get_data(as_text=True))
    assert HITS == ["Bearer mc-1", "Bearer mc-2"]
    assert ch1.cooldown_calls == []
    assert ch2.cooldown_calls == []
    assert banned == []


def test_minicode_gate_empty_stream_fails_over_without_cooldown(monkeypatch):
    """minicode 空流（只有 message_start+message_stop，翻译后是
    role 帧+末块+[DONE] 无内容）→ 判负换通道，冷却零调用。"""
    ch1 = _FakeMiniChannel("mc-a", "mc-1", MOCK)
    ch2 = _FakeMiniChannel("mc-b", "mc-2", MOCK)
    _install_minicode_channels(monkeypatch, ch1, ch2)
    _use_channel_providers()
    _reset({"Bearer mc-1": "mc_empty", "Bearer mc-2": "mc_ok"})
    banned = _spy_failover_bans(monkeypatch)
    r = _post("mc-gate-model")
    assert r.status_code == 200, r.status_code
    _assert_mc_ok_body(r.get_data(as_text=True))
    assert HITS == ["Bearer mc-1", "Bearer mc-2"]
    assert ch1.cooldown_calls == []
    assert ch2.cooldown_calls == []
    assert banned == []


def test_minicode_gate_thinking_only_is_content(monkeypatch):
    """纯 thinking 流（thinking_delta→reasoning_content）是合法
    内容：门判健康放行，不换通道（与主池 reasoning 语义一致）。"""
    ch1 = _FakeMiniChannel("mc-a", "mc-1", MOCK)
    _install_minicode_channels(monkeypatch, ch1)
    _use_channel_providers()
    _reset({"Bearer mc-1": "mc_think"})
    banned = _spy_failover_bans(monkeypatch)
    r = _post("mc-gate-model")
    assert r.status_code == 200, r.status_code
    lines = _sse_data_lines(r.get_data(as_text=True))
    # role + reasoning_content + 末块 + [DONE]
    assert len(lines) == 4, lines
    assert json.loads(lines[1])["choices"][0]["delta"][
        "reasoning_content"] == "暗中思考"
    assert json.loads(lines[2])["choices"][0]["finish_reason"] == "stop"
    assert HITS == ["Bearer mc-1"]       # 判健康不换通道
    assert ch1.cooldown_calls == []
    assert banned == []


def test_minicode_gate_tool_only_is_content(monkeypatch):
    """纯工具调用流（content_block_start(tool_use)→tool_calls、
    input_json_delta→arguments）是合法内容：门判健康放行。"""
    ch1 = _FakeMiniChannel("mc-a", "mc-1", MOCK)
    _install_minicode_channels(monkeypatch, ch1)
    _use_channel_providers()
    _reset({"Bearer mc-1": "mc_tool"})
    banned = _spy_failover_bans(monkeypatch)
    r = _post("mc-gate-model")
    assert r.status_code == 200, r.status_code
    lines = _sse_data_lines(r.get_data(as_text=True))
    # role + tool_calls(建槽) + arguments 增量 + 末块 + [DONE]
    assert len(lines) == 5, lines
    tool = json.loads(lines[1])["choices"][0]["delta"]["tool_calls"][0]
    assert tool["id"] == "toolu_gate_1"
    assert tool["function"]["name"] == "get_weather"
    args = json.loads(lines[2])["choices"][0]["delta"]["tool_calls"][0]
    assert args["function"]["arguments"] == '{"city":"合肥"}'
    assert json.loads(lines[3])["choices"][0]["finish_reason"] == "tool_calls"
    assert HITS == ["Bearer mc-1"]
    assert ch1.cooldown_calls == []
    assert banned == []


def test_minicode_gate_off_passthrough_byte_identical(monkeypatch):
    """features.stream_gate=False：上游 error 事件经 wrapper 翻译
    的错误块逐字节透传（与升级前一致），不换通道、冷却零调用。"""
    ch1 = _FakeMiniChannel("mc-a", "mc-1", MOCK)
    _install_minicode_channels(monkeypatch, ch1)
    _use_channel_providers(features={"stream_gate": False})
    _reset({"Bearer mc-1": "mc_err"})
    banned = _spy_failover_bans(monkeypatch)
    r = _post("mc-gate-model")
    assert r.status_code == 200
    lines = _sse_data_lines(r.get_data(as_text=True))
    # message_start 的 role 帧 + 翻译后的 OpenAI 错误块 +
    # 末块 + [DONE]（wrapper 语义不变，共 4 条 data 行）
    assert len(lines) == 4, lines
    assert json.loads(lines[0])["choices"][0]["delta"] == {"role": "assistant"}
    err = json.loads(lines[1])
    assert err["error"]["message"] == "minicode upstream exploded"
    assert err["error"]["type"] == "api_error"
    assert json.loads(lines[2])["choices"][0]["finish_reason"] == "stop"
    assert HITS == ["Bearer mc-1"]       # 门关：不判负、不换通道
    assert ch1.cooldown_calls == []
    assert banned == []


# ─── external 通道（上游原生 OpenAI 兼容形，无 wrapper）──────────

def test_external_gate_healthy_zero_byte_loss(monkeypatch):
    """external 判健康：客户端收到上游原始字节（零丢失）。"""
    _use_channel_providers()
    _reset({"Bearer ek-1": "ok"})
    banned = _spy_failover_bans(monkeypatch)
    r = _post("ext-gate-model")
    assert r.status_code == 200, r.status_code
    assert r.get_data(as_text=True) == EXPECTED_EXT_SSE
    assert HITS == ["Bearer ek-1"]
    assert banned == []


def test_external_gate_inband_error_fails_over_without_cooldown(monkeypatch):
    """external 判负→换下一个 key：首 key 200+流内 error 帧，
    由第二个 key 正常服务；provider 级冷却零触发（审计红线）。"""
    _use_channel_providers()
    _reset({"Bearer ek-1": "gate_err", "Bearer ek-2": "ok"})
    banned = _spy_failover_bans(monkeypatch)
    r = _post("ext-gate-model")
    assert r.status_code == 200, r.status_code
    assert r.get_data(as_text=True) == EXPECTED_EXT_SSE
    assert HITS == ["Bearer ek-1", "Bearer ek-2"]
    assert RP._prov_quota_cooldown == {}      # provider 级冷却零触发
    assert banned == []


def test_external_all_keys_gate_bad_returns_503(monkeypatch):
    """全部 key 被判负：attempts 预算耗尽 → 既有 503 路径，
    provider 级冷却零触发。"""
    _use_channel_providers()
    _reset({"Bearer ek-1": "gate_err", "Bearer ek-2": "gate_err"})
    banned = _spy_failover_bans(monkeypatch)
    r = _post("ext-gate-model")
    assert r.status_code == 503
    assert r.get_json()["error"]["code"] == "external_credentials_unavailable"
    assert HITS == ["Bearer ek-1", "Bearer ek-2"]
    assert RP._prov_quota_cooldown == {}
    assert banned == []


def test_external_gate_off_passthrough_byte_identical(monkeypatch):
    """features.stream_gate=False：垃圾流逐字节原样透传
    （无 wrapper，与升级前完全一致），不换 key。"""
    _use_channel_providers(features={"stream_gate": False})
    _reset({"Bearer ek-1": "gate_err"})
    banned = _spy_failover_bans(monkeypatch)
    r = _post("ext-gate-model")
    assert r.status_code == 200
    assert r.get_data(as_text=True) == EXT_GATE_ERR_PASSTHROUGH
    assert HITS == ["Bearer ek-1"]       # 门关：不判负、不换 key
    assert RP._prov_quota_cooldown == {}
    assert banned == []


def test_external_gate_healthy_then_upstream_breaks(monkeypatch):
    """门判健康后 wrapper/上游才坏：已放行内容逐字节不丢，
    prefetched 交接后 live 续读失败走既有 _stream_body 的
    in-band 错误收尾（routes/proxy.py:621-627：proxy_stream_error
    帧 + [DONE]），与无门时逐字节一致；冷却零调用。"""
    _use_channel_providers()
    _reset({"Bearer ek-1": "gate_then_break"})
    banned = _spy_failover_bans(monkeypatch)
    r = _post("ext-gate-model")
    assert r.status_code == 200, r.status_code
    body = r.get_data(as_text=True)
    # 已放行的健康帧在最前（prefetched 交接，一个字节不丢）
    assert body.startswith(
        'data: {"model":"upstream-gate","choices":'
        '[{"delta":{"content":"he"}}]}\n\n'), body[:200]
    # 既有 in-band 错误收尾（routes/proxy.py:621-627）
    assert '"type": "proxy_stream_error"' in body, body[-300:]
    assert body.endswith("data: [DONE]\n\n")
    assert HITS == ["Bearer ek-1"]   # 门已判健康：不换 key
    assert RP._prov_quota_cooldown == {}
    assert banned == []


# ─── 门判定形状锁（每协议 1 正 1 负；勘察结论：三通道在门层
#     均为 OpenAI 形，_gate_classify 无需扩展）──────────────────

def test_gate_classify_openai_shape_positive_and_negative():
    """OpenAI 形（cline/external 原生、主池同款）：内容帧判健康、
    流内 error 帧判负、[DONE]/finish_reason 为终止信号、
    纯 usage 帧无信号。"""
    # 正：任一内容字段非空即健康（正文/reasoning/工具调用）
    assert RP._gate_classify(
        'data: {"choices":[{"delta":{"content":"he"}}]}') == "content"
    assert RP._gate_classify(
        'data: {"choices":[{"delta":{"reasoning_content":"想"}}]}') == "content"
    assert RP._gate_classify(
        'data: {"choices":[{"delta":{"reasoning":"想"}}]}') == "content"
    assert RP._gate_classify(
        'data: {"choices":[{"delta":{"tool_calls":['
        '{"index":0,"id":"c1","type":"function",'
        '"function":{"name":"f","arguments":"{}"}}]}}]}') == "content"
    assert RP._gate_classify(
        'data: {"choices":[{"delta":{"function_call":'
        '{"name":"f","arguments":"{}"}}}]}') == "content"
    # 负：流内 error 帧
    assert RP._gate_classify(
        'data: {"error":{"message":"boom","type":"server_error"}}') == "error"
    # 终止信号
    assert RP._gate_classify('data: [DONE]') == "terminator"
    assert RP._gate_classify(
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}') == "terminator"
    # 无信号（纯 usage 帧）
    assert RP._gate_classify(
        'data: {"choices":[{"delta":{}],'
        '"usage":{"prompt_tokens":1,"completion_tokens":0,"total_tokens":1}}'
        ) == "none"


def test_gate_classify_minicode_translated_shape_positive_and_negative():
    """minicode 上游原生 Anthropic 形，但门看到的是
    MiniCodeStreamWrap 翻译后的 OpenAI 形（勘察结论）：
    thinking/text/tool_use 的翻译结果都是内容（判健康），
    error 事件被翻译成 OpenAI 错误块（判负），message_start
    的纯 role 帧无信号。"""
    # 正：text_delta → delta.content
    assert RP._gate_classify(
        'data: {"choices":[{"delta":{"content":"he"}}]}') == "content"
    # 正：thinking_delta → delta.reasoning_content
    assert RP._gate_classify(
        'data: {"choices":[{"delta":{"reasoning_content":"想"}}]}') == "content"
    # 正：content_block_start(tool_use) → delta.tool_calls
    assert RP._gate_classify(
        'data: {"choices":[{"delta":{"tool_calls":['
        '{"index":0,"id":"toolu_1","type":"function",'
        '"function":{"name":"f","arguments":""}}]}}]}') == "content"
    # 正：input_json_delta → tool_calls[].function.arguments
    assert RP._gate_classify(
        'data: {"choices":[{"delta":{"tool_calls":['
        '{"index":0,"function":{"arguments":"{}"}}]}}]}') == "content"
    # 负：error 事件 → OpenAI 错误块
    assert RP._gate_classify(
        'data: {"error":{"message":"minicode upstream error",'
        '"type":"upstream_error"}}') == "error"
    # 无信号：message_start → 纯 role 帧
    assert RP._gate_classify(
        'data: {"choices":[{"delta":{"role":"assistant"}}]}') == "none"


# ─── 排队语义（方案 v2 §3.2）：/api/gate 与 _gate_waiters 契约 ──

def test_api_gate_reports_free_channel_state():
    """GET /api/gate 返回 free_channel 忙/排队状态（面板就绪，
    不新增前端卡片）。未设 admin_password 时不鉴权（check_admin_auth
    约定）。"""
    with cline._gate_cv:
        cline._gate_busy = True
        cline._gate_waiters = 2
    try:
        r = client.get("/api/gate")
        assert r.status_code == 200
        assert r.get_json() == {"free_channel": {"busy": True, "waiting": 2}}
    finally:
        with cline._gate_cv:
            cline._gate_busy = False
            cline._gate_waiters = 0
    r = client.get("/api/gate")
    assert r.get_json() == {"free_channel": {"busy": False, "waiting": 0}}


def test_gate_waiters_counter_tracks_wait_then_acquire():
    """_gate_waiters 计数契约：等待中 +1、拿到位 -1（锁内操作）。

    排队不计账号失败/冷却（wait_for_slot 的契约注释）——
    本用例只验证计数本身：排队时计数为 1，拿到位后归零且
    门被占位。
    """
    with cline._gate_cv:
        cline._gate_busy = True
    try:
        t = threading.Thread(target=cline.wait_for_slot,
                             args=("cline-free/gate-test",), daemon=True)
        t.start()
        deadline = time.time() + 5
        while cline._gate_waiters < 1 and time.time() < deadline:
            time.sleep(0.01)
        with cline._gate_cv:
            assert cline._gate_waiters == 1      # 等待中 +1
        with cline._gate_cv:                     # 放行
            cline._gate_busy = False
            cline._gate_cv.notify_all()
        t.join(10)
        assert not t.is_alive()
        with cline._gate_cv:
            assert cline._gate_waiters == 0      # 拿到位 -1
            assert cline._gate_busy is True      # 线程已占位
    finally:
        cline.mark_slot_done("cline-free/gate-test")
    with cline._gate_cv:
        assert cline._gate_busy is False
        assert cline._gate_waiters == 0
