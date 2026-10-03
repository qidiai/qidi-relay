"""流内嗅探门（T2 / 升级方案 v2 §3.1）回归：全测试对本地 mock 服务器
发请求（127.0.0.1，零真实网络），覆盖 200 之后才暴露的上游故障——
流内 error 帧、空流、纯工具流、仅 usage 帧、非流式 error 体——
的判负换号，以及"判负绝不冷却账号"的审计红线。

运行：py -3.12 -m pytest tests/test_stream_sniff.py -v
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

TMP = pathlib.Path(tempfile.mkdtemp(prefix="qidi-relay-test-"))
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

# 路径重定向（条件化，见下方注释）
import proxy_core.settings as S  # noqa: E402
import proxy_core.accounts as A  # noqa: E402
import proxy_core.stats as ST  # noqa: E402
import proxy_core.water as WA  # noqa: E402
import proxy_core.providers as PV  # noqa: E402

# import 顺序契约（与 test_provider_keys.py 同思路）：
# proxy_core.state 的 am 单例在首次 import 时按 accounts.py
# 的 ACCOUNTS_FILE 快照加载账号——water/providers/accounts/
# stats/keystore/router/db 都不 import state（已逐一核实），
# 故可用 sys.modules 判定 state 是否已被实例化。
# · 全量收集时 state 已被 test_provider_keys.py 触发实例化
#   （am 已在内存，此时改路径对它无效、只会污染其它文件的
#   用例环境）→ 跳过 import 时覆写，靠下方 fixture 切换；
# · 单独运行本文件时 state 尚未实例化，必须在 import
#   routes.proxy 之前把路径指到本文件临时目录，am 才会
#   加载本文件的 9001-9003 账号。
if "proxy_core.state" not in sys.modules:
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

# ─── mock 上游：HITS 记录每次请求命中的账号 key ─────────────────────

HITS = []          # 每次请求命中的账号 key
MODELS = []        # 每次请求收到的请求体 model 字段（改写测试用）
BEHAVIOR = {}      # "key-9001" -> "200" | "gate_err" | "gate_empty" | ...


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *args):
        pass

    def do_POST(self):  # noqa: N802
        raw = self.rfile.read(int(self.headers.get("content-length") or 0))
        req = json.loads(raw.decode("utf-8"))    # 测试体本身是合法 JSON
        auth = self.headers.get("authorization", "").removeprefix("Bearer ").strip()
        mode = BEHAVIOR.get(auth, "402")
        HITS.append(auth)
        MODELS.append(req.get("model"))
        if mode == "200":
            if not req.get("stream"):
                # 非流式：普通 chat 完成响应（r.json() 可解析）
                body = json.dumps({
                    "model": "GLM-5.3",
                    "choices": [{"message": {"role": "assistant", "content": "hello"},
                                 "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 11, "completion_tokens": 2,
                              "total_tokens": 13},
                }).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for line in (
                'data: {"model":"GLM-5.3","choices":[{"delta":{"content":"he"}}]}\n\n',
                'data: {"model":"GLM-5.3","choices":[{"delta":{"content":"llo"}}],'
                '"usage":{"prompt_tokens":11,"completion_tokens":2,"total_tokens":13}}\n\n',
                "data: [DONE]\n\n",
            ):
                self.wfile.write(line.encode("utf-8"))
                self.wfile.flush()
            return
        if mode == "gate_err":
            # 200 之后流内才报错：首事件即 error 帧（网关层包装失败）
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b'data: {"error":{"message":"upstream exploded",'
                             b'"type":"server_error","code":"boom"}}\n\n')
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
        if mode == "gate_tool":
            # 纯工具调用流：仅 tool_calls delta、无正文——合法内容，必须放行
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for line in (
                'data: {"model":"GLM-5.3","choices":[{"delta":{"tool_calls":['
                '{"index":0,"id":"call_1","type":"function",'
                '"function":{"name":"get_weather","arguments":"{}"}}]}}]}\n\n',
                'data: {"model":"GLM-5.3","choices":[{"delta":{},'
                '"finish_reason":"tool_calls"}]}\n\n',
                "data: [DONE]\n\n",
            ):
                self.wfile.write(line.encode("utf-8"))
                self.wfile.flush()
            return
        if mode == "gate_usage":
            # 仅 usage 帧 + [DONE]：无内容事件（"usage 后直接 DONE"故障形态）
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for line in (
                'data: {"model":"GLM-5.3","choices":[{"delta":{},'
                '"finish_reason":"stop"}],"usage":{"prompt_tokens":11,'
                '"completion_tokens":0,"total_tokens":11}}\n\n',
                "data: [DONE]\n\n",
            ):
                self.wfile.write(line.encode("utf-8"))
                self.wfile.flush()
            return
        if mode == "gate_late":
            # 迟到内容：先发无内容 role delta，停顿超过（被
            # monkeypatch 缩小的）无内容超时阈值后，再发首内容
            # 帧——内容判定必须先于超时判定（R2 P2-1）
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b'data: {"model":"GLM-5.3","choices":'
                             b'[{"delta":{"role":"assistant"}}]}\n\n')
            self.wfile.flush()
            time.sleep(0.3)
            self.wfile.write(b'data: {"model":"GLM-5.3","choices":'
                             b'[{"delta":{"content":"he"}}]}\n\n')
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            return
        if mode == "gate_slowjunk":
            # 真超时：先发无内容 role delta，停顿超过阈值后再发
            # 一条仍无内容的 usage 帧 → 无内容超时判负换号
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b'data: {"model":"GLM-5.3","choices":'
                             b'[{"delta":{"role":"assistant"}}]}\n\n')
            self.wfile.flush()
            time.sleep(0.3)
            self.wfile.write(b'data: {"model":"GLM-5.3","choices":'
                             b'[{"delta":{}}],"usage":{"prompt_tokens":1,'
                             b'"completion_tokens":0,"total_tokens":1}}\n\n')
            self.wfile.flush()
            return
        if mode == "gate_budget":
            # 事件预算耗尽：连续 10 条无内容 data 行（纯 usage 帧，
            # 无 finish_reason），第 8 条触达 GATE_MAX_EVENTS 上限
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for _ in range(10):
                self.wfile.write(
                    b'data: {"model":"GLM-5.3","choices":[{"delta":{}}],'
                    b'"usage":{"prompt_tokens":1,"completion_tokens":0,'
                    b'"total_tokens":1}}\n\n')
                self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            return
        if mode == "gate_eof":
            # EOF 残余内容：唯一一条 data 行无尾随换行——内容塞在
            # 最后一个不带 \n 的行里（上游违规但内容真实，必须放行）
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b'data: {"model":"GLM-5.3",'
                             b'"choices":[{"delta":{"content":"hi"}}]}')
            self.wfile.flush()
            return
        if mode == "gate_b8":
            # 边界：前 7 条均为无内容事件，第 8 条（事件上限内
            # 最后一条）恰是首个内容帧 → 必须放行
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for _ in range(7):
                self.wfile.write(
                    b'data: {"model":"GLM-5.3","choices":[{"delta":{}}],'
                    b'"usage":{"prompt_tokens":1,"completion_tokens":0,'
                    b'"total_tokens":1}}\n\n')
                self.wfile.flush()
            for content in ("he", "llo"):
                self.wfile.write(
                    b'data: {"model":"GLM-5.3","choices":'
                    b'[{"delta":{"content":"' + content.encode() + b'"}}]}\n\n')
                self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            return
        if mode == "gate_raise":
            # peek 读流中途抛异常：Content-Length 谎报大于实发字节，
            # 发一条非内容帧后掐断写半侧 → httpx 迭代时抛
            # RemoteProtocolError（与 test_stream_failover 的
            # "truncated" 同机制；这里首 chunk 无内容，使异常
            # 发生在 peek 期间而非吐流期间）
            payload = (b'data: {"model":"GLM-5.3","choices":[{"delta":{}}],'
                       b'"usage":{"prompt_tokens":1,"completion_tokens":0,'
                       b'"total_tokens":1}}\n\n')
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(payload) + 500))
            self.end_headers()
            self.wfile.write(payload)
            self.wfile.flush()
            self.connection.shutdown(socket.SHUT_WR)
            return
        if mode == "nonstream_err":
            # 非流式 200 + error 体：网关层把失败包装成 200 + error 对象
            body = json.dumps({"error": {"message": "upstream exploded",
                                         "type": "server_error",
                                         "code": "boom"}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        # 未知模式按 "<状态码>" 数字兜底（本文件用例不触发）
        code = int(mode)
        body = json.dumps({"error": {"message": f"upstream rejects ({code})",
                                     "type": "invalid_request_error",
                                     "param": None, "code": str(code)}}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


# 上游 "200" 流式模式所发的逐字节内容。嗅探门判健康放行时，
# 客户端收到的必须是 prefetched + 续读 拼接后的全量——
# 与升级前透传结果逐字节一致（一个字节都不丢）。
EXPECTED_SSE = (
    'data: {"model":"GLM-5.3","choices":[{"delta":{"content":"he"}}]}\n\n'
    'data: {"model":"GLM-5.3","choices":[{"delta":{"content":"llo"}}],'
    '"usage":{"prompt_tokens":11,"completion_tokens":2,"total_tokens":13}}\n\n'
    "data: [DONE]\n\n"
)

_srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
_srv.daemon_threads = True
threading.Thread(target=_srv.serve_forever, daemon=True).start()
MOCK = f"http://127.0.0.1:{_srv.server_address[1]}"

import routes.proxy as RP  # noqa: E402
from proxy_core.state import am, api_cache  # noqa: E402
from proxy_core.keystore import session_pool  # noqa: E402

# 注意：不在 import 时把 RP.LITELLM_API_BASE 指向本文件的 mock——
# test_stream_failover.py 同样起了本地 mock 服务器并在 import 时
# 覆写该全局，pytest 按文件名收集时本模块后导入，会把上游
# 指向本文件服务器，令 failover 用例的请求打到空 BEHAVIOR
# （默认 402）而全挂。改为 autouse fixture：仅在本文件用例
# 前后切换并还原，两文件互不污染。
api_cache.get = lambda acc: f"key-{acc['user_id']}"      # 免向上游换 key

from flask import Flask  # noqa: E402

_app = Flask(__name__)
_app.register_blueprint(RP.bp)
client = _app.test_client()


@pytest.fixture(scope="module", autouse=True)
def _use_sniff_mock():
    """把上游与磁盘路径切到本文件的 mock/临时目录（用例后还原）。

    两处覆写都不放在 import 时：① pytest 按文件名收集，import
    阶段的覆写"后到者赢"——test_stream_failover.py 同样在
    import 时把 RP.LITELLM_API_BASE 覆写为它自己的 mock 服务器、
    把 S.CONFIG_FILE 指到它自己的临时目录（外部 provider 用例
    还会把 providers 段写进那个目录），本模块后导入会赢下该
    全局，令 failover 用例的请求打到空 BEHAVIOR 的服务器
    （默认 402）、load_config 读不到 providers 段（模型守卫
    404）；② 全量收集时 am 等单例已由 test_provider_keys.py
    实例化，import 时改路径对其内存状态无效。fixture 保证：
    本文件用例期间全局指向本文件 mock，用例结束还原成
    failover 的状态，两文件互不污染。
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


