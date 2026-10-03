# -*- coding: utf-8 -*-
"""客户端预算限额（阶段 C / 方案 §5）测试：day 窗、按 key 限额。

全程打本地 mock 上游（127.0.0.1，零真实网络）：
- est 复用 router._estimate_tokens（chars/2.5）——
  用例正文长度即估值（25 字符 → est=10，
  150 字符 → est=60，6250 字符 → est=2500）；
- 用量重放走 stats 既有按 key 日记录
  （token_stats.json "__keys__"），零网络零真实
  数据文件（路径常量注入 TMP，方案 §7.7 纪律）。

覆盖（派工 ①-⑨ + 补充）：
① 限额内正常请求通过且按 key 记账；
② 超限 429（OpenAI 形）：消息含 key 名/已用/上限/
   重置时间，Retry-After > 0；
③ /v1/messages 超限 429 为 Anthropic 错误形；
④ 并发预留：两请求同时在途，第二个 est 使
   used+reserved 触顶 → 429（超发≈1 请求语义）；
⑤ 流式完成后预留释放（第二请求恢复放行）；
⑥ embeddings 计入、/v1/models 豁免（按 path 分流）；
⑦ daily_token_limit 旧字段迁移语义 + limit.tokens
   优先 + 非 day period 告警按 day 处理；
⑧ features.budget=False → 429 不触发、零行为改变；
⑨ 跨午夜重置（注入昨日条目，验证 stats rollover
   清零——day 窗=本地日历日，重置点=当日午夜）；
⑩ release 幂等（同一次 reserve 多次调用只减一次，
   magpie sync.Once 同款）；
⑪ 成本限额判负（cost 按 pricing 现价折算 est）；
⑫ /api/keys/info 预算快照（§5.4）；
⑬ cache_reads=false（缺省）：缓存读 token 不计入
   预算；
⑭ /v1/messages 流式记账打通（R1）：桥消费至
   EOF → _stream_body 成功路径记账段执行，
   key_stats 有记录、在途释放；
⑮ est 按端点归一（R2）：chat/responses/
   embeddings/messages 四端点 est>0（统一
   chars/2.5，不另起公式）；
⑯ raw≥limit 且 budget 开启 → 429 为预算形状
   （带 Retry-After），不是旧 daily_token_limit
   形状（R3：旧检查在预算生效时被门控跳过）。

跑法：py -3.12 -m pytest tests/test_budget.py -v
"""

import json
import os
import pathlib
import socket
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
os.environ.setdefault("CODELY_CLI_VERSION", "0.0.0-test")  # 免 subprocess 探测 CLI 版本

TMP = pathlib.Path(tempfile.mkdtemp(prefix="qidi-budget-test-"))
(TMP / "config.json").write_text(json.dumps({
    "port": 0,
    "features": {"affinity": False},
    "pool_models": ["GLM-5.3", "KIMI-K3"],
    "model_groups": {},
    "providers": {},
    "roles": {},
    "vision_model": "GLM-5.3",
    "vision_capable_models": [],
    "pricing": {"GLM-5.3": {"input": 4, "output": 16}},
    # embedding 上游指向本地 mock（/v1/embeddings 转发目标）
    "embedding_upstream": {"base_url": None, "timeout": 5},
    "api_keys": [
        {"key": "bk-1", "label": "预算甲",
         "limit": {"period": "day", "tokens": 100, "cost": 5.0}},
        {"key": "bk-2", "label": "预算乙",
         "limit": {"period": "day", "tokens": 100, "cost": 5.0}},
        {"key": "bk-3", "label": "旧字段丙", "daily_token_limit": 100},
        {"key": "bk-4", "label": "无限丁"},
        {"key": "bk-5", "label": "成本戊",
         "limit": {"period": "day", "cost": 0.01}},
        {"key": "bk-6", "label": "并存己", "daily_token_limit": 500,
         "limit": {"period": "day", "tokens": 100}},
        {"key": "bk-7", "label": "周窗庚",
         "limit": {"period": "week", "tokens": 50}},
    ],
}), encoding="utf-8")
(TMP / "accounts.json").write_text(json.dumps({"accounts": [
    {"user_id": uid, "username": f"t{uid}", "org_id": "0", "access_token": "a.b.c",
     "enabled": True, "has_glm53": False, "budget_exceeded": False, "group": ""}
    for uid in ("9001", "9002", "9003")]}), encoding="utf-8")

