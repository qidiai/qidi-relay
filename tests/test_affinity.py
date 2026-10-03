"""会话粘性（阶段 B / 方案 v2 §4，v1 仅主池）测试。

全程打本地 mock 上游，零网络、零真实数据文件：
路径常量（含 conversations.json）注入 TMP——
方案 §7.7 纪律：新模块一律走 settings.py 路径
常量模式，禁止 seedguard 式 BASE_DIR 兜底。

跑法：py -3.12 -m pytest tests/test_affinity.py -v
"""

import hashlib
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
os.environ.setdefault("CODELY_CLI_VERSION", "0.0.0-test")  # 免 subprocess 探测 CLI 版本

TMP = pathlib.Path(tempfile.mkdtemp(prefix="qidi-affinity-test-"))
(TMP / "config.json").write_text(json.dumps({
    "port": 0,
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

# 路径常量必须在实例化单例（proxy_core.state）之前替换掉，否则会写进真实数据文件
import proxy_core.settings as S  # noqa: E402

S.BASE_DIR = TMP
S.CONFIG_FILE = TMP / "config.json"
S.ACCOUNTS_FILE = TMP / "accounts.json"
S.STATS_FILE = TMP / "token_stats.json"
S.WATER_FILE = TMP / "water_check.json"
S.LOG_FILE = TMP / "proxy.log"
S.CONVERSATIONS_FILE = TMP / "conversations.json"   # 会话粘性记忆（本文件专属）

import proxy_core.accounts as A  # noqa: E402
import proxy_core.stats as ST  # noqa: E402
import proxy_core.water as WA  # noqa: E402
import proxy_core.providers as PV  # noqa: E402

A.ACCOUNTS_FILE = S.ACCOUNTS_FILE
ST.STATS_FILE = S.STATS_FILE
WA.WATER_FILE = S.WATER_FILE
PV.CONFIG_FILE = S.CONFIG_FILE

# ─── mock 上游：HITS 记录每次请求的 Authorization ────────────────

HITS = []          # 依次记录被命中的账号 key
BEHAVIOR = {}      # "key-9001" -> "200" | "<状态码>"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *args):
        pass

    def do_POST(self):  # noqa: N802
        raw = self.rfile.read(int(self.headers.get("content-length") or 0))
        req = json.loads(raw.decode("utf-8"))    # 请求体必须仍是合法 JSON
        auth = self.headers.get("authorization", "").removeprefix("Bearer ").strip()
        mode = BEHAVIOR.get(auth, "402")
        HITS.append(auth)
        if mode == "200":
            # usage 带 prompt_tokens_details.cached_tokens=2048——
            # 跨 turn 粘性证据（≥1024 且 <5min 即粘，方案 §4 B-3）
            usage = {"prompt_tokens": 10, "completion_tokens": 2,
                     "total_tokens": 12,
                     "prompt_tokens_details": {"cached_tokens": 2048}}
            if not req.get("stream"):
                # 非流式：普通 chat 完成响应（SSE 文本会让 r.json() 解析失败）
                body = json.dumps({
                    "model": "GLM-5.3",
                    "choices": [{"message": {"role": "assistant", "content": "hello"},
                                 "finish_reason": "stop"}],
                    "usage": usage,
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
                '"usage":' + json.dumps(usage) + '}\n\n',
                "data: [DONE]\n\n",
            ):
                self.wfile.write(line.encode("utf-8"))
                self.wfile.flush()
            return
        if mode == "broken":
            # 首字节已出后掐线（无 [DONE] 终止符）：
            # 模拟上游截断（httpx 只看到正常 EOF，
            # 不抛异常——走 _stream_body 的 saw_done
            # 判定路径，F5 用例靠它）
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(
                b'data: {"model":"GLM-5.3","choices":'
                b'[{"delta":{"content":"x"}}]}\n\n')
            self.wfile.flush()
            self.connection.shutdown(socket.SHUT_WR)
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

RP.LITELLM_API_BASE = MOCK
WA.LITELLM_API_BASE = MOCK
api_cache.get = lambda acc: f"key-{acc['user_id']}"      # 免向上游换 key

from flask import Flask  # noqa: E402

_app = Flask(__name__)
_app.register_blueprint(RP.bp)
_app.register_blueprint(admin_bp)
client = _app.test_client()


@pytest.fixture(scope="module", autouse=True)
def _use_affinity_env():
    """把上游与磁盘路径切到本文件的 mock/临时目录（用例后还原）。

    与 test_channel_gate.py 的 fixture 同构：pytest 全量收集时
    import 阶段的覆写"先到者赢"，本 fixture 保证无论收集顺序
    如何，本文件用例期间全局指向本文件 mock，用例结束还原，
    互不污染。conversations.json 亦在列——粘性记忆绝不写
    仓库根数据文件。
    """
    saved = (S.BASE_DIR, S.CONFIG_FILE, S.ACCOUNTS_FILE,
             S.STATS_FILE, S.WATER_FILE, S.LOG_FILE,
             S.CONVERSATIONS_FILE,
             A.ACCOUNTS_FILE, ST.STATS_FILE, WA.WATER_FILE,
             PV.CONFIG_FILE, RP.LITELLM_API_BASE, WA.LITELLM_API_BASE)
    S.BASE_DIR = TMP
    S.CONFIG_FILE = TMP / "config.json"
    S.ACCOUNTS_FILE = TMP / "accounts.json"
    S.STATS_FILE = TMP / "token_stats.json"
    S.WATER_FILE = TMP / "water_check.json"
    S.LOG_FILE = TMP / "proxy.log"
    S.CONVERSATIONS_FILE = TMP / "conversations.json"
    A.ACCOUNTS_FILE = S.ACCOUNTS_FILE
    ST.STATS_FILE = S.STATS_FILE
    WA.WATER_FILE = S.WATER_FILE
    PV.CONFIG_FILE = S.CONFIG_FILE
    PV._cache = None          # providers 配置热重载：切目录即清缓存
    RP.LITELLM_API_BASE = MOCK
    WA.LITELLM_API_BASE = MOCK
    yield
    # 先清粘性内存态（清 _DIRTY——后台扫尾线程在路径还原后
    # 绝不落盘），再还原路径常量
    with AFF._LOCK:
        AFF._TABLE.clear()
        AFF._WHY.clear()
        AFF._DIRTY = False
        AFF._LOADED = False
        AFF._LAST_MTIME = None
    for p in (TMP / "conversations.json",
              *TMP.glob("conversations.json.*.tmp")):
        try:
            p.unlink()
        except FileNotFoundError:
            pass
    (S.BASE_DIR, S.CONFIG_FILE, S.ACCOUNTS_FILE,
     S.STATS_FILE, S.WATER_FILE, S.LOG_FILE,
     S.CONVERSATIONS_FILE,
     A.ACCOUNTS_FILE, ST.STATS_FILE, WA.WATER_FILE,
     PV.CONFIG_FILE, RP.LITELLM_API_BASE, WA.LITELLM_API_BASE) = saved
    PV._cache = None


def _reset(behavior):
    HITS.clear()
    BEHAVIOR.clear()
    BEHAVIOR.update(behavior)
    am.index = 0
    api_cache.cache.clear()
    RP._prov_quota_cooldown.clear()   # provider 冷却是模块级状态，必须清
    activity._events.clear()
    for a in am.accounts:
        a["enabled"] = True
        a["has_glm53"] = False
        a["budget_exceeded"] = False
        a["budget_cooldown_until"] = 0
        a["cooldown_reason"] = ""
        a["quota_team"] = ""     # team 学习结果：不清会污染后续用例的命中次数
        a["group"] = ""          # 分组用例会临时改组，必须还原
    # 粘性内存态清零（_LOADED 保持 True：不重读，用例间互不串味；
    # 磁盘文件由本函数一并清掉——后台线程无脏数据可写）
    with AFF._LOCK:
        AFF._TABLE.clear()
        AFF._WHY.clear()
        AFF._DIRTY = False
        AFF._LAST_MTIME = None
    for p in (TMP / "conversations.json",
              *TMP.glob("conversations.json.*.tmp")):
        try:
            p.unlink()
        except FileNotFoundError:
            pass


def _post(stream=False, headers=None, messages=None):
    return client.post("/v1/chat/completions", json={
        "model": "GLM-5.3", "stream": stream,
        "messages": messages or [{"role": "user", "content": "hi"}],
    }, headers=headers or {})


# ─── 用例 ────────────────────────────────────────────────────────────

def test_same_session_sticks_to_same_account():
    """①同一会话连发落同账号：首请求轮换选 9001 并记录
    （mock 回 cached_tokens=2048），次请求跨 turn 粘性生效。"""
    _reset({"key-9001": "200", "key-9002": "200", "key-9003": "200"})
    r1 = _post(headers={"X-Session-Id": "sid-1"})
    assert r1.status_code == 200, r1.status_code
    r2 = _post(headers={"X-Session-Id": "sid-1"})
    assert r2.status_code == 200, r2.status_code
    assert HITS == ["key-9001", "key-9001"], HITS


def test_cooled_account_drifts_with_why_resting():
    """②账号冷却后漂移：9001 冷却中 → why=resting 不粘，
    轮换落到 9003（index=1，候选 [9002,9003]），并留痕
    漂移事件（status="affinity"）。"""
    _reset({"key-9001": "200", "key-9002": "200", "key-9003": "200"})
    assert _post(headers={"X-Session-Id": "sid-2"}).status_code == 200
    am.mark_cooldown("9001", 3600, "上游拒绝凭证")
    r2 = _post(headers={"X-Session-Id": "sid-2"})
    assert r2.status_code == 200, r2.status_code
    assert HITS == ["key-9001", "key-9003"], HITS
    assert AFF._WHY.get("resting", 0) >= 1, AFF._WHY
    assert any(e.get("status") == "affinity" and "resting" in e.get("message", "")
               for e in activity.list(50)), "漂移应留痕 info 事件"


def test_spent_and_disabled_accounts_do_not_stick():
    """402 配额耗尽 → why=spent；账号禁用 → why=disabled：
    均不粘（误判慢账号不能把它冷却掉，这里只是不选它）。"""
    _reset({})
    AFF.record("s-spent", "9001", "GLM-5.3",
               {"prompt_tokens": 10,
                "prompt_tokens_details": {"cached_tokens": 2048}}, True)
    am.mark_budget_402("9001")
    assert AFF.pick_prefer("s-spent", True) == (None, AFF.WHY_SPENT)
    AFF.record("s-dis", "9002", "GLM-5.3",
               {"prompt_tokens": 10,
                "prompt_tokens_details": {"cached_tokens": 2048}}, True)
    am.accounts[1]["enabled"] = False
    assert AFF.pick_prefer("s-dis", True) == (None, AFF.WHY_DISABLED)


def test_cross_turn_sticks_with_warm_cache():
    """③跨 turn 粘性：last cache_read≥1024 且距今<300s → 粘；
    299s 仍在窗内，301s 出窗 → cold。"""
    _reset({})
    usage = {"prompt_tokens": 100,
             "prompt_tokens_details": {"cached_tokens": 2048}}
    AFF.record("s-warm", "9001", "GLM-5.3", usage, True)
    assert AFF.pick_prefer("s-warm", True) == ("9001", AFF.WHY_CACHE)
    with AFF._LOCK:
        AFF._TABLE["s-warm"]["at"] = time.time() - 299
    assert AFF.pick_prefer("s-warm", True) == ("9001", AFF.WHY_CACHE)
    with AFF._LOCK:
        AFF._TABLE["s-warm"]["at"] = time.time() - 301
    assert AFF.pick_prefer("s-warm", True) == (None, AFF.WHY_COLD)


def test_within_turn_sticks_without_cache_evidence():
    """turn 内（工具循环续请求）必粘——无需 cache 证据。"""
    _reset({})
    AFF.record("s-turn", "9001", "GLM-5.3", {"prompt_tokens": 10}, False)
    assert AFF.pick_prefer("s-turn", False) == ("9001", AFF.WHY_TURN)


def test_missing_cache_read_is_no_data():
    """④cache_read 缺失 → why=no-data 不粘（§4 B-3）；
    cache_read<1024 → no-cache 不粘；Anthropic 形
    cache_read_input_tokens 亦可提取。"""
    _reset({})
    AFF.record("s-nodata", "9001", "GLM-5.3",
               {"prompt_tokens": 10, "completion_tokens": 2,
                "total_tokens": 12}, True)
    assert AFF.pick_prefer("s-nodata", True) == (None, AFF.WHY_NO_DATA)
    AFF.record("s-nocache", "9001", "GLM-5.3",
               {"prompt_tokens": 10,
                "prompt_tokens_details": {"cached_tokens": 512}}, True)
    assert AFF.pick_prefer("s-nocache", True) == (None, AFF.WHY_NO_CACHE)
    AFF.record("s-anth", "9001", "GLM-5.3",
               {"cache_read_input_tokens": 4096}, True)
    assert AFF.pick_prefer("s-anth", True) == ("9001", AFF.WHY_CACHE)


def test_session_key_four_level_fallback():
    """⑤会话键四级 fallback 各一例：chat["user"] /
    X-Session-Id 头 / 首条 user 消息哈希 / key_label /
    remote_addr 兜底；system 永不参与（_proxy_chat 把它
    替换成固定串，跨会话恒定、无区分度）。"""
    _reset({})
    # ① chat["user"]（Anthropic metadata.user_id 已被
    #    proxy_core/anthropic.py:163-165 译入）
    assert AFF.session_key({"user": "u-1"}, None, None, None) == "u:u-1"
    # ② 请求头 X-Session-Id
    assert AFF.session_key({}, {"X-Session-Id": "sid-1"},
                           None, None) == "h:sid-1"
    # ③ 首条 user 消息前 512B sha256[:16]
    k1 = AFF.session_key(
        {"messages": [{"role": "user", "content": "hello world"}]},
        None, None, None)
    expect = "m:" + hashlib.sha256(b"hello world").hexdigest()[:16]
    assert k1 == expect
    # 稳定性 + 内容敏感性
    k2 = AFF.session_key(
        {"messages": [{"role": "user", "content": "hello world"}]},
        None, None, None)
    assert k2 == k1
    k3 = AFF.session_key(
        {"messages": [{"role": "user", "content": "hello worle"}]},
        None, None, None)
    assert k3 != k1
    # system/assistant 消息不产生会话键（无 user 消息 → 落到兜底）
    assert AFF.session_key(
        {"messages": [{"role": "system", "content": "sys"},
                      {"role": "assistant", "content": "a"}]},
        None, "wk-a", "127.0.0.1") == "k:wk-a"
    # 多模态 content 块列表：序列化后取哈希
    k5 = AFF.session_key(
        {"messages": [{"role": "user",
                       "content": [{"type": "text", "text": "hi"}]}]},
        None, None, None)
    assert k5.startswith("m:") and len(k5) == 18
    # ④ 兜底
    assert AFF.session_key({}, None, "wk-a", None) == "k:wk-a"
    assert AFF.session_key({}, None, None, "127.0.0.1") == "r:127.0.0.1"
    assert AFF.session_key({}, None, None, None) == "anon"


def test_turn_detection_dual_protocol():
    """⑥turn 判定双协议：OpenAI chat 形 messages[-1].role=="tool"；
    Anthropic 形 content 块含 tool_result；普通 user / 空消息
    均非工具续请求。"""
    _reset({})
    assert AFF.is_turn_continuation({
        "messages": [{"role": "user", "content": "q"},
                     {"role": "tool", "content": "res",
                      "tool_call_id": "c1"}]})
    assert AFF.is_turn_continuation({
        "messages": [{"role": "user",
                      "content": [{"type": "tool_result",
                                   "content": "res",
                                   "tool_use_id": "t1"}]}]})
    assert not AFF.is_turn_continuation(
        {"messages": [{"role": "user", "content": "q"}]})
    assert not AFF.is_turn_continuation({})


def test_conversations_json_atomic_write_crash_safe():
    """⑦落盘崩溃安全：后台线程 ≤2s 经 tmp+rename 原子写；
    写一半 kill（.tmp 残留半截 JSON）绝不污染主文件；主文件
    损坏 → 优雅降级保留内存态；外部实例合法写入 → mtime 变化
    触发重读（last-write-wins）。"""
    _reset({"key-9001": "200"})
    AFF._ensure_worker()
    AFF.record("s9", "9001", "GLM-5.3",
               {"prompt_tokens": 10,
                "prompt_tokens_details": {"cached_tokens": 2048}}, True)
    path = TMP / "conversations.json"
    # ① 后台 ≤2s 原子落盘 → 主文件可用
    deadline = time.time() + 6
    saved = None
    while time.time() < deadline:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if raw.get("sessions", {}).get("s9", {}).get("uid") == "9001":
                saved = raw
                break
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        time.sleep(0.1)
    assert saved is not None, "后台线程应在 ≤2s 内原子落盘 conversations.json"
    # ② 写一半 kill：.tmp 残留半截 JSON，主文件不受影响
    #    （F4：tmp 名带 pid——模拟别实例崩溃残留）
    (TMP / "conversations.json.999999.tmp").write_text(
        '{"sessions": {"s9": {"uid": "9001", "at": ', encoding="utf-8")
    with AFF._LOCK:
        AFF._LAST_MTIME = None
        AFF._reload_if_changed_locked()
    assert AFF._TABLE.get("s9", {}).get("uid") == "9001", \
        "半截 .tmp 绝不应污染主文件"
    # ③ 主文件损坏：重读优雅降级（告警 + 保留内存态，不抛）
    path.write_text("{truncated", encoding="utf-8")
    with AFF._LOCK:
        AFF._LAST_MTIME = None
        AFF._reload_if_changed_locked()
    assert AFF._TABLE.get("s9", {}).get("uid") == "9001", \
        "损坏文件应保留内存态（粘性记忆是良性可丢状态）"
    # ④ 外部实例写入合法文件：mtime 变化 → 重读（last-write-wins）
    path.write_text(json.dumps({"sessions": {"s9": {
        "uid": "9002", "model": "GLM-5.3", "turn": 1,
        "at": time.time(), "cache_read": 0,
        "cache_data": False}}}, ensure_ascii=False), encoding="utf-8")
    with AFF._LOCK:
        AFF._LAST_MTIME = None
        AFF._reload_if_changed_locked()
    assert AFF._TABLE.get("s9", {}).get("uid") == "9002", \
        "mtime 变化应触发重读（last-write-wins）"


def test_feature_off_skips_session_key_and_record():
    """⑧features.affinity=False → get_next 未收到 prefer
    （spy 取证）+ 零 record（内存表/why 分布全空）——
    客户端可见行为与升级前逐字节一致。"""
    _reset({"key-9001": "200"})
    cfg_path = TMP / "config.json"
    original = cfg_path.read_text(encoding="utf-8")
    try:
        cfg = json.loads(original)
        cfg["features"] = {"affinity": False}
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False),
                            encoding="utf-8")
        seen = {}
        orig_get_next = am.get_next

        def _spy(model=None, loads=None, prefer=None):
            seen["prefer"] = prefer
            return orig_get_next(model, loads=loads, prefer=prefer)

        am.get_next = _spy
        try:
            r = _post()
            assert r.status_code == 200, r.status_code
        finally:
            am.get_next = orig_get_next
        assert seen.get("prefer") is None, \
            "开关关闭时 get_next 不得收到 prefer"
        with AFF._LOCK:
            assert len(AFF._TABLE) == 0, "开关关闭时不得 record"
            assert len(AFF._WHY) == 0
    finally:
        cfg_path.write_text(original, encoding="utf-8")


def test_lru_eviction_and_ttl_expiry():
    """⑨512 条 LRU：第 513 条逐出最久未访问；24h 过期条目
    pick 视为 cold 且被 snapshot 清出。"""
    _reset({})
    for i in range(513):
        AFF.record(f"m:lru{i:04d}", "9001", "GLM-5.3",
                   {"prompt_tokens": 1}, True)
    with AFF._LOCK:
        assert len(AFF._TABLE) == 512
        assert "m:lru0000" not in AFF._TABLE, "LRU 应逐出最久未访问"
        assert "m:lru0512" in AFF._TABLE
    # 24h 过期：pick 视为无记忆（cold），snapshot 惰性清出
    with AFF._LOCK:
        AFF._TABLE["m:lru0512"]["at"] = time.time() - 25 * 3600
    assert AFF.pick_prefer("m:lru0512", True) == (None, AFF.WHY_COLD)
    stuck, _dist = AFF.snapshot()
    assert stuck == 511, stuck


def test_get_next_prefer_healthy_selects_it():
    """get_next 单测：prefer 账号健康 → 直接选它（忽略负载/轮转）。"""
    _reset({})
    acc = am.get_next("GLM-5.3", prefer="9002")
    assert acc["user_id"] == "9002"


def test_get_next_prefer_cooled_falls_back():
    """get_next 单测：prefer 账号冷却中 → 回落原轮转逻辑。"""
    _reset({})
    am.mark_cooldown("9002", 3600, "上游拒绝凭证")
    acc = am.get_next("GLM-5.3", prefer="9002")
    assert acc["user_id"] == "9001"


def test_get_next_prefer_disabled_falls_back():
    """get_next 单测：prefer 账号禁用 → 回落原轮转逻辑。"""
    _reset({})
    am.accounts[1]["enabled"] = False
    acc = am.get_next("GLM-5.3", prefer="9002")
    assert acc["user_id"] == "9001"


def test_stream_path_records_and_sticks():
    """流式路径同样 record（_stream_body 尾部 usage 记账处）
    并粘性生效：同会话两条流均落 9001。"""
    _reset({"key-9001": "200", "key-9002": "200", "key-9003": "200"})
    r1 = _post(stream=True, headers={"X-Session-Id": "sid-s"})
    assert r1.status_code == 200, r1.status_code
    body = r1.get_data(as_text=True)
    assert '"content":"he"' in body and "data: [DONE]" in body
    r2 = _post(stream=True, headers={"X-Session-Id": "sid-s"})
    assert r2.status_code == 200, r2.status_code
    assert HITS == ["key-9001", "key-9001"], HITS


def test_api_affinity_stats_endpoint():
    """观测：GET /api/affinity/stats 返回 stuck_sessions 与
    why_distribution（内存表锁内快照）。"""
    _reset({"key-9001": "200", "key-9002": "200", "key-9003": "200"})
    assert _post(headers={"X-Session-Id": "sid-stats"}).status_code == 200
    assert _post(headers={"X-Session-Id": "sid-stats"}).status_code == 200
    r = client.get("/api/affinity/stats")
    assert r.status_code == 200, r.status_code
    j = r.get_json()
    assert j["stuck_sessions"] == 1, j
    assert j["why_distribution"].get("cold") == 1, j
    assert j["why_distribution"].get("cache") == 1, j


def test_prefer_respects_reverse_isolation():
    """R1①：prefer=free 组账号 + 未绑定分组的订阅池模型
    （GLM-5.3-FLASH）→ 反向隔离排除 free 组，prefer 不被
    选（回退语义：只用未分组订阅号）。"""
    _reset({})
    am.accounts[0]["group"] = "free"      # 9001 降为 free 组
    acc = am.get_next("GLM-5.3-FLASH", prefer="9001")
    assert acc["user_id"] == "9002", acc  # free 组账号被隔离排除


def test_prefer_respects_glm_qualification():
    """R1②：prefer=无 GLM 直连权限账号 + model 含 glm →
    GLM 资格过滤排除，prefer 不被选（选有权限的账号）。"""
    _reset({})
    am.accounts[0]["has_glm53"] = True    # 仅 9001 有 GLM 权限
    acc = am.get_next("GLM-5.3", prefer="9002")
    assert acc["user_id"] == "9001", acc  # 无权限账号被资格过滤排除


def test_truncated_stream_does_not_record():
    """F5：截断流（上游半途断、无 [DONE] 终止符）不得覆写
    粘性记忆——record 仅在成功路径（见 [DONE]）执行。"""
    _reset({"key-9001": "broken", "key-9002": "200",
            "key-9003": "200"})
    activity._events.clear()
    r = _post(stream=True, headers={"X-Session-Id": "sid-trunc"})
    assert r.status_code == 200, r.status_code
    body = r.get_data(as_text=True)
    assert '"content":"x"' in body
    assert "data: [DONE]" not in body      # 上游确实没吐终止符
    assert any(e.get("status") == "截断" for e in activity.list(50))
    stuck, _dist = AFF.snapshot()
    assert stuck == 0, "截断流不得 record（成功才记忆）"