def _reset(behavior):
    HITS.clear()
    MODELS.clear()
    BEHAVIOR.clear()
    BEHAVIOR.update(behavior)
    am.index = 0
    api_cache.cache.clear()
    RP._prov_quota_cooldown.clear()   # provider 冷却是模块级状态，必须清
    for a in am.accounts:
        a["enabled"] = True
        a["budget_exceeded"] = False
        a["budget_cooldown_until"] = 0
        a["cooldown_reason"] = ""
        a["quota_team"] = ""
        a["group"] = ""


def _post(stream=True):
    return client.post("/v1/chat/completions", json={
        "model": "GLM-5.3", "stream": stream,
        "messages": [{"role": "user", "content": "hi"}]})


def _assert_no_cooldown():
    """审计红线断言：嗅探判负路径绝不动账号冷却表。"""
    for a in am.accounts:
        assert a["enabled"] is True
        assert a["budget_exceeded"] is False
        assert a["budget_cooldown_until"] == 0


# ─── 用例 ────────────────────────────────────────────────────────────

def test_gate_normal_stream_passes_prefetched_and_rest():
    """① 正常流：嗅探门放行，客户端收到 prefetched + 后续（无丢字节）。"""
    _reset({f"key-{u}": "200" for u in ("9001", "9002", "9003")})
    r = _post()
    assert r.status_code == 200, r.status_code
    # 逐字节等于上游所发：peek 预读的字节与续读的字节拼接后
    # 必须与升级前的透传结果完全一致
    assert r.get_data(as_text=True) == EXPECTED_SSE
    assert HITS == ["key-9001"]          # 健康首账号一次命中，无换号
    assert "text/event-stream" in r.headers["Content-Type"]


