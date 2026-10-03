# -*- coding: utf-8 -*-
"""TTFT 双口径 + 账号资格评分 v0 测试（阶段 D / 方案 §6）。

全程零真实网络：流式用例半驱动 _stream_body（学 test_budget
⑭——fake _UpstreamOpen 提供 iter_bytes 续读 + close，生成器
自然消费至 EOF → 成功路径记账段执行 → water.observe 落
perf）；聚合/端点用例驱动纯函数与 water 单例。

覆盖（派工 ①-⑤ + 端点/上限补充）：
① 纯 reasoning 流 → ttft_content 有值、ttft_text 无；
② 正常文本流 → 两者都有且 text≥content；
③ water.observe 新参缺省 → perf 结构与升级前一致
  （回归：缺省不写新键，旧 ttft/tps 照旧）；
④ quality 聚合 p50 数学正确（构造已知样本：奇/偶
  中位数、tps 均值、samples 计数）；
⑤ is_stream 分桶（非流式 ttft=latency 样本不进
  流式 p50）；
⑥ 端点 GET /api/stats/quality：200、water_hits
  （被动事件 + 探针 severity）、按 samples 降序、
  上限 50 行。

跑法：py -3.12 -m pytest tests/test_stats_quality.py -v
"""

import json
import os
import pathlib
import sys
import tempfile
import time

import pytest

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
os.environ.setdefault("CODELY_CLI_VERSION", "0.0.0-test")  # 免 subprocess 探测 CLI 版本

TMP = pathlib.Path(tempfile.mkdtemp(prefix="qidi-quality-test-"))
(TMP / "config.json").write_text(json.dumps({
    "port": 0,
    "features": {"affinity": False},
    "pool_models": ["GLM-5.3"],
    "model_groups": {},
    "providers": {},
    "roles": {},
    "vision_model": "GLM-5.3",
    "vision_capable_models": [],
    "pricing": {"GLM-5.3": {"input": 4, "output": 16}},
}), encoding="utf-8")
(TMP / "accounts.json").write_text(json.dumps({"accounts": [
    {"user_id": uid, "username": f"t{uid}", "org_id": "0",
     "access_token": "a.b.c", "enabled": True,
     "has_glm53": False, "budget_exceeded": False, "group": ""}
    for uid in ("9001", "9002", "9003")]}), encoding="utf-8")

# 路径常量必须在实例化单例（proxy_core.state）之前替换掉，
# 否则会写进真实数据文件（与 test_budget.py 同纪律：全量
# 收集时 import 阶段的覆写"先到者赢"——条件不成立则靠
# 下方 module 级 autouse fixture 切换，互不污染）。
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

import routes.proxy as RP  # noqa: E402
from routes.admin import bp as admin_bp  # noqa: E402
from proxy_core.state import token_stats  # noqa: E402
from proxy_core import budget as BUDGET  # noqa: E402

import flask  # noqa: E402

_app = flask.Flask(__name__)
_app.register_blueprint(admin_bp)
client = _app.test_client()

# 本文件用例零上游调用（半驱动 + 纯函数 + 端点读单例）；
# 上游地址指到不可达端口，任何意外调用立即失败而不是
# 打真实端点。
MOCK_UPSTREAM = "http://127.0.0.1:1"


@pytest.fixture(scope="module", autouse=True)
def _use_quality_tmp():
    """把磁盘路径切到本文件临时目录，并快照/还原 water
    单例内存态（perf/passive/probes 跨测试文件共享）。"""
    saved = (S.BASE_DIR, S.CONFIG_FILE, S.ACCOUNTS_FILE,
             S.STATS_FILE, S.WATER_FILE, S.LOG_FILE,
             A.ACCOUNTS_FILE, ST.STATS_FILE, WA.WATER_FILE,
             PV.CONFIG_FILE, WA.LITELLM_API_BASE)
    saved_water = (
        {k: {bk: list(bv) for bk, bv in v.items()}
         for k, v in WA.water.perf.items()},
        list(WA.water.passive),
        {k: {"last": v.get("last"),
             "history": list(v.get("history") or ())}
         for k, v in WA.water.probes.items()},
    )
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
    WA.LITELLM_API_BASE = MOCK_UPSTREAM
    token_stats.key_stats.clear()
    BUDGET._inflight.clear()
    yield
    (S.BASE_DIR, S.CONFIG_FILE, S.ACCOUNTS_FILE,
     S.STATS_FILE, S.WATER_FILE, S.LOG_FILE,
     A.ACCOUNTS_FILE, ST.STATS_FILE, WA.WATER_FILE,
     PV.CONFIG_FILE, WA.LITELLM_API_BASE) = saved
    PV._cache = None
    WA.water.perf, WA.water.passive, WA.water.probes = saved_water
    token_stats.key_stats.clear()
    BUDGET._inflight.clear()


