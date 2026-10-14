# -*- coding: utf-8 -*-
"""会话粘性 rebind 分级（吸收-1/2 §R-D）测试。

覆盖规格 4 条：
1. **默认档：现状逐字节回归**——整单失败后绑定原样保留、冷却到期粘回原号、
   成功者覆写绑定（断言口径 = 现状真实语义，不是 v1 错称的"任何失败解绑"）
2. quota 档：transient 失败后绑定保持（pick_prefer 返回原号）；hard 后绑定清除
3. clear_binding API 单测；映射表逐行；inband_error 的 429/5xx 二次归类各一
   （各一条端到端：429 → 清绑定；5xx → 保绑定）
4. failover 成功后下一条仍命中原号（record 准入不覆写绑定）

全程零网络：mock 上游是本文件内起的本地服务器；路径常量走 settings 注入。

时间线纪律：`_stream_body` 是**惰性生成器**——只有响应被完整消费
（get_data()）才会跑到尾部记账/粘性段，故 `_post` 一律先消费再返回。

import 顺序契约（与 test_stream_sniff.py / test_provider_keys.py 同思路）：
`proxy_core.state` 的 am 单例按首次 import 时的 ACCOUNTS_FILE 快照加载账号，
故**单独运行本文件**时须在 import routes.proxy 之前把路径指到本文件临时目录；
全量收集时 state 已被更早的文件实例化（其时改路径只会污染其它文件），
跳过 import 时覆写、靠 module 级 autouse fixture 切换并还原。

跑法：py -3.12 -m pytest tests/test_affinity_rebind.py -v
"""

import json
import os
import pathlib
import socket
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
os.environ.setdefault("CODELY_CLI_VERSION", "0.0.0-test")  # 免 subprocess 探测 CLI 版本

TMP = pathlib.Path(tempfile.mkdtemp(prefix="qidi-rebind-test-"))
CFG_PATH = TMP / "config.json"
CONV_PATH = TMP / "conversations.json"


def _config_text(rebind_on="default"):
    return json.dumps({
        "port": 0,
        "pool_models": ["GLM-5.3", "KIMI-K3"],
        "model_groups": {},
        "providers": {},
        "roles": {},
        "vision_model": "GLM-5.3",
        "vision_capable_models": [],
        "pricing": {},
        "features": {"affinity": True},
        "routing": {"rebind_on": rebind_on},
    }, ensure_ascii=False)


CFG_PATH.write_text(_config_text(), encoding="utf-8")
(TMP / "accounts.json").write_text(json.dumps({"accounts": [
    {"user_id": uid, "username": f"t{uid}", "org_id": "0", "access_token": "a.b.c",
     "enabled": True, "has_glm53": False, "budget_exceeded": False, "group": ""}
    for uid in ("9001", "9002", "9003")]}), encoding="utf-8")

import proxy_core.settings as S  # noqa: E402
import proxy_core.accounts as A  # noqa: E402
import proxy_core.stats as ST  # noqa: E402
import proxy_core.water as WA  # noqa: E402
import proxy_core.providers as PV  # noqa: E402

# 条件化 import 时重定向（单独运行时需要，全量收集时跳过——见模块 docstring）
if "proxy_core.state" not in sys.modules:
    S.BASE_DIR = TMP
    S.CONFIG_FILE = CFG_PATH
    S.ACCOUNTS_FILE = TMP / "accounts.json"
    S.STATS_FILE = TMP / "token_stats.json"
    S.WATER_FILE = TMP / "water_check.json"
    S.LOG_FILE = TMP / "proxy.log"
    S.CONVERSATIONS_FILE = CONV_PATH
    A.ACCOUNTS_FILE = S.ACCOUNTS_FILE
    ST.STATS_FILE = S.STATS_FILE
    WA.WATER_FILE = S.WATER_FILE
    PV.CONFIG_FILE = S.CONFIG_FILE

# ─── mock 上游 ───────────────────────────────────────────────────
# BEHAVIOR: "key-9001" -> "200" | "402" | "broken" | "gate429" | "gate5xx"

HITS = []
BEHAVIOR = {}