def test_gate_inband_error_frame_fails_over_without_cooldown(monkeypatch):
    """② 200 + 流内 error 帧：换号由第二账号应答，判负账号不进冷却。

    审计红线：判负只耗 attempt 预算——_penalize_account 与
    am.mark_cooldown 均未被调用。peek 只看得到流的最开头，
    慢而健康的账号可能被误判，拖进冷却会造成级联耗竭。
    """
    penalized = []
    orig_penalize = RP._penalize_account

    def _spy_penalize(uid, opened, model):
        penalized.append(uid)
        return orig_penalize(uid, opened, model)

    monkeypatch.setattr(RP, "_penalize_account", _spy_penalize)
    cooled = []
    orig_mark = am.mark_cooldown

    def _spy_mark(uid, secs, reason):
        cooled.append(uid)
        return orig_mark(uid, secs, reason)

    monkeypatch.setattr(am, "mark_cooldown", _spy_mark)

    _reset({"key-9001": "gate_err", "key-9002": "200", "key-9003": "200"})
    r = _post()
    assert r.status_code == 200, r.status_code
    body = r.get_data(as_text=True)
    assert '"content":"he"' in body and '"content":"llo"' in body
    assert "data: [DONE]" in body
    # 换号计数：首账号判负、第二账号应答成功
    assert HITS[0] == "key-9001" and len(HITS) == 2
    assert "key-9001" not in HITS[1:]
    # 审计红线：判负路径绝不动冷却表
    assert penalized == []
    assert cooled == []
    _assert_no_cooldown()