@pytest.fixture(autouse=True)
def _reset_water():
    """每用例前后清空 water 内存态（单例跨用例共享；
    用例本身也用唯一 uid|model 键，双保险）。"""
    WA.water.perf.clear()
    WA.water.passive.clear()
    WA.water.probes.clear()
    yield
    WA.water.perf.clear()
    WA.water.passive.clear()
    WA.water.probes.clear()


# ─── 测试替身（学 test_budget ⑭）─────────────────────

def _drive_stream(chunks, uid, model):
    """半驱动 _stream_body：fake _UpstreamOpen 提供
    iter_bytes 续读 + close（无嗅探门 → opened.peek_iter
    不存在 → iter_bytes 路径，与生产无门路径同形）。
    生成器自然消费至 EOF（[DONE]）→ 成功路径记账段
    （token_stats/kdb/water.observe，proxy.py:836 起）
    执行。"""

    class _FakeResp:
        def __init__(self, chunks):
            self._iter = iter(chunks)

        def iter_bytes(self):
            # 逐 chunk 睡眠 2ms 模拟真实网络节奏：
            # perf 记录门 if usage and (ttft_ms or
            # total_ms) 在零耗时 mock 下为假（真实
            # 口径，不为测试改生产代码），睡眠后
            # ttft_ms/total_ms ≥1ms 与生产同形。
            for chunk in self._iter:
                time.sleep(0.002)
                yield chunk

    class _FakeOpened:
        """_UpstreamOpen 替身：iter_bytes 续读 + close。"""

        def __init__(self, chunks):
            self.resp = _FakeResp(chunks)
            self.closed = False

        def close(self):
            self.closed = True

    body = RP._stream_body(_FakeOpened(chunks), model, uid,
                           time.perf_counter(), None, None)
    for _ in body:
        pass


def _sse(payload: str) -> bytes:
    """一条 data: SSE 帧（尾 \n\n，与上游逐字节同形）。"""
    return f"data: {payload}\n\n".encode("utf-8")


# 终止 usage 帧（completion_tokens=8 → tps 有值）
USAGE = ("{\"choices\": [], \"usage\": {\"prompt_tokens\": 3, "
         "\"completion_tokens\": 8, \"total_tokens\": 11}}")


# ─── 用例 ──────────────────────────────────────────────

def test_pure_reasoning_stream_split():
    """①纯 reasoning 流：ttft_content 有值（reasoning
    属内容字段集 _GATE_CONTENT_FIELDS）、ttft_text 为
    None（可见文本口径剥离思考过程，全 None 保持 None）。"""
    chunks = [
        _sse('{"choices": [{"delta": {"reasoning_content": "想"}}]}'),
        _sse('{"choices": [{"delta": {"reasoning": "再想"}}]}'),
        _sse(USAGE),
        b"data: [DONE]\n\n",
    ]
    _drive_stream(chunks, "uid-q-reason", "GLM-5.3")
    p = WA.water.perf["uid-q-reason|GLM-5.3"]
    assert p.get("ttft_content"), "纯推理流：内容口径 TTFT 必须有值"
    assert not p.get("ttft_text"), "纯推理流：可见文本口径必须为 None（无键）"


def test_text_stream_both_ttft():
    """②正常文本流：两口径都有且 text≥content（推理
    delta 先到、文本 delta 后到，perf_counter 单调）。"""
    chunks = [
        _sse('{"choices": [{"delta": {"reasoning_content": "先想"}}]}'),
        _sse('{"choices": [{"delta": {"content": "你好"}}]}'),
        _sse(USAGE),
        b"data: [DONE]\n\n",
    ]
    _drive_stream(chunks, "uid-q-text", "GLM-5.3")
    p = WA.water.perf["uid-q-text|GLM-5.3"]
    assert p.get("ttft_content") and p.get("ttft_text")
    assert p["ttft_text"][0] >= p["ttft_content"][0]