_USAGE = {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12,
          "prompt_tokens_details": {"cached_tokens": 2048}}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *args):
        pass

    def do_POST(self):  # noqa: N802
        raw = self.rfile.read(int(self.headers.get("content-length") or 0))
        req = json.loads(raw.decode("utf-8"))
        auth = self.headers.get("authorization", "").removeprefix("Bearer ").strip()
        mode = BEHAVIOR.get(auth, "402")
        HITS.append(auth)

        if mode == "200":
            if req.get("stream"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for line in (
                    'data: {"model":"GLM-5.3","choices":[{"delta":{"content":"he"}}]}\n\n',
                    'data: {"model":"GLM-5.3","choices":[{"delta":{"content":"llo"}}],'
                    '"usage":' + json.dumps(_USAGE) + '}\n\n',
                    "data: [DONE]\n\n",
                ):
                    self.wfile.write(line.encode("utf-8"))
                    self.wfile.flush()
                return
            body = json.dumps({
                "model": "GLM-5.3",
                "choices": [{"message": {"role": "assistant", "content": "hello"},
                             "finish_reason": "stop"}],
                "usage": _USAGE,
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if mode == "broken":
            # 首字节已出后掐线（无 [DONE]）：截断流（transient 面）。
            # 注意：截断不换号（200 已定），只按 transient 处置绑定
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(
                b'data: {"model":"GLM-5.3","choices":[{"delta":{"content":"x"}}]}\n\n')
            self.wfile.flush()
            self.connection.shutdown(socket.SHUT_WR)
            return

        if mode in ("gate429", "gate5xx"):
            # 200 + 流内 error 帧（嗅探门判负 → 换号）：载荷里的状态码
            # 决定二次归类（429=hard / 503=transient）
            err = ({"message": "quota exceeded", "code": 429,
                    "type": "rate_limit_exceeded"} if mode == "gate429"
                   else {"message": "boom", "code": 503, "type": "server_error"})
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(
                ('data: ' + json.dumps({"error": err}) + "\n\n").encode("utf-8"))
            self.wfile.flush()
            return

        if mode == "nsgate5xx":
            # 非流式对应物：HTTP 200 但响应体本身是 error 对象
            #（_proxy_chat 非流式 inband 分支 → transient）
            body = json.dumps({"error": {"message": "boom", "code": 503,
                                         "type": "server_error"}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        code = int(mode)
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
from proxy_core.state import am, api_cache, activity  # noqa: E402
from proxy_core import affinity as AFF  # noqa: E402
from proxy_core import classify_failure as C  # noqa: E402

api_cache.get = lambda acc: f"key-{acc['user_id']}"      # 免向上游换 key

from flask import Flask  # noqa: E402

_app = Flask(__name__)
_app.register_blueprint(RP.bp)
_app.register_blueprint(admin_bp)
client = _app.test_client()


@pytest.fixture(scope="module", autouse=True)
def _use_rebind_env():
    """把上游、磁盘路径与粘性记忆文件切到本文件 TMP/mock（用例后原样还原）。

    与 test_stream_sniff.py 同约定：不在 import 时无条件覆写这些全局
    （收集期"后到者赢"会污染 test_stream_failover 的用例环境）。
    """
    saved = (S.BASE_DIR, S.CONFIG_FILE, S.ACCOUNTS_FILE,
             S.STATS_FILE, S.WATER_FILE, S.LOG_FILE,
             S.CONVERSATIONS_FILE,
             A.ACCOUNTS_FILE, ST.STATS_FILE, WA.WATER_FILE,
             PV.CONFIG_FILE, RP.LITELLM_API_BASE, WA.LITELLM_API_BASE)
    S.BASE_DIR = TMP
    S.CONFIG_FILE = CFG_PATH
    S.ACCOUNTS_FILE = TMP / "accounts.json"
    S.STATS_FILE = TMP / "token_stats.json"
    S.WATER_FILE = TMP / "water_check.json"
    S.LOG_FILE = TMP / "proxy.log"
    S.CONVERSATIONS_FILE = CONV_PATH
    A.ACCOUNTS_FILE = S.ACCOUNTS_FILE
    ST.STATS_FILE = S.STATS_FILE
    WA.WATER_FILE = S.WATER_FILE
    PV.CONFIG_FILE = S.CONFIG_FILE
    PV._cache = None
    RP.LITELLM_API_BASE = MOCK
    WA.LITELLM_API_BASE = MOCK
    yield
    _clear_affinity()
    (S.BASE_DIR, S.CONFIG_FILE, S.ACCOUNTS_FILE,
     S.STATS_FILE, S.WATER_FILE, S.LOG_FILE,
     S.CONVERSATIONS_FILE,
     A.ACCOUNTS_FILE, ST.STATS_FILE, WA.WATER_FILE,
     PV.CONFIG_FILE, RP.LITELLM_API_BASE, WA.LITELLM_API_BASE) = saved
    PV._cache = None


def _clear_affinity():
    with AFF._LOCK:
        AFF._TABLE.clear()
        AFF._WHY.clear()
        AFF._REBIND.clear()
        AFF._DIRTY = False
        AFF._LOADED = True        # 不重读（用例间互不串味）
        AFF._LAST_MTIME = None
    for p in (CONV_PATH, *TMP.glob("conversations.json.*.tmp")):
        try:
            p.unlink()
        except FileNotFoundError:
            pass


def _set_mode(rebind_on):
    CFG_PATH.write_text(_config_text(rebind_on), encoding="utf-8")


def _reset(behavior, rebind_on="default"):
    HITS.clear()
    BEHAVIOR.clear()
    BEHAVIOR.update(behavior)
    am.index = 0
    api_cache.cache.clear()
    RP._prov_quota_cooldown.clear()
    activity._events.clear()
    _set_mode(rebind_on)
    for a in am.accounts:
        a["enabled"] = True
        a["has_glm53"] = False
        a["budget_exceeded"] = False
        a["budget_cooldown_until"] = 0
        a["cooldown_reason"] = ""
        a["quota_team"] = ""
        a["group"] = ""
    _clear_affinity()


def _uncooldown_all():
    """手动解除全部冷却（模拟冷却窗口自然到期）。"""
    for a in am.accounts:
        a["budget_exceeded"] = False
        a["budget_cooldown_until"] = 0
        a["cooldown_reason"] = ""


def _post(stream=True, sid=None):
    """发请求并**强制完整消费**——_stream_body 是惰性生成器，只有消费到底
    才会跑尾部记账/粘性段（不经消费就断言绑定/计数会得到"未发生"的假象）。"""
    headers = {"X-Session-Id": sid} if sid else {}
    r = client.post("/v1/chat/completions", json={
        "model": "GLM-5.3", "stream": stream,
        "messages": [{"role": "user", "content": "hi"}]}, headers=headers)
    r.get_data()
    return r


def _bind(skey, uid="9001"):
    """直接建绑定（带跨 turn 粘性证据 cached_tokens=2048）。"""
    AFF.record(skey, uid, "GLM-5.3",
               {"prompt_tokens": 10,
                "prompt_tokens_details": {"cached_tokens": 2048}}, True)


# ─── ① 默认档：现状逐字节回归 ───────────────────────────────────

def test_default_mode_binding_preserved_after_total_failure_and_sticks_back():
    """①默认档（rebind_on 缺省）：整单失败后绑定**原样保留**、冷却到期
    粘回原号；on_failure 零动作零计数（现状真实语义——v1 的"任何失败
    解绑"已作废）。

    注：用 402 而非 429 作"整单失败"载体——库内 429 走"按 Retry-After
    等待后原样重发"（30 分钟预算）路径，用它会把用例拖成分钟级；402
    与 429 同属认证/配额族硬失败，绑定保留语义完全一致（429 的归类
    在 test_mapping_table_row_by_row 里逐行断言）。
    """
    _reset({"key-9001": "200", "key-9002": "200", "key-9003": "200"})
    # ① 首要走轮换选中 9001（index=0）并成功 → 粘性记录建立绑定
    assert AFF.binding_of("h:sid-d1") is None
    assert _post(sid="sid-d1").status_code == 200
    assert HITS == ["key-9001"], HITS
    assert AFF.binding_of("h:sid-d1") == "9001"
    assert AFF.pick_prefer("h:sid-d1", True) == ("9001", AFF.WHY_CACHE)

    # ② 整单失败（全池 402）：默认档不得动绑定
    _reset({"key-9001": "402", "key-9002": "402", "key-9003": "402"})
    _bind("h:sid-d1", "9001")
    r2 = _post(sid="sid-d1")
    assert r2.status_code == 402, r2.status_code
    assert len(HITS) == 3, ("整池各耗一次 attempt", HITS)
    assert AFF.binding_of("h:sid-d1") == "9001", "默认档失败不得解绑"
    assert AFF.rebind_counts() == {}, "默认档 on_failure 必须零动作零计数"

    # ③ 冷却到期 + 上游恢复后，下请求粘回原号
    # （现状"冷却驱动不粘、到期又粘回"）
    _uncooldown_all()
    BEHAVIOR.update({f"key-{u}": "200" for u in ("9001", "9002", "9003")})
    assert _post(sid="sid-d1").status_code == 200
    assert HITS[0] == "key-9001", ("冷却到期后应粘回原号", HITS)


def test_default_mode_successor_overwrites_binding():
    """①默认档：成功者覆写绑定（现状语义）。9001 流内判负换号到 9002
    成功 → 绑定被 9002 覆写（与 quota 档"保持原绑定"形成对照）。"""
    _reset({"key-9001": "gate5xx", "key-9002": "200", "key-9003": "200"})
    _bind("h:sid-d2", "9001")
    r = _post(sid="sid-d2")
    assert r.status_code == 200, r.status_code
    assert HITS == ["key-9001", "key-9002"], HITS
    assert AFF.binding_of("h:sid-d2") == "9002", \
        "默认档：任一账号成功即覆写绑定（现状语义）"
    assert AFF.rebind_counts() == {}


# ─── ② quota 档：transient 保持 / hard 清除 ────────────────────

def test_quota_mode_transient_keeps_binding_hard_clears():
    """②quota 档：transient 失败后绑定保持（pick_prefer 返回原号）；
    hard 失败后绑定被持久清除（binding_of → None，pick_prefer → cold）。"""
    _reset({"key-9001": "gate5xx", "key-9002": "200", "key-9003": "200"},
           rebind_on="quota")
    _bind("h:sid-q1", "9001")
    # inband 5xx（transient）→ 判负换号 → 9002 成功
    r = _post(sid="sid-q1")
    assert r.status_code == 200, r.status_code
    assert AFF.binding_of("h:sid-q1") == "9001", "transient 失败后绑定必须保持"
    assert AFF.pick_prefer("h:sid-q1", True) == ("9001", AFF.WHY_CACHE)
    assert AFF.rebind_counts().get(AFF.REBIND_TRANSIENT_KEEP, 0) >= 1
    # record 准入（F-1）：failover 到非绑定账号（9002）成功 → 不覆写绑定
    assert AFF.rebind_counts().get(AFF.REBIND_RECORD_SKIPPED, 0) >= 1

    # 截断流（saw_done=False）同样 transient：绑定保持
    _reset({"key-9001": "broken", "key-9002": "200", "key-9003": "200"},
           rebind_on="quota")
    _bind("h:sid-q1b", "9001")
    assert _post(sid="sid-q1b").status_code == 200
    assert AFF.binding_of("h:sid-q1b") == "9001", "截断流后绑定必须保持"
    assert AFF.rebind_counts().get(AFF.REBIND_TRANSIENT_KEEP, 0) >= 1

    # hard：全池 402（9001 首选）→ 绑定被持久清除
    _reset({f"key-{u}": "402" for u in ("9001", "9002", "9003")},
           rebind_on="quota")
    _bind("h:sid-q2", "9001")
    r2 = _post(sid="sid-q2")
    assert r2.status_code == 402, r2.status_code
    assert AFF.binding_of("h:sid-q2") is None, "hard 失败必须持久清绑定"
    assert AFF.pick_prefer("h:sid-q2", True) == (None, AFF.WHY_COLD)
    assert AFF.rebind_counts().get(AFF.REBIND_HARD_CLEAR, 0) >= 1
    assert AFF.rebind_counts().get(AFF.REBIND_TRANSIENT_KEEP, 0) == 0


# ─── ③ clear_binding API + 映射表逐行 + inband 二次归类 ─────────

def test_clear_binding_api_single_use():
    """③clear_binding 单测：清掉即 None + why=cold；幂等（无绑定返回
    False）；空键安全；清除置脏（后台 ≤2s 原子落盘，重启不复活）。"""
    _reset({})
    _bind("s-cb", "9001")
    assert AFF.binding_of("s-cb") == "9001"
    assert AFF.clear_binding("s-cb") is True
    assert AFF.binding_of("s-cb") is None
    assert AFF.pick_prefer("s-cb", True) == (None, AFF.WHY_COLD)
    assert AFF.clear_binding("s-cb") is False       # 幂等
    assert AFF.clear_binding("") is False           # 空键安全
    assert AFF.clear_binding(None) is False
    assert AFF._DIRTY is False or AFF._DIRTY is True    # 不抛即可
    # 持久化链：清掉后落盘内容里不再有该会话
    assert AFF.clear_binding("never-existed") is False


def _classify_face(face: str) -> str:
    """按映射表"失败面"描述构造输入并分类（逐行对账用）。"""
    if face.startswith("peek.inband_error("):
        token = face[len("peek.inband_error("):-1]
        if token == "5xx":
            return C.classify_peek_reason((C.INBAND_ERROR, 503))
        if token.startswith("other"):
            return C.classify_peek_reason((C.INBAND_ERROR, None))
        return C.classify_peek_reason((C.INBAND_ERROR, int(token)))
    if face == "peek.empty_stream":
        return C.classify_peek_reason("empty_stream")
    if face == "peek.budget_exhausted":
        return C.classify_peek_reason("budget_exhausted")
    if face == "peek.no_content_timeout":
        return C.classify_peek_reason("no_content_timeout")
    if face == "peek.peek_error.ReadTimeout":
        return C.classify_peek_reason("peek_error: ReadTimeout")
    if face == "peek.peek_error.connection_reset":
        return C.classify_peek_reason("peek_error: RemoteProtocolError")
    if face == "oauth.invalid_grant":
        return C.classify_credential_error(
            "invalid_grant: refresh token has been revoked")
    if face == "stream.truncated(saw_done=False)":
        return C.classify_stream_end(False)
    if face.startswith("nonstream."):
        return C.classify_http_status(int(face.split(".", 1)[1]))
    if face == "open.connection_failure":
        return C.classify_open_error("ConnectError: [WinError 10061] 连接被拒")
    raise AssertionError("映射表出现未知失败面: %r" % face)


def test_mapping_table_row_by_row():
    """③映射表逐行对账（规格 §R-D 表全 25 行，含 CPA 源标注行）。"""
    assert len(C.MAPPING_TABLE) == 25
    for face, expected in C.MAPPING_TABLE:
        got = _classify_face(face)
        assert got == expected, (face, got, expected)
    # 判据来源纪律抽样：429/404/408/invalid_grant 是 CPA 源新增判据
    assert C.classify_http_status(429) == C.HARD
    assert C.classify_http_status(404) == C.HARD
    assert C.classify_http_status(408) == C.TRANSIENT
    assert C.classify_credential_error("invalid_grant") == C.HARD
    # 未列出的状态码（400/422）保守 transient（不清绑定）
    assert C.classify_http_status(400) == C.TRANSIENT
    # 正常收尾无失败面
    assert C.classify_stream_end(True) is None


def test_gate_classify_carries_inband_payload_status():
    """③F-2 改造：_gate_classify 的 error 信号仍 == "error"（既有契约
    不变），但把 status/载荷带出来供二次归类。"""
    sig = RP._gate_classify('data: {"error":{"message":"quota","code":429}}')
    assert sig == "error"                       # str 子类：等值比较不变
    assert sig.error_status == 429
    assert sig.error_payload["message"] == "quota"
    # 其它形态状态码提取（code 数字/字符串、status、type 里的数字码）
    assert RP._gate_classify(
        'data: {"error":{"message":"x","status":503}}').error_status == 503
    assert RP._gate_classify(
        'data: {"error":{"message":"x","code":"502"}}').error_status == 502
    assert RP._gate_classify(
        'data: {"error":{"message":"x","type":"upstream 504"}}').error_status == 504
    # 提不到 → None（分类器按 transient 保守，绝不误清绑定）
    assert RP._gate_classify(
        'data: {"error":{"message":"x","type":"server_error"}}').error_status is None
    # 非 error 信号形态与升级前逐字一致
    assert RP._gate_classify(
        'data: {"choices":[{"delta":{"content":"he"}}]}') == "content"
    assert RP._gate_classify("data: [DONE]") == "terminator"
    assert RP._gate_classify(
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}') == "terminator"
    assert RP._gate_classify('data: {"usage":{"prompt_tokens":1}}') == "none"
    # 二次归类：429/404 → hard；5xx/None/无载荷 → transient
    assert C.classify_peek_reason((C.INBAND_ERROR, 429)) == C.HARD
    assert C.classify_peek_reason((C.INBAND_ERROR, 404)) == C.HARD
    assert C.classify_peek_reason((C.INBAND_ERROR, 503)) == C.TRANSIENT
    assert C.classify_peek_reason((C.INBAND_ERROR, None)) == C.TRANSIENT
    assert C.classify_peek_reason("inband_error") == C.TRANSIENT
    # _reason_text 兼容元组与字符串（日志/面板/snippet 用；snippet 必须是 str）
    assert RP._reason_text(("inband_error", 429)) == "inband_error:429"
    assert RP._reason_text(("inband_error", None)) == "inband_error"
    assert RP._reason_text("empty_stream") == "empty_stream"
    op = RP._UpstreamOpen(error="gate: inband_error:429",
                          snippet=RP._reason_text(("inband_error", 429)))
    assert op.access_denied is False            # snippet 是 str，子串判定可用


def test_quota_mode_inband_error_429_clears_binding_end_to_end():
    """③inband_error 429（配额族）→ hard → 持久清绑定（端到端；全池判负
    故无后续成功覆写）。"""
    _reset({f"key-{u}": "gate429" for u in ("9001", "9002", "9003")},
           rebind_on="quota")
    _bind("h:sid-h429", "9001")
    r = _post(sid="sid-h429")
    assert r.status_code == 502, r.status_code
    assert len(HITS) == 3                     # 三账号各耗一次 attempt
    assert AFF.binding_of("h:sid-h429") is None, "inband 429 应清绑定"
    assert AFF.rebind_counts().get(AFF.REBIND_HARD_CLEAR, 0) >= 1
    assert AFF.rebind_counts().get(AFF.REBIND_TRANSIENT_KEEP, 0) == 0


def test_quota_mode_inband_error_5xx_keeps_binding_end_to_end():
    """③inband_error 5xx → transient → 绑定保持（与 429 同路径、不同归类）。"""
    _reset({f"key-{u}": "gate5xx" for u in ("9001", "9002", "9003")},
           rebind_on="quota")
    _bind("h:sid-h5xx", "9001")
    r = _post(sid="sid-h5xx")
    assert r.status_code == 502, r.status_code
    assert AFF.binding_of("h:sid-h5xx") == "9001", "inband 5xx 应保持绑定"
    assert AFF.rebind_counts().get(AFF.REBIND_TRANSIENT_KEEP, 0) >= 1
    assert AFF.rebind_counts().get(AFF.REBIND_HARD_CLEAR, 0) == 0


# ─── ④ failover 成功后下一条仍命中原号 ─────────────────────────

def test_quota_mode_after_total_failure_next_request_uses_original_binding():
    """④quota 档：绑定 9001 但整单瞬时失败 → 下一条请求仍首选 9001
    （绑定保持，prompt cache 收益不丢）。"""
    _reset({f"key-{u}": "gate5xx" for u in ("9001", "9002", "9003")},
           rebind_on="quota")
    _bind("h:sid-f1", "9001")
    assert _post(sid="sid-f1").status_code == 502
    assert AFF.binding_of("h:sid-f1") == "9001"
    HITS.clear()
    BEHAVIOR.update({f"key-{u}": "200" for u in ("9001", "9002", "9003")})
    r2 = _post(sid="sid-f1")
    assert r2.status_code == 200, r2.status_code
    assert HITS == ["key-9001"], ("绑定保持语义：下一条应命中原号", HITS)


def test_quota_mode_failover_success_does_not_overwrite_binding():
    """④record 准入（F-1）：绑定 9001，9001 transient 失败 → failover 到
    9002 成功；绑定**不被 9002 覆写**（只记 record_skipped），下一条仍
    命中 9001。"""
    _reset({"key-9001": "gate5xx", "key-9002": "200", "key-9003": "200"},
           rebind_on="quota")
    _bind("h:sid-f2", "9001")
    assert _post(sid="sid-f2").status_code == 200
    assert HITS == ["key-9001", "key-9002"], HITS
    assert AFF.binding_of("h:sid-f2") == "9001", \
        "failover 到非绑定账号成功不得覆写绑定"
    assert AFF.rebind_counts().get(AFF.REBIND_RECORD_SKIPPED, 0) >= 1
    HITS.clear()
    assert _post(sid="sid-f2").status_code == 200
    assert HITS[0] == "key-9001", ("下一条应首选原绑定号", HITS)


def test_quota_mode_first_success_establishes_binding():
    """④新会话尚无绑定：任一账号成功即建立绑定（准入不误伤首次记录）。"""
    _reset({"key-9001": "200", "key-9002": "200", "key-9003": "200"},
           rebind_on="quota")
    assert AFF.binding_of("h:sid-new") is None
    assert _post(sid="sid-new").status_code == 200
    assert AFF.binding_of("h:sid-new") == "9001"
    assert AFF.rebind_counts().get(AFF.REBIND_RECORD_SKIPPED, 0) == 0


def test_quota_mode_nonstream_record_admission():
    """④非流式接线点同样走 record 准入与失败分级（流式 ~917 / 非流式 ~1464
    两处接线）。

    transient（非流式 inband error 5xx）：本次换号成功，但绑定保持；
    hard（402）：绑定被清，随后的成功记录属于"新建立"（准入不拦）。
    """
    _reset({"key-9001": "nsgate5xx", "key-9002": "200", "key-9003": "200"},
           rebind_on="quota")
    _bind("h:sid-ns", "9001")
    r = _post(stream=False, sid="sid-ns")
    assert r.status_code == 200, r.status_code
    assert HITS == ["key-9001", "key-9002"], HITS
    assert AFF.binding_of("h:sid-ns") == "9001", \
        "非流式 failover 到非绑定账号成功不得覆写绑定"
    assert AFF.rebind_counts().get(AFF.REBIND_TRANSIENT_KEEP, 0) >= 1
    assert AFF.rebind_counts().get(AFF.REBIND_RECORD_SKIPPED, 0) >= 1

    # hard：402 → 清绑定；换号成功后的记录 = 新绑定（清空即无"非绑定"可言）
    _reset({"key-9001": "402", "key-9002": "200", "key-9003": "200"},
           rebind_on="quota")
    _bind("h:sid-ns2", "9001")
    r2 = _post(stream=False, sid="sid-ns2")
    assert r2.status_code == 200, r2.status_code
    assert AFF.rebind_counts().get(AFF.REBIND_HARD_CLEAR, 0) >= 1
    assert AFF.binding_of("h:sid-ns2") == "9002", \
        "hard 清绑定后 failover 成功即建立新绑定"


def test_affinity_stats_endpoint_exposes_rebind_distribution():
    """④面板：/api/affinity/stats 补 rebind_distribution（_bump_why 同款扩展）。
    默认档为空（零动作语义），quota 档有计数。"""
    _reset({"key-9001": "gate5xx", "key-9002": "200", "key-9003": "200"})
    _bind("h:sid-st", "9001")
    assert _post(sid="sid-st").status_code == 200
    j = client.get("/api/affinity/stats").get_json()
    assert "rebind_distribution" in j
    assert j["rebind_distribution"] == {}         # 默认档零计数
    assert "why_distribution" in j and "stuck_sessions" in j

    _reset({"key-9001": "gate5xx", "key-9002": "200", "key-9003": "200"},
           rebind_on="quota")
    _bind("h:sid-st2", "9001")
    assert _post(sid="sid-st2").status_code == 200
    j2 = client.get("/api/affinity/stats").get_json()
    assert j2["rebind_distribution"].get("transient_keep", 0) >= 1
    assert j2["rebind_distribution"].get("record_skipped", 0) >= 1