def test_gate_empty_stream_fails_over_without_cooldown():
    """③ 200 + 空流（[DONE] 无内容）：判 empty_stream 换号，不冷却。"""
    _reset({"key-9001": "gate_empty", "key-9002": "200", "key-9003": "200"})
    r = _post()
    assert r.status_code == 200, r.status_code
    assert '"content":"he"' in r.get_data(as_text=True)
    assert HITS[0] == "key-9001" and len(HITS) == 2
    assert "key-9001" not in HITS[1:]
    _assert_no_cooldown()


def test_gate_tool_call_only_stream_passes():
    """④ 200 + 纯工具调用流（仅 tool_calls delta）：合法内容，放行。

    tool_call 是合法回复——纯工具流若被判空流换号，等于把
    成功的工具调用全部作废重打。
    """
    _reset({f"key-{u}": "gate_tool" for u in ("9001", "9002", "9003")})
    r = _post()
    assert r.status_code == 200, r.status_code
    body = r.get_data(as_text=True)
    assert "tool_calls" in body and "get_weather" in body
    assert "data: [DONE]" in body
    assert HITS == ["key-9001"]          # 无换号：首账号即放行
    _assert_no_cooldown()


def test_gate_usage_only_frame_judged_empty():
    """⑤ 200 + 仅 usage 帧 + [DONE]（无内容）：判空换号。

    "usage 后直接 DONE" 是上游网关的典型假活形态——旧逻辑会
    让客户端拿到"成功空回复"，必须在这里判负换号。
    """
    _reset({"key-9001": "gate_usage", "key-9002": "200", "key-9003": "200"})
    r = _post()
    assert r.status_code == 200, r.status_code
    assert '"content":"he"' in r.get_data(as_text=True)
    assert HITS[0] == "key-9001" and len(HITS) == 2
    assert "key-9001" not in HITS[1:]
    _assert_no_cooldown()


def test_gate_all_attempts_bad_returns_502_no_cooldown():
    """⑥ 全部 attempt 判负：客户端收 502（如实失败），冷却表仍为空。"""
    _reset({f"key-{u}": "gate_err" for u in ("9001", "9002", "9003")})
    r = _post()
    assert r.status_code == 502, r.status_code
    err = r.get_json()["error"]
    assert isinstance(err, dict) and err["code"] == "upstream_502"
    assert len(HITS) == 3               # 每账号各耗一次 attempt
    _assert_no_cooldown()


