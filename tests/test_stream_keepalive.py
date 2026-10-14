# -*- coding: utf-8 -*-
"""SSE 心跳保活（吸收-1/2 §R-C）测试。

覆盖规格 4 条：
1. 真静默：首 chunk 后上游完全静默 ≥3×keepalive → ≥2 条心跳 + 随后正常收尾
2. keepalive_seconds=0 关闭分支不发心跳
3. 正常快流不心跳；心跳不混入数据帧（内容逐字节分离）
4. 两桥（/v1/messages、/v1/responses）注释行透传断言；GeneratorExit 清理
   （读线程退出无泄漏）

全程零网络：mock 上游是本文件内起的 SSE 服务器，按 base_url 路径后缀选行为。

跑法：py -3.12 -m pytest tests/test_stream_keepalive.py -v
"""

import json
import os
import pathlib
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
os.environ.setdefault("CODELY_CLI_VERSION", "0.0.0-test")  # 免 subprocess 探测 CLI 版本

TMP = pathlib.Path(tempfile.mkdtemp(prefix="qidi-keepalive-test-"))
CFG_PATH = TMP / "config.json"
CFG_PATH.write_text(json.dumps({"port": 0, "providers": {}}), encoding="utf-8")
(TMP / "accounts.json").write_text(json.dumps({"accounts": []}), encoding="utf-8")

# ⚠ 路径/上游地址全局**不在 import 时**覆写（约定见 test_stream_sniff.py:305
# 的长注释）：pytest 按文件名收集，import 阶段的覆写"后到者赢"——
# test_stream_failover.py 在 import 时把 RP.LITELLM_API_BASE 覆写为它自己的
# mock 且没有 autouse fixture 还原，本文件（文件名在它之后）若也在 import 时
# 覆写，会让它的用例全部打到本文件的 mock 上。切换只在下面的 fixture 内做。

import proxy_core.settings as S  # noqa: E402
import proxy_core.accounts as A  # noqa: E402
import proxy_core.stats as ST  # noqa: E402
import proxy_core.water as WA  # noqa: E402
import proxy_core.providers as PV  # noqa: E402

# ─── mock 上游：按 URL 路径后缀决定行为 ───────────────────────────
# /silent/chat/completions：首 content 块 → 静默 SILENT_S → [DONE]
# /fast/chat/completions  ：content 块 + [DONE] 立即
# /drip/chat/completions  ：每 DRIP_S 一块，长流（供 GeneratorExit 用）

SILENT_S = 1.2
DRIP_S = 0.04
DRIP_N = 400

FAST_FRAMES = [
    b'data: {"model":"ka-model","choices":[{"delta":{"content":"he"}}]}\n\n',
    b'data: {"model":"ka-model","choices":[{"delta":{"content":"llo"}}],'
    b'"usage":{"prompt_tokens":3,"completion_tokens":2,"total_tokens":5}}\n\n',
    b'data: {"model":"ka-model","choices":[{"finish_reason":"stop"}]}\n\n',
    b"data: [DONE]\n\n",
]
FAST_BODY = b"".join(FAST_FRAMES)

HITS = []


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *args):
        pass

    def do_POST(self):  # noqa: N802
        self.rfile.read(int(self.headers.get("content-length") or 0))
        HITS.append(self.path)
        mode = ("silent" if "/silent/" in self.path
                else "drip" if "/drip/" in self.path else "fast")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        try:
            if mode == "fast":
                self.wfile.write(FAST_BODY)
                self.wfile.flush()
                return
            if mode == "silent":
                # 首 chunk 即内容 → 嗅探门判健康
                self.wfile.write(
                    b'data: {"model":"ka-model","choices":[{"delta":{"content":"he"}}]}\n\n')
                self.wfile.flush()
                time.sleep(SILENT_S)          # 完全静默
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
                return
            # drip
            for _ in range(DRIP_N):
                self.wfile.write(
                    b'data: {"model":"ka-model","choices":[{"delta":{"content":"x"}}]}\n\n')
                self.wfile.flush()
                time.sleep(DRIP_S)
        except Exception:
            pass


_srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
_srv.daemon_threads = True
threading.Thread(target=_srv.serve_forever, daemon=True).start()
MOCK = f"http://127.0.0.1:{_srv.server_address[1]}"

import routes.proxy as RP  # noqa: E402
from routes.admin import bp as admin_bp  # noqa: E402
from proxy_core.state import activity  # noqa: E402

from flask import Flask  # noqa: E402

_app = Flask(__name__)
_app.register_blueprint(RP.bp)
_app.register_blueprint(admin_bp)
client = _app.test_client()


# ─── 工具 ────────────────────────────────────────────────────────

_mtime_seq = [time.time() + 2000.0]


def _write_config(providers, keepalive):
    """写 config.json（providers + requests.keepalive_seconds）并前推 mtime。"""
    cfg = {"port": 0, "pool_models": ["GLM-5.3"], "providers": providers,
           "requests": {"keepalive_seconds": keepalive}}
    CFG_PATH.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    _mtime_seq[0] += 1.0
    os.utime(CFG_PATH, (_mtime_seq[0], _mtime_seq[0]))
    PV._cache = None
    return cfg


def _provider(name, mode, model):
    return {name: {"base_url": f"{MOCK}/{mode}",
                   "api_keys": ["k-1"],
                   "models": [model]}}


@pytest.fixture(scope="module", autouse=True)
def _use_keepalive_env():
    """把配置/数据路径与上游地址切到本文件 TMP/mock（用例后原样还原）。

    与 test_stream_sniff.py 同约定：不在 import 时覆写这些全局（收集期
    "后到者赢"会污染 test_stream_failover），只在 fixture 内切换。
    """
    saved = (S.BASE_DIR, S.CONFIG_FILE, S.ACCOUNTS_FILE,
             S.STATS_FILE, S.WATER_FILE, S.LOG_FILE,
             A.ACCOUNTS_FILE, ST.STATS_FILE, WA.WATER_FILE,
             PV.CONFIG_FILE, RP.LITELLM_API_BASE, WA.LITELLM_API_BASE)
    S.BASE_DIR = TMP
    S.CONFIG_FILE = CFG_PATH
    S.ACCOUNTS_FILE = TMP / "accounts.json"
    S.STATS_FILE = TMP / "token_stats.json"
    S.WATER_FILE = TMP / "water_check.json"
    S.LOG_FILE = TMP / "proxy.log"
    A.ACCOUNTS_FILE = S.ACCOUNTS_FILE
    ST.STATS_FILE = S.STATS_FILE
    WA.WATER_FILE = S.WATER_FILE
    PV.CONFIG_FILE = S.CONFIG_FILE
    PV._cache = None
    RP.LITELLM_API_BASE = MOCK
    WA.LITELLM_API_BASE = MOCK
    yield
    PV.close_all_clients()
    (S.BASE_DIR, S.CONFIG_FILE, S.ACCOUNTS_FILE,
     S.STATS_FILE, S.WATER_FILE, S.LOG_FILE,
     A.ACCOUNTS_FILE, ST.STATS_FILE, WA.WATER_FILE,
     PV.CONFIG_FILE, RP.LITELLM_API_BASE, WA.LITELLM_API_BASE) = saved
    PV._cache = None


def _reset(providers, keepalive=0.25):
    HITS.clear()
    RP._prov_quota_cooldown.clear()
    activity._events.clear()
    PV.close_all_clients()
    return _write_config(providers, keepalive)


def _pump_threads():
    return [t for t in threading.enumerate() if t.name == "relay-stream-pump"]


def _post_chat(model, stream=True):
    return client.post("/v1/chat/completions", json={
        "model": model, "stream": stream,
        "messages": [{"role": "user", "content": "hi"}]})


# ─── ① 真静默 → 心跳 ─────────────────────────────────────────────