# 路径常量必须在实例化单例（proxy_core.state）之前替换掉，否则会写进真实数据文件
# （与 test_channel_gate.py 同思路：全量收集时 state 可能已被更早的文件
# 实例化——条件不成立则靠下方 module 级 autouse fixture 切换，互不污染）。
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
from proxy_core import budget as BUDGET  # noqa: E402

# ─── mock 上游：HITS 记录每次请求的 Authorization ────────────────

HITS = []          # 每次请求的 Authorization 头
BEHAVIOR = {}      # "key-9001" -> "200" | "<状态码>"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *args):
        pass

    def do_GET(self):  # noqa: N802
        # /v1/models 聚合探测（_fetch_models_for_groups 打上游 GET）
        body = json.dumps({"data": [{"id": "GLM-5.3"},
                                     {"id": "KIMI-K3"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        raw = self.rfile.read(int(self.headers.get("content-length") or 0))
        req = json.loads(raw.decode("utf-8"))
        auth = self.headers.get("authorization", "").removeprefix("Bearer ").strip()
        mode = BEHAVIOR.get(auth, "200")
        HITS.append(auth)
        if mode != "200":
            self._serve_plain_error(int(mode))
            return
        if req.get("stream"):
            # SSE：内容块 + usage 终止块 + [DONE]
            lines = [
                'data: {"choices": [{"delta": {"content": "he"}, "index": 0}]}',
                'data: {"choices": [{"delta": {"content": "llo"}, "index": 0}]}',
                'data: {"choices": [], "usage": {"prompt_tokens": 10, '
                '"completion_tokens": 2, "total_tokens": 12}}',
                "data: [DONE]",
            ]
            body = ("\n".join(lines) + "\n\n").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            body = json.dumps({
                "model": "GLM-5.3",
                "choices": [{"message": {"role": "assistant", "content": "hello"},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2,
                          "total_tokens": 12},
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

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
# embedding_upstream.base_url 在 config 里为 None（导入期常量，
# 由 fixture 注入的模块级常量不可热改）——预算判负发生在
# 装饰器（视图之前），豁免场景不依赖上游可达性。
(TMP / "config.json").write_text(json.dumps(
    json.loads((TMP / "config.json").read_text(encoding="utf-8"))
    | {"embedding_upstream": {"base_url": MOCK, "timeout": 5}}),
    encoding="utf-8")

import routes.proxy as RP  # noqa: E402
from routes.admin import bp as admin_bp  # noqa: E402
from proxy_core.state import am, api_cache, token_stats  # noqa: E402

# 注意：与 test_channel_gate.py 同理——不在 import 时覆写
# 全局路径/上游，靠下方 module 级 autouse fixture 切换并还原。

from flask import Flask  # noqa: E402

_app = Flask(__name__)
_app.register_blueprint(RP.bp)
_app.register_blueprint(admin_bp)
client = _app.test_client()

api_cache.get = lambda acc: f"key-{acc['user_id']}"      # 免向上游换 key

TODAY = datetime.now().strftime("%Y-%m-%d")


def _midnight_str() -> str:
    """下一个本地午夜的字符串表示（预算重置点）。"""
    nxt = (datetime.now() + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return nxt.strftime("%Y-%m-%d %H:%M:%S")


@pytest.fixture(scope="module", autouse=True)
def _use_budget_mock():
    """把上游与磁盘路径切到本文件的 mock/临时目录（用例后还原）。

    与 test_channel_gate.py 的 fixture 同构：pytest 全量收集时
    import 阶段的覆写"先到者赢"，本 fixture 保证无论收集顺序
    如何，本文件用例期间全局指向本文件 mock，用例结束还原，
    互不污染。
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
    # 清掉别文件留下的内存记账/在途预留（单例跨文件共享）
    token_stats.key_stats.clear()
    BUDGET._inflight.clear()
    yield
    (S.BASE_DIR, S.CONFIG_FILE, S.ACCOUNTS_FILE,
     S.STATS_FILE, S.WATER_FILE, S.LOG_FILE,
     A.ACCOUNTS_FILE, ST.STATS_FILE, WA.WATER_FILE,
     PV.CONFIG_FILE, RP.LITELLM_API_BASE, WA.LITELLM_API_BASE) = saved
    PV._cache = None
    token_stats.key_stats.clear()
    BUDGET._inflight.clear()


@pytest.fixture(autouse=True)
def _reset():
    """每用例前清空内存记账与在途表（单例跨用例共享）。"""
    HITS.clear()
    BEHAVIOR.clear()
    am.index = 0
    api_cache.cache.clear()
    RP._prov_quota_cooldown.clear()   # provider 冷却是模块级状态，必须清
    PV._key_rr.clear()                # pick_key 轮转计数同为模块级
    RP._models_agg_cache.clear()      # /v1/models 聚合缓存（60s TTL）
    for a in am.accounts:
        a["enabled"] = True
        a["budget_exceeded"] = False
        a["budget_cooldown_until"] = 0
        a["cooldown_reason"] = ""
        a["quota_team"] = ""
        a["group"] = ""
    token_stats.key_stats.clear()
    BUDGET._inflight.clear()
    yield


# ─── 测试替身 ──────────────────────────────────────────────────────

def _seed_used(label: str, tokens: int, cache_tokens: int = 0):
    """把指定 key 的当日用量种进 stats 按 key 日记录
    （走既有 record_key 路径，与真实请求记账同形）。"""
    usage = {"prompt_tokens": tokens, "completion_tokens": 0,
             "total_tokens": tokens}
    if cache_tokens:
        usage["prompt_tokens_details"] = {"cached_tokens": cache_tokens}
    token_stats.record_key(label, "GLM-5.3", usage)


def _chat(key_id: str, content: str = "你好",
          stream: bool = False, model: str = "GLM-5.3"):
    return client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {key_id}"},
        json={"model": model, "stream": stream,
              "messages": [{"role": "user", "content": content}]})


# ─── 用例 ──────────────────────────────────────────────────────────

def test_in_limit_request_passes_and_accounts():
    """①限额内正常请求通过且按 key 记账（方案 §5）。"""
    r = _chat("bk-1", content="你好")      # est = 2/2.5 = 0
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    r.get_data()
    e = token_stats.key_stats.get("预算甲")
    assert e is not None and e["date"] == TODAY
    assert e["total_tokens"] == 12         # mock 上游 usage
    assert e["requests"] == 1
    assert "bk-1" not in BUDGET._inflight  # 非流式：装饰器已释放


def test_over_limit_429_openai_shape():
    """②超限 429（OpenAI 形）：消息含 key 名/已用/上限/
    重置时间（本地时区），Retry-After > 0。"""
    _seed_used("预算甲", 95)
    r = _chat("bk-1", content="x" * 25)    # est=10 → 95+10>=100
    assert r.status_code == 429
    body = r.get_json()
    assert body["error"]["code"] == "budget_limit_exceeded"
    assert body["error"]["type"] == "rate_limit_error"
    assert body["error"]["param"] is None
    msg = body["error"]["message"]
    assert "预算甲" in msg                   # key 名
    assert "95" in msg and "100" in msg      # 已用/上限
    assert _midnight_str() in msg            # 重置时间（本地时区）
    assert int(r.headers["Retry-After"]) > 0
    assert "bk-1" not in BUDGET._inflight  # 判负回滚，不留在途


def test_messages_over_limit_anthropic_shape():
    """/v1/messages 超限 429 为 Anthropic 错误形
    （anthropic.error_payload 套出，方案 §5.3）。"""
    _seed_used("预算乙", 95)
    r = client.post(
        "/v1/messages",
        headers={"x-api-key": "bk-2"},        # 备用鉴权头
        json={"model": "GLM-5.3", "max_tokens": 16,
              "messages": [{"role": "user", "content": "x" * 25}]})
    assert r.status_code == 429
    body = r.get_json()
    assert body["type"] == "error"
    assert body["error"]["type"] == "rate_limit_error"
    assert "预算乙" in body["error"]["message"]
    assert int(r.headers["Retry-After"]) > 0


def test_concurrent_reserve_overadmission():
    """④并发预留（真并发：ThreadPool×2 + Barrier
    同步同时在途）：两请求同时抵达临界区，第二
    个 est 使 used+reserved 触顶 → 判负（超发
    ≈1 请求语义：第一个 est=4 放行时 95+4=99，
    距顶仅 1）。

    真并行机制：ThreadPoolExecutor(2) 两真实
    线程 + Barrier(2) 会合后同时进入 reserve；
    结果确定（与调度次序无关）——锁串行化临界
    区：先登记者 95+4=99<100 放行，后登记者
    95+8=103>=100 判负，恰好一成一负。"""
    _seed_used("预算甲", 95)
    barrier = threading.Barrier(2)

    def _worker():
        barrier.wait()      # 两线程会合后同时进入临界区
        return BUDGET.reserve("bk-1", 4, model="GLM-5.3")

    with ThreadPoolExecutor(2) as pool:
        f1 = pool.submit(_worker)
        f2 = pool.submit(_worker)
        r1, refusal1 = f1.result()
        r2, refusal2 = f2.result()
    assert (refusal1 is None) != (refusal2 is None)  # 恰好一成一负
    ok_rel = r1 if refusal1 is None else r2
    rej = refusal1 if refusal1 is not None else refusal2
    assert rej["reason"] == "tokens"
    assert rej["used_tokens"] == 95
    assert rej["limit_tokens"] == 100
    assert "bk-1" in BUDGET._inflight       # 仅放行请求在途
    assert BUDGET._inflight["bk-1"]["n"] == 1
    ok_rel()                                # 放行请求完成释放
    assert "bk-1" not in BUDGET._inflight


def test_stream_release():
    """⑤流式完成后预留释放（第二请求恢复放行）：
    若释放缺失，第二请求会看到在途 60+60 触顶而 429。"""
    r = _chat("bk-1", content="x" * 150, stream=True)   # est=60
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    r.get_data()                            # 消费完整条流 → finally 释放
    assert "bk-1" not in BUDGET._inflight
    r2 = _chat("bk-1", content="x" * 150, stream=True)  # used=12, 12+60<100
    assert r2.status_code == 200, r2.get_data(as_text=True)[:300]
    r2.get_data()


def test_embeddings_counted_models_exempt():
    """⑥embeddings 计入预算、/v1/models 豁免（按 request.path
    分流，USAGE_PATHS 常量）。

    成本-only 限额（无 tokens → 旧日限额检查永不触发）：
    已用成本按 pricing 现价折算（2500 token × 4 元/百万
    = 0.01 元）已达上限 → 产生 usage 的端点 429；
    /v1/models 不产生用量 → 豁免放行。"""
    _seed_used("成本戊", 2500)
    r = client.post(
        "/v1/embeddings",
        headers={"Authorization": "Bearer bk-5"},
        json={"model": "GLM-5.3", "input": "hello"})
    assert r.status_code == 429
    assert r.get_json()["error"]["code"] == "budget_limit_exceeded"
    r2 = client.get("/v1/models",
                    headers={"Authorization": "Bearer bk-5"})
    assert r2.status_code == 200, r2.get_data(as_text=True)[:300]


def test_daily_limit_migration_and_priority():
    """⑦daily_token_limit 旧字段迁移语义（方案 §5.1）：
    无 limit 块 → 视为 {"period":"day","tokens":旧值}；
    与 limit.tokens 并存 → tokens 优先；非 day period
    告警并按 day 处理。"""
    keys = S.get_api_keys()
    c = keys["bk-3"]
    assert c["budget"] == {"period": "day", "tokens": 100,
                           "cost": None, "cache_reads": False}
    assert c["limit"] == 100                # 迁移后生效阈值
    j = keys["bk-6"]
    assert j["budget"]["tokens"] == 100     # limit.tokens 优先于 500
    assert j["limit"] == 100
    g = keys["bk-7"]
    assert g["budget"]["period"] == "day"   # week → 告警并按 day 处理
    assert g["budget"]["tokens"] == 50
    assert keys["bk-4"]["budget"] is None   # 无任何限额配置


def test_feature_off_no_budget_429():
    """⑧features.budget=False → 429 不触发、零行为改变：
    同一请求（预算开时 429）在关后放行，且零在途预留。"""
    _seed_used("预算甲", 95)
    cfg = json.loads((TMP / "config.json").read_text(encoding="utf-8"))
    cfg["features"] = {"affinity": False, "budget": False}
    (TMP / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
    try:
        r = _chat("bk-1", content="x" * 25)   # est=10：预算开时 95+10>=100
        assert r.status_code == 200, r.get_data(as_text=True)[:300]
        r.get_data()
        assert "bk-1" not in BUDGET._inflight  # 零预留
    finally:
        cfg["features"] = {"affinity": False}
        (TMP / "config.json").write_text(json.dumps(cfg), encoding="utf-8")


def test_midnight_reset():
    """⑨跨午夜重置：day 窗=本地日历日，重置点=当日午夜。

    把条目日期改到昨天 → stats.get_key_stats() 的
    rollover（stats.py:61-67）清零旧日期条目 → 预算
    used 重放为 0（若不重置，95+10>=100 会 429）。"""
    _seed_used("预算甲", 95)
    token_stats.key_stats["预算甲"]["date"] = (
        datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    r = _chat("bk-1", content="x" * 25)    # est=10
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    r.get_data()
    # rollover 后条目已是今日零起点（随后记账在其上累加）
    e = token_stats.key_stats.get("预算甲")
    assert e is not None and e["date"] == TODAY


def test_release_idempotent():
    """⑩释放幂等：同一次 reserve 的 release 调多次只减
    一次（magpie sync.Once 同款语义）。"""
    r1, _ = BUDGET.reserve("bk-1", 10, model="GLM-5.3")
    r2, _ = BUDGET.reserve("bk-1", 10, model="GLM-5.3")
    assert BUDGET._inflight["bk-1"]["n"] == 2
    assert BUDGET._inflight["bk-1"]["tokens"] == 20
    r1(); r1(); r1()                        # 多次调用只减一次
    assert BUDGET._inflight["bk-1"]["n"] == 1
    assert BUDGET._inflight["bk-1"]["tokens"] == 10
    r2()
    assert "bk-1" not in BUDGET._inflight


def test_cost_limit_429():
    """⑪成本限额判负：cost 按 costing.cost_of 以 pricing
    现价折算 est（est 全部视作输入 token；2500 token ×
    4 元/百万 = 0.01 元 = 上限 → 判负）。"""
    r = _chat("bk-5", content="x" * 6250)   # est=2500
    assert r.status_code == 429
    body = r.get_json()
    assert body["error"]["code"] == "budget_limit_exceeded"
    assert "0.01" in body["error"]["message"]
    assert "成本戊" in body["error"]["message"]


def test_keys_info_budget_snapshot():
    """⑫/api/keys/info 预算快照（方案 §5.4）：配置了
    limit 块的 key 曝光 budget 各维度（含
    inflight_cost）；无 limit 的 key 不加 budget
    键；tokens_left/cost_left 均含在途扣减。"""
    _seed_used("预算甲", 40)
    r = client.get("/api/keys/info")
    assert r.status_code == 200
    keys = {k["label"]: k for k in r.get_json()["keys"]}
    b = keys["预算甲"]["budget"]
    assert b["limit_tokens"] == 100
    assert b["used_tokens"] == 40
    assert b["tokens_left"] == 60
    assert b["limit_cost"] == 5.0
    assert b["used_cost"] == round(40 / 1e6 * 4, 6)
    assert b["cost_left"] == round(5.0 - 40 / 1e6 * 4, 6)
    assert b["reset_at"] == _midnight_str()
    assert b["inflight"] == 0 and b["inflight_tokens"] == 0
    assert b["inflight_cost"] == 0             # 零在途
    assert "budget" not in keys["无限丁"]        # 无 limit → 不加键
    assert keys["旧字段丙"]["budget"]["limit_tokens"] == 100  # 迁移语义
    # 在途预留计入快照（口径与新请求判负条件一致，
    # cost 维度同步扣减）
    rel, _ = BUDGET.reserve("bk-1", 20, model="GLM-5.3")
    r2 = client.get("/api/keys/info")
    b2 = {k["label"]: k for k in r2.get_json()["keys"]}["预算甲"]["budget"]
    assert b2["inflight"] == 1
    assert b2["inflight_tokens"] == 20
    assert b2["inflight_cost"] == round(20 / 1e6 * 4, 6)  # 8e-05
    assert b2["tokens_left"] == 40              # 100-40-20
    assert b2["cost_left"] == round(
        5.0 - 40 / 1e6 * 4 - 20 / 1e6 * 4, 6)  # 4.99976
    rel()


def test_cache_reads_excluded_when_false():
    """⑬cache_reads=false（缺省）：缓存读 token 不计入
    预算（stats 侧 record_key 单独累计 cache_tokens）。

    total=95 其中缓存读 20 → 预算口径 used=75；
    est=10 → 75+10=85<100 放行（若不剔除缓存读，
    95+10=105>=100 会 429）。"""
    _seed_used("预算甲", 95, cache_tokens=20)
    r = _chat("bk-1", content="x" * 25)        # est=10
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    r.get_data()


def test_messages_stream_records_key_stats(monkeypatch):
    """⑭/v1/messages 流式记账打通（R1）：桥消费至
    EOF（[DONE] 只跳过其后 data 行、外层续读到
    流尾）→ 内层 _stream_body 生成器跑到自然结束
    （StopIteration）→ 成功路径记账段（token_stats
    .record/record_key，proxy.py:791 起）执行。

    机制：fake _proxy_chat 返回的 Flask Response
    其 response 体是真 _stream_body 生成器——与
    生产路径同形（_proxy_chat 流式分支返回
    Response(_stream_body(...))，proxy.py:1183）。
    修复前桥在 [DONE] 处 break 外层循环，
    _stream_body 挂起点收到 finally 中
    inner.close() 触发的 GeneratorExit，记账段
    整段跳过（Claude Code 主路径 key_stats 恒
    0、预算 used 恒 0）；现有 test_stream_failover
    .py 的流式用例 mock 返回裸 chunk 迭代器，
    测不到该机制，故本用例半驱动复现。"""
    def fake_proxy(data):
        assert data["stream"] is True
        chunks = [
            'data: {"model": "GLM-5.3", "choices": '
            '[{"delta": {"content": "你"}}]}\n\n'.encode("utf-8"),
            'data: {"choices": [{"delta": {"content": "好"}}], '
            '"usage": {"prompt_tokens": 2, "completion_tokens": 5, '
            '"total_tokens": 7}}\n\n'.encode("utf-8"),
            'data: {"choices": [{"finish_reason": "stop"}]}\n\n'
            .encode("utf-8"),
            b"data: [DONE]\n\n",
        ]

        class _FakeResp:
            def __init__(self, chunks):
                self._iter = iter(chunks)

            def iter_bytes(self):
                return self._iter

        class _FakeOpened:
            """_UpstreamOpen 替身：iter_bytes 续读 +
            close（_stream_body 经 getattr(opened,
            "peek_iter", None) 判无嗅探门 → 走
            iter_bytes 续读）。"""

            def __init__(self, chunks):
                self.resp = _FakeResp(chunks)
                self.closed = False

            def close(self):
                self.closed = True

        body = RP._stream_body(
            _FakeOpened(chunks), "GLM-5.3", "uid-r5-stream",
            time.perf_counter(), None, "预算甲",
            budget_release=RP._g_budget_release())
        return RP.Response(body, content_type="text/event-stream")

    monkeypatch.setattr(RP, "_proxy_chat", fake_proxy)
    r = client.post(
        "/v1/messages",
        headers={"Authorization": "Bearer bk-1"},
        json={
            "model": "GLM-5.3", "max_tokens": 16,
            "stream": True,
            "system": "你是一个助手",
            "tools": [{"name": "get_weather",
                       "description": "查天气"}],
            "messages": [{"role": "user",
                          "content": "x" * 150}],
        })
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    body = r.get_data(as_text=True)
    assert "message_stop" in body     # 桥正常收尾（finish 事件）
    # 记账段已执行（修复前此处 key_stats 恒 0）
    e = token_stats.key_stats.get("预算甲")
    assert e is not None, "流式成功路径记账段未执行（R1 回归）"
    assert e["total_tokens"] == 7            # mock 上游 usage
    assert e["requests"] == 1
    assert "bk-1" not in BUDGET._inflight    # _stream_body finally 释放


def test_estimate_tokens_per_endpoint_normalized():
    """⑮est 按端点归一（R2）：四端点 est>0，口径
    统一 chars/2.5（复用 router._estimate_tokens
    遍历口径，不另起公式）。

    - chat：messages（原口径）；
    - responses/embeddings：input（字符串或块
      列表——请求体是 input 而非 messages，原
      口径对这两端点 est 恒 0）；
    - messages：messages + 顶层 system + tools
      JSON 长（原口径漏 system/tools，est 偏低）；
    - 非 usage 端点（/v1/models）：0。"""
    # chat：250 字符内容 → 100
    assert BUDGET.estimate_tokens_for(
        "/v1/chat/completions",
        {"messages": [{"role": "user", "content": "x" * 250}]}) == 100
    # responses：input 字符串 / 块列表
    assert BUDGET.estimate_tokens_for(
        "/v1/responses", {"input": "x" * 250}) == 100
    assert BUDGET.estimate_tokens_for(
        "/v1/responses",
        {"input": [{"type": "input_text", "text": "y" * 250}]}) == 100
    # embeddings：input 字符串 / 字符串列表
    assert BUDGET.estimate_tokens_for(
        "/v1/embeddings", {"input": "x" * 250}) == 100
    assert BUDGET.estimate_tokens_for(
        "/v1/embeddings", {"input": ["z" * 125, "w" * 125]}) == 100
    # messages：仅 messages → 100；加顶层 system
    # （250 字符）+ tools（JSON 长）后显著升高
    msgs_est = BUDGET.estimate_tokens_for(
        "/v1/messages",
        {"messages": [{"role": "user", "content": "x" * 250}]})
    tools = [{"name": "t", "description": "d" * 250}]
    full_est = BUDGET.estimate_tokens_for(
        "/v1/messages",
        {"messages": [{"role": "user", "content": "x" * 250}],
         "system": "s" * 250, "tools": tools})
    assert msgs_est == 100
    assert full_est > msgs_est               # system+tools 计入
    tools_json_len = len(json.dumps(tools, ensure_ascii=False))
    assert full_est == int((250 + 250 + tools_json_len) / 2.5)
    # 非 usage 端点：0（预算不预留）
    assert BUDGET.estimate_tokens_for("/v1/models", {}) == 0


def test_raw_limit_reached_budget_on_yields_budget_shape():
    """⑯raw≥limit 且 budget 开启 → 429 为预算形状
    （R3）：旧 daily_token_limit 检查在预算生效时
    被门控跳过（兜底语义仅 features.budget=False
    时），拦截来自预算判负（used+在途>=limit）——
    响应是预算形状（code=budget_limit_exceeded、
    带 Retry-After），不是旧形状
    （daily_token_limit_exceeded）。"""
    _seed_used("预算甲", 100)     # raw 已达 limit=100
    r = _chat("bk-1", content="x" * 25)   # est=10 → 100+10>=100
    assert r.status_code == 429
    body = r.get_json()
    assert body["error"]["code"] == "budget_limit_exceeded"
    assert body["error"]["code"] != "daily_token_limit_exceeded"
    assert body["error"]["type"] == "rate_limit_error"
    assert int(r.headers["Retry-After"]) > 0
    assert "bk-1" not in BUDGET._inflight  # 判负回滚，不留在途