def test_gate_disabled_skips_peek(monkeypatch):
    """⑦ features.stream_gate=False：不走 peek，行为与升级前一致。

    键缺省视为开启；显式关闭时 _peek_stream 一次都不被调用
    （计数=0），客户端照常拿到完整 SSE。
    """
    calls = []

    def _fake_peek(*args, **kwargs):
        calls.append(1)
        return ("bad", [], "should_not_happen")

    monkeypatch.setattr(RP, "_peek_stream", _fake_peek)
    monkeypatch.setattr(RP, "load_config",
                        lambda: {"features": {"stream_gate": False}})
    _reset({f"key-{u}": "200" for u in ("9001", "9002", "9003")})
    r = _post()
    assert r.status_code == 200, r.status_code
    assert r.get_data(as_text=True) == EXPECTED_SSE
    assert calls == []                  # peek 调用次数 = 0


def test_nonstream_inband_error_fails_over_without_cooldown(monkeypatch):
    """⑧ 非流式 200 + error 体：换号不冷却；全部判负 → 502。

    与流式门同一红线：只耗 attempt，绝不 _penalize_account。
    """
    penalized = []
    orig_penalize = RP._penalize_account

    def _spy_penalize(uid, opened, model):
        penalized.append(uid)
        return orig_penalize(uid, opened, model)

    monkeypatch.setattr(RP, "_penalize_account", _spy_penalize)

    # 单账号判负 → 换号由第二账号应答
    _reset({"key-9001": "nonstream_err", "key-9002": "200",
            "key-9003": "200"})
    r = _post(stream=False)
    assert r.status_code == 200, r.status_code
    assert r.get_json()["choices"][0]["message"]["content"] == "hello"
    assert HITS[0] == "key-9001" and len(HITS) == 2
    assert "key-9001" not in HITS[1:]
    assert penalized == []
    _assert_no_cooldown()

    # 全部 attempt 判负 → 502 收尾，冷却表仍为空
    _reset({f"key-{u}": "nonstream_err" for u in ("9001", "9002", "9003")})
    r = _post(stream=False)
    assert r.status_code == 502, r.status_code
    assert r.get_json()["error"]["code"] == "upstream_502"
    assert len(HITS) == 3
    assert penalized == []
    _assert_no_cooldown()


def test_stream_body_prefetched_none_byte_identical(monkeypatch):
    """⑨ prefetched=None（门关闭）时 _stream_body 输出逐字节与升级前一致。

    现有 89 条回归测试（test_stream_failover.py 等）在本路径上
    运行兜底；本用例再显式断言客户端收到的字节序列与上游所发
    完全相等，双保险。
    """
    monkeypatch.setattr(RP, "load_config",
                        lambda: {"features": {"stream_gate": False}})
    _reset({f"key-{u}": "200" for u in ("9001", "9002", "9003")})
    r = _post()
    assert r.status_code == 200, r.status_code
    assert r.get_data(as_text=True) == EXPECTED_SSE


# ─── R2 返工新增用例 ──────────────────────────────────────────

def test_gate_late_content_after_deadline_passes(monkeypatch):
    """① 迟到内容放行：deadline 后到达的首内容帧不被误判超时。

    超时判定移到内容解析之后（双审共识 P2-1）：本用例把
    GATE_NO_EVENT_TIMEOUT 缩到 0.2s，上游先发无内容 role
    delta、停 0.3s 再发首内容帧——内容帧到达时已过 deadline，
    旧逻辑（先判超时后见内容）会误判 no_content_timeout。
    """
    monkeypatch.setattr(RP, "GATE_NO_EVENT_TIMEOUT", 0.2)
    _reset({"key-9001": "gate_late", "key-9002": "200",
            "key-9003": "200"})
    r = _post()
    assert r.status_code == 200, r.status_code
    body = r.get_data(as_text=True)
    assert '"content":"he"' in body
    assert "data: [DONE]" in body
    assert HITS == ["key-9001"]          # 首账号放行，无换号
    _assert_no_cooldown()