def test_true_silence_emits_heartbeats_then_finishes():
    """①首 chunk 后上游完全静默（1.2s ≫ 3×0.25s）：客户端收到 ≥2 条
    ': keepalive' 注释，随后流正常收尾（内容完整 + [DONE]）。"""
    ka = 0.25
    _reset(_provider("silp", "silent", "sil-m"), keepalive=ka)
    r = _post_chat("sil-m")
    assert r.status_code == 200, r.status_code
    body = r.get_data(as_text=True)
    n_ka = body.count(": keepalive")
    assert n_ka >= 2, (n_ka, body[:400])
    # 静默 ≥3×keepalive → 心跳条数应与静默时长量级相符
    assert n_ka >= int(SILENT_S / ka) - 1, (n_ka, SILENT_S, ka)
    # 静默之后仍能正常收尾：内容在线、终止符在位
    assert '"content":"he"' in body
    assert body.rstrip().endswith("data: [DONE]")
    # 心跳不逐条进 ActivityLog：按流聚合进该请求动态条目
    oks = [e for e in activity.list(50)
           if e.get("kind") == "ok" and e.get("keepalives")]
    assert oks and oks[0]["keepalives"] >= 2, activity.list(5)
    assert sum(1 for e in activity.list(200) if e.get("kind") == "ok"
               and e.get("keepalives")) == 1, "心跳必须聚合计数，不得逐条进日志"


# ─── ② keepalive=0 关闭 ─────────────────────────────────────────

def test_keepalive_disabled_emits_none():
    """②keepalive_seconds=0：关闭分支不发心跳，流行为与升级前一致。"""
    _reset(_provider("offp", "silent", "off-m"), keepalive=0)
    r = _post_chat("off-m")
    assert r.status_code == 200, r.status_code
    body = r.get_data(as_text=True)
    assert ": keepalive" not in body
    assert '"content":"he"' in body and body.rstrip().endswith("data: [DONE]")
    # 关闭分支不建读线程
    assert _pump_threads() == []
    oks = [e for e in activity.list(50) if e.get("kind") == "ok"]
    assert oks and oks[0]["keepalives"] == 0


# ─── ③ 快流不心跳 + 内容逐字节分离 ───────────────────────────────

def test_fast_stream_no_heartbeat_and_bytes_identical():
    """③正常快流不产生心跳；且心跳不存在时输出与上游逐字节一致
    （心跳绝不混入数据帧）。"""
    _reset(_provider("fastp", "fast", "fast-m"), keepalive=0.25)
    r = _post_chat("fast-m")
    assert r.status_code == 200, r.status_code
    body = r.get_data()                    # 原始字节（逐字节比对用）
    assert b": keepalive" not in body
    assert body == FAST_BODY, "无心跳时输出必须与上游逐字节一致"


# ─── ④ 双桥注释行透传 + GeneratorExit 清理 ──────────────────────

def _fake_proxy_with_keepalive():
    """假 _proxy_chat：吐 keepalive 注释 + 正常 data 帧（驱动真实桥 gen()）。"""
    chunks = [
        b": keepalive\n\n",
        b'data: {"choices":[{"delta":{"content":"\xe4\xbd\xa0"}}]}\n\n',
        b": keepalive\n\n",
        b'data: {"choices":[{"delta":{"content":"\xe5\xa5\xbd"}}],'
        b'"usage":{"prompt_tokens":2,"completion_tokens":5}}\n\n',
        b'data: [DONE]\n\n',
    ]

    def _fake(data, **kw):
        return RP.Response(iter(list(chunks)),
                           content_type="text/event-stream")
    return _fake


def test_anthropic_bridge_passes_keepalive_comments(monkeypatch):
    """④/v1/messages 桥：':' 开头注释行原样透传（原先只认 'data:' 前缀
    会把心跳整条吞掉 → 主力客户端（Claude Code）用不上心跳）。"""
    _reset({})
    monkeypatch.setattr(RP, "_proxy_chat", _fake_proxy_with_keepalive())
    r = client.post("/v1/messages", json={
        "model": "GLM-5.3", "stream": True, "max_tokens": 32,
        "messages": [{"role": "user", "content": "你好"}]})
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_data(as_text=True)
    assert body.count(": keepalive") == 2, body[:500]
    # 数据帧仍被正常桥成 Anthropic 事件（注释透传不污染桥）
    assert "event: message_start" in body
    assert '"type": "text_delta"' in body
    assert body.rstrip().endswith('data: {"type": "message_stop"}')