def test_observe_default_params_schema():
    """③回归：observe 新参缺省 → perf 结构与升级前一致
    （只有 ttft/tps，不写新键；tps 口径照旧 =
    completion_tokens/(total_ms/1000)；持久化文件同形）。"""
    WA.water.observe("uid-q-default", "GLM-5.3", "GLM-5.3",
                     {"completion_tokens": 5, "total_tokens": 7},
                     ttft_ms=10, total_ms=20)
    p = WA.water.perf["uid-q-default|GLM-5.3"]
    assert set(p) == {"ttft", "tps"}, f"缺省调用不得写新键: {sorted(p)}"
    assert p["ttft"] == [10]
    assert p["tps"] == [250.0]
    on_disk = json.loads(
        (TMP / "water_check.json").read_text(encoding="utf-8"))
    assert set(on_disk["perf"]["uid-q-default|GLM-5.3"]) == {"ttft", "tps"}


def test_p50_math():
    """④聚合 p50 数学正确：奇数取中、偶数取中间两数
    均值、tps 均值、samples 计数（三桶长度和）。"""
    perf = {
        "a|M": {
            "ttft_content": [100, 200, 300],      # p50=200
            "ttft_text": [100, 200, 300, 400],    # p50=250
            "tps": [10.0, 20.0],                  # 均值=15.0
        },
    }
    out = WA.quality_aggregate(perf)
    assert len(out["rows"]) == 1
    row = out["rows"][0]
    assert row["user_id"] == "a" and row["model"] == "M"
    assert row["ttft_p50"] == 200
    assert row["ttft_text_p50"] == 250
    assert row["tokens_per_sec"] == 15.0
    assert row["samples"] == 9
    assert row["water_hits"] == 0


def test_nonstream_excluded():
    """⑤is_stream 分桶：非流式样本（ttft=latency_ms）
    只进旧 ttft/tps 桶，不污染流式 p50（直接排除）。"""
    perf = {
        "b|M": {
            "ttft": [5000, 6000],         # 非流式延迟（=latency_ms）
            "tps": [100.0],
            "ttft_content": [100, 200],   # 流式口径
            "ttft_text": [120, 220],
        },
    }
    out = WA.quality_aggregate(perf)
    row = out["rows"][0]
    assert row["ttft_p50"] == 150        # (100+200)/2，非流式 5000/6000 不参与
    assert row["ttft_text_p50"] == 170   # (120+220)/2
    assert out["bucketing"] == "nonstream_excluded"


def test_endpoint_hits_and_fields():
    """⑥端点：200 + water_hits（被动事件 + 探针
    severity suspect/watered）+ success_rate 不可用
    以 samples 代替并注明。"""
    WA.water.perf["c|M"] = {"ttft_content": [30, 40], "tps": [5.0]}
    WA.water.perf["d|M"] = {"ttft_content": [50], "tps": [1.0]}
    WA.water.passive.append({
        "time": "10-03 12:00:00", "key": "c|M",
        "user_id": "c", "model": "M",
        "returned": "other", "note": "请求 M，上游返回 other",
    })
    WA.water.probes["d|M"] = {
        "last": {"user_id": "d", "model": "M", "severity": "watered"},
        "history": [{"user_id": "d", "model": "M", "severity": "suspect"}],
    }
    r = client.get("/api/stats/quality")
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    data = r.get_json()
    rows = {f"{row['user_id']}|{row['model']}": row
            for row in data["rows"]}
    assert rows["c|M"]["water_hits"] == 1   # 被动事件 1 次
    assert rows["d|M"]["water_hits"] == 2   # last + history 各 1
    assert rows["c|M"]["samples"] == 3      # 2+0+1
    assert rows["d|M"]["samples"] == 2      # 1+0+1
    assert data["success_rate"] is None     # water 无成败计数
    assert data["count"] == len(data["rows"])


def test_sort_desc_cap_50():
    """⑥补：按 samples 降序、上限 50 行（55 个目标
    只留 samples 最大的 50 个）。"""
    perf = {}
    for i in range(55):
        # samples = (i+1)+1 → u54 最多（56），u0 最少（2）
        perf[f"u{i}|M"] = {"ttft_content": [1] * (i + 1), "tps": [1.0]}
    out = WA.quality_aggregate(perf)
    assert out["count"] == 50
    assert out["rows"][0]["user_id"] == "u54"
    samples_seq = [r["samples"] for r in out["rows"]]
    assert samples_seq == sorted(samples_seq, reverse=True)
    kept = {r["user_id"] for r in out["rows"]}
    assert "u54" in kept and "u0" not in kept