def test_gate_no_content_timeout_fails_over(monkeypatch):
    """② 真超时判负：有字节流动但持续无内容事件 → 换号不冷却。

    deadline 语义固定（首 chunk 时刻 + 阈值，不滚动）——
    本用例缩阈值到 0.2s，role delta 后停 0.3s 再发一条
    仍无内容的 usage 帧 → no_content_timeout 判负换号。
    """
    monkeypatch.setattr(RP, "GATE_NO_EVENT_TIMEOUT", 0.2)
    _reset({"key-9001": "gate_slowjunk", "key-9002": "200",
            "key-9003": "200"})
    r = _post()
    assert r.status_code == 200, r.status_code
    assert '"content":"he"' in r.get_data(as_text=True)
    assert HITS[0] == "key-9001" and len(HITS) == 2
    assert "key-9001" not in HITS[1:]
    _assert_no_cooldown()


def test_gate_budget_exhausted_fails_over_without_cooldown():
    """③b 事件预算耗尽：≥8 条非内容 data 行 → 判负换号不冷却。"""
    _reset({"key-9001": "gate_budget", "key-9002": "200",
            "key-9003": "200"})
    r = _post()
    assert r.status_code == 200, r.status_code
    assert '"content":"he"' in r.get_data(as_text=True)
    assert HITS[0] == "key-9001" and len(HITS) == 2
    assert "key-9001" not in HITS[1:]
    _assert_no_cooldown()


def test_gate_eof_residual_content_passes():
    """④ EOF 残余内容：无尾随换行、内容在最后一行里 → 放行。

    上游违规流（最后一行不带换行）可能把真实内容塞在这里，
    误判成空流会冤枉健康账号——与 _stream_body 的
    末尾 line_buf 冲洗同风格。
    """
    _reset({f"key-{u}": "gate_eof" for u in ("9001", "9002", "9003")})
    r = _post()
    assert r.status_code == 200, r.status_code
    body = r.get_data(as_text=True)
    assert '"content":"hi"' in body
    assert "data: [DONE]" not in body   # 上游确实没吐终止符
    assert HITS == ["key-9001"]          # 无换号：首账号即放行
    _assert_no_cooldown()


def test_gate_8th_event_is_first_content_passes():
    """⑤ 边界：第 8 条事件恰是首个内容帧 → 放行。

    前 7 条无内容事件 + 第 8 条（GATE_MAX_EVENTS 上限内
    最后一条）内容帧——事件预算判定不得抢在内容判定之前。
    """
    _reset({f"key-{u}": "gate_b8" for u in ("9001", "9002", "9003")})
    r = _post()
    assert r.status_code == 200, r.status_code
    body = r.get_data(as_text=True)
    assert '"content":"he"' in body and '"content":"llo"' in body
    assert "data: [DONE]" in body
    assert HITS == ["key-9001"]
    _assert_no_cooldown()


def test_gate_read_exception_fails_over_without_cooldown():
    """⑥ peek 读流异常：iter_bytes 中途抛错 → 判负换号不冷却。

    Content-Length 谎报 + 掐断写半侧 → httpx 在 peek 迭代
    时抛 RemoteProtocolError，映射为 no_content_timeout
    （reason 枚举四值之一），不让单账号流内死亡把请求拖成 500。
    """
    _reset({"key-9001": "gate_raise", "key-9002": "200",
            "key-9003": "200"})
    r = _post()
    assert r.status_code == 200, r.status_code
    assert '"content":"he"' in r.get_data(as_text=True)
    assert HITS[0] == "key-9001" and len(HITS) == 2
    assert "key-9001" not in HITS[1:]
    _assert_no_cooldown()


def test_gate_paths_release_session_no_inflight_leak():
    """⑦ session 释放守护：判负/成功路径后 in-flight 归零。

    防 keystore._pools 无界增长——判负分支的
    session_pool.release(sess) 必须执行（上游 200 已开、
    sess 已 acquire，判负时不归还就是泄漏）。
    """
    # 判负路径：全部 attempt 判负 → 502，每个 sess 都已 release
    _reset({f"key-{u}": "gate_err" for u in ("9001", "9002", "9003")})
    r = _post()
    assert r.status_code == 502, r.status_code
    stats = session_pool.stats()
    assert stats, "session_pool 应有本用例 key 的记录"
    assert all(v["active"] == 0 for v in stats.values()), stats

    # 成功路径：门放行的正常流，_stream_body 的 finally release
    _reset({f"key-{u}": "200" for u in ("9001", "9002", "9003")})
    r = _post()
    assert r.status_code == 200, r.status_code
    r.get_data()
    stats = session_pool.stats()
    assert all(v["active"] == 0 for v in stats.values()), stats