def test_responses_bridge_passes_keepalive_comments(monkeypatch):
    """④/v1/responses 桥：':' 开头注释行原样透传。"""
    _reset({})
    monkeypatch.setattr(RP, "_proxy_chat", _fake_proxy_with_keepalive())
    r = client.post("/v1/responses", json={
        "model": "GLM-5.3", "stream": True, "input": "你好"})
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_data(as_text=True)
    assert body.count(": keepalive") == 2, body[:500]
    assert '"type": "response.created"' in body or "response.created" in body
    assert body.rstrip().endswith("data: [DONE]")


def test_keepalive_reaches_client_through_anthropic_bridge_end_to_end():
    """④端到端：真静默上游 → _stream_body 发心跳 → /v1/messages 桥透传
    → 客户端（Claude Code 路径）真收到 ': keepalive'。"""
    _reset(_provider("silb", "silent", "silb-m"), keepalive=0.25)
    r = client.post("/v1/messages", json={
        "model": "silb-m", "stream": True, "max_tokens": 32,
        "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_data(as_text=True)
    assert body.count(": keepalive") >= 2, body[:400]
    assert "message_stop" in body


# ─── ④b GeneratorExit / 客户端断开 → 读线程退出 ─────────────────

class _DripResp:
    """_stream_body 需要的最小 resp 形状（iter_bytes + close）。"""

    def __init__(self, it):
        self._it = it
        self.closed = False

    def iter_bytes(self):
        return self._it

    def close(self):
        self.closed = True


def _drip_iter(n=DRIP_N, delay=DRIP_S):
    for _ in range(n):
        time.sleep(delay)
        yield b'data: {"model":"ka-model","choices":[{"delta":{"content":"x"}}]}\n\n'


def test_generator_exit_stops_reader_thread_without_leak():
    """④客户端断开（GeneratorExit）：读线程退出、resp 被关、无残留线程。
    ——"通知读线程退出（关 resp 触发读异常）"的清理路径。"""
    _reset(_provider("dripp", "drip", "drip-m"), keepalive=0.25)
    base = len(_pump_threads())
    opened = RP._UpstreamOpen(status=200, resp=_DripResp(_drip_iter()))
    gen = RP._stream_body(opened, "drip-m", "u-drip", time.perf_counter())
    first = next(gen)                      # 消费首块 → 读线程已在跑
    assert first.startswith(b"data:")
    assert len(_pump_threads()) == base + 1, "读线程应已启动"
    gen.close()                            # 模拟客户端断开 → GeneratorExit
    deadline = time.time() + 5
    while time.time() < deadline and len(_pump_threads()) > base:
        time.sleep(0.05)
    assert len(_pump_threads()) == base, "GeneratorExit 后读线程必须退出（无泄漏）"
    assert opened.resp.closed is True, "断开路径必须关 resp（触发读异常）"


def test_stream_error_from_reader_thread_reaches_existing_except_branch():
    """读线程内的异常经队列传回主生成器，走既有 except 分支收尾
    （in-band 错误事件 + [DONE]，不让单账号流内死亡炸成 500）。"""
    _reset(_provider("errp", "fast", "err-m"), keepalive=0.25)

    def _boom():
        yield b'data: {"choices":[{"delta":{"content":"a"}}]}\n\n'
        raise RuntimeError("socket reset by peer")

    opened = RP._UpstreamOpen(status=200, resp=_DripResp(_boom()))
    out = b"".join(RP._stream_body(opened, "err-m", "u-err",
                                   time.perf_counter())).decode("utf-8")
    assert '"content":"a"' in out
    assert "proxy_stream_error" in out
    assert out.rstrip().endswith("data: [DONE]")
    assert opened.resp.closed is True
    assert _pump_threads() == [], "异常收尾后读线程也必须退出"
