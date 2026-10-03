# -*- coding: utf-8 -*-
"""语料采集（阶段 E / 方案 §7）测试：correlation 三协议、
_StreamTap 累积、决策表、QueueWorker 落盘。

全程零真实网络零仓库数据文件：settings 路径常量
注入 TMP（方案 §7.7 纪律——新模块一律走
settings.py 路径常量模式），config.json 的
corpus 块按用例重写。

覆盖：
① 三协议 correlation 捕获（openai/responses/
   anthropic——原始 messages 形态、tools 名称
   列表、protocol 标记；图片块 [image:N] 占位；
   开关关闭 → None 零开销）；
② _StreamTap 累积（content/thinking/tool_calls
   分片合并、finish_reason）；
③ 决策表三分支（partial 丢弃 / water_suspect
   保留+打标 / 正常保留）；
④ sample_rate 采样（rate=0 全弃）；
⑤ roles 白名单；
⑥ max_body_tokens 超限丢弃；
⑦ 脱敏后落盘（worker：messages+assistant 全
   类目脱敏、redact_hits 写入、local_YYYYMM
   .jsonl 逐条 flush+fsync 落盘可见）；
⑧ 本地 id 规则 local-YYYYMMDD-序号；
⑨ worker 异常不炸（毒样本 → 丢弃计数、好样本
   仍落盘——真实 worker 线程驱动）；
⑩ seedguard 告警态暂停采集（activity 提示一次
   防刷屏）；
⑪ include_thinking=False 思考过程不入语料；
⑫ /api/corpus/stats 端点（今日/本月/分布/
   water_suspect/redact_hits/discarded/backlog）；
⑬ _stream_body 半驱动集成（流式 tap 累积 →
   成功收尾组样本 → 落盘）；
⑭ 媒体块白名单占位（递归 + 文本白名单——
   tool_result 嵌套 base64/document →
   [media:N,type] 占位，R6 项目2）；
⑮ meta 字段脱敏（client/session_id 各过
   文本规则，R6 项目1）；
⑯ tool_calls.arguments 结构化脱敏（JSON 串
   键名探测生效，R6 项目6）；
⑰ _StreamTap 字符软顶（20 万字符停累积 +
   body_truncated 落盘，R6 项目7）；
⑱ worker 开关复检（入队后关开关 → 丢弃
   计数不落盘，R6 项目8）；
⑲ 入口级关闭逐字节一致（chat/responses/
   messages 三入口 corpus 关闭响应体逐字节
   恒定，R6 项目10）。

跑法：py -3.12 -m pytest tests/test_corpus.py -v
"""

import json
import os
import pathlib
import sys
import tempfile
import threading
import time
from http.server import (BaseHTTPRequestHandler,
                         ThreadingHTTPServer)

import pytest

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
os.environ.setdefault("CODELY_CLI_VERSION", "0.0.0-test")  # 免 subprocess 探测 CLI 版本

TMP = pathlib.Path(tempfile.mkdtemp(prefix="qidi-corpus-test-"))
(TMP / "config.json").write_text(json.dumps({
    "port": 0,
    "features": {"affinity": False},
    "pool_models": ["GLM-5.3", "KIMI-K3"],
    "model_groups": {},
    "providers": {},
    "roles": {"coder": "GLM-5.3"},
    "vision_model": "GLM-5.3",
    "vision_capable_models": [],
    "corpus": {"enabled": True},
}), encoding="utf-8")
(TMP / "accounts.json").write_text(json.dumps({"accounts": [
    {"user_id": uid, "username": f"t{uid}", "org_id": "0",
     "access_token": "a.b.c", "enabled": True,
     "has_glm53": False, "budget_exceeded": False, "group": ""}
    for uid in ("9001", "9002")]}), encoding="utf-8")

# 路径常量必须在实例化单例（proxy_core.state）之前替换掉，
# 否则会写真实数据文件（与 test_stats_quality.py 同纪律：全量
# 收集时 import 阶段的覆写"先到者赢"——条件不成立则靠下方
# module 级 autouse fixture 切换，互不污染）。
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
import proxy_core.corpus as CO  # noqa: E402

import routes.proxy as RP  # noqa: E402
from routes.admin import bp as admin_bp  # noqa: E402
from proxy_core.state import (am, activity,  # noqa: E402
                              api_cache,
                              token_stats)

import flask  # noqa: E402

_app = flask.Flask(__name__)
_app.register_blueprint(admin_bp)
_app.register_blueprint(RP.bp)   # ⑲ 入口级用例
client = _app.test_client()

# 本文件用例零真实上游调用（半驱动 + 纯函数 + 端点
# 读单例；⑲ 入口级用例打本文件 ThreadingHTTPServer
# 本地 mock——与 test_stream_failover.py 同款纪律）；
# 其余用例的上游地址指到不可达端口，任何意外调用
# 立即失败而不是打真实端点。
MOCK_UPSTREAM = "http://127.0.0.1:1"


@pytest.fixture(scope="module", autouse=True)
def _use_corpus_tmp():
    """把磁盘路径切到本文件临时目录，并快照/还原
    water 单例内存态（perf/passive/probes 跨测试
    文件共享）。"""
    saved = (S.BASE_DIR, S.CONFIG_FILE, S.ACCOUNTS_FILE,
             S.STATS_FILE, S.WATER_FILE, S.LOG_FILE,
             A.ACCOUNTS_FILE, ST.STATS_FILE, WA.WATER_FILE,
             PV.CONFIG_FILE, WA.LITELLM_API_BASE,
             CO.CORPUS_DIR)
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
    CO.CORPUS_DIR = TMP / "corpus_local"
    token_stats.key_stats.clear()
    yield
    # 先停 worker 线程（join + 排空——防线程
    # 在路径还原后落盘写进仓库数据目录），
    # 再还原磁盘路径
    CO.worker.stop()
    (S.BASE_DIR, S.CONFIG_FILE, S.ACCOUNTS_FILE,
     S.STATS_FILE, S.WATER_FILE, S.LOG_FILE,
     A.ACCOUNTS_FILE, ST.STATS_FILE, WA.WATER_FILE,
     PV.CONFIG_FILE, WA.LITELLM_API_BASE,
     CO.CORPUS_DIR) = saved
    PV._cache = None
    WA.water.perf, WA.water.passive, WA.water.probes = saved_water
    token_stats.key_stats.clear()


@pytest.fixture(autouse=True)
def _reset_corpus():
    """每用例前后：corpus 块复位、water 单例内存态
    清空、worker 队列排空（单例跨用例共享）。"""
    _write_corpus_cfg(enabled=True, sample_rate=1.0,
                      roles=None, include_thinking=True,
                      max_body_tokens=64000,
                      redact_extra=None)
    WA.water.perf.clear()
    WA.water.passive.clear()
    WA.water.probes.clear()
    CO.worker.drain()
    yield
    CO.worker.drain()
    WA.water.perf.clear()
    WA.water.passive.clear()
    WA.water.probes.clear()


# ─── 测试替身/辅助 ─────────────────────────────

def _write_corpus_cfg(**overrides):
    """重写 config.json 的 corpus 块（其余配置
    不动）。"""
    cfg = json.loads(
        (TMP / "config.json").read_text(encoding="utf-8"))
    block = dict(cfg.get("corpus") or {})
    block.update(overrides)
    cfg["corpus"] = block
    (TMP / "config.json").write_text(
        json.dumps(cfg, ensure_ascii=False),
        encoding="utf-8")


def _read_local_samples():
    """读当月 local_YYYYMM.jsonl 全部样本。"""
    month = time.strftime("%Y%m")
    path = CO.CORPUS_DIR / f"local_{month}.jsonl"
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def _wait_for_sample(pred, timeout=10.0):
    """轮询落盘文件，直到出现满足 pred 的样本
    （worker 线程/同步 drain 两条消费路径都等
    到——防异步消费竞态）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        for s in _read_local_samples():
            if pred(s):
                return s
        time.sleep(0.05)
    return None


def _corr(model="GLM-5.3", content="你好"):
    """构造 correlation（capture_correlation 的
    手工等价物——决策表用例不需要真实捕获）。"""
    return {
        "protocol": "openai",
        "messages": [{"role": "user", "content": content}],
        "tools": [],
        "session_id": "m:testsession",
        "client": "127.0.0.1",
    }


def _assistant(content="回复内容", thinking=""):
    return {"content": content, "thinking": thinking,
            "tool_calls": [], "finish": "stop"}


def _sse(obj):
    return ("data: " + json.dumps(obj, ensure_ascii=False)
            + "\n\n").encode("utf-8")


# ─── ① 三协议 correlation 捕获 ─────────────────

def test_01_capture_openai():
    data = {
        "model": "coder",
        "messages": [
            {"role": "system", "content": "你是助手"},
            {"role": "user", "content": "你好"},
        ],
        "tools": [{"type": "function",
                    "function": {"name": "lookup"}}],
    }
    corr = CO.capture_correlation(
        "openai", data, headers=None,
        key_label=None, client="127.0.0.1")
    assert corr is not None
    assert corr["protocol"] == "openai"
    assert corr["messages"] == data["messages"]
    assert corr["tools"] == ["lookup"]
    assert corr["client"] == "127.0.0.1"
    # session_id：affinity 会话键四级 fallback
    # （首条 user 消息哈希）
    assert corr["session_id"].startswith("m:")


def test_01_capture_responses():
    # instructions→system 转换前捕获：instructions
    # 原样入 system 消息，input 原样保留
    data = {
        "model": "GLM-5.3",
        "instructions": "系统提示",
        "input": [{"role": "user", "content": "问题"}],
        "tools": [{"type": "function",
                    "function": {"name": "search"}}],
    }
    corr = CO.capture_correlation(
        "responses", data, client="127.0.0.1")
    assert corr is not None
    assert corr["protocol"] == "responses"
    assert corr["messages"][0] == {"role": "system",
                                    "content": "系统提示"}
    assert corr["messages"][1] == {"role": "user",
                                    "content": "问题"}
    assert corr["tools"] == ["search"]


def test_01_capture_anthropic():
    # anthropic_to_chat 转换前捕获：原始 messages
    # + system（str 形态）
    data = {
        "model": "GLM-5.3",
        "system": "系统提示",
        "messages": [{"role": "user", "content": "你好"}],
        "tools": [{"name": "search", "description": "搜"}],
    }
    corr = CO.capture_correlation(
        "anthropic", data, client="127.0.0.1")
    assert corr is not None
    assert corr["protocol"] == "anthropic"
    assert corr["messages"][0] == {"role": "system",
                                    "content": "系统提示"}
    assert corr["messages"][1] == {"role": "user",
                                    "content": "你好"}
    assert corr["tools"] == ["search"]


def test_01_placeholder_images():
    # 媒体块 → {"type":"text","text":
    # "[media:N,type]"} 占位（R6 项目2：白名单
    # 只放行 text/str；旧实现为顶层图片黑名单
    # + [image:N] 字符串），图片 URL 不入语料；
    # N 全样本连续计数
    data = {
        "model": "GLM-5.3",
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": "看图"},
                {"type": "image_url",
                 "image_url": {"url": "https://x/1.png"}},
                {"type": "image_url",
                 "image_url": {"url": "https://x/2.png"}},
            ],
        }],
    }
    corr = CO.capture_correlation(
        "openai", data, client="127.0.0.1")
    assert corr is not None
    # 混合 content：文本块原样保留（dict），
    # 图片块换占位符（{"type":"text",...}）
    content = corr["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "看图"}
    assert content[1] == {"type": "text",
                          "text": "[media:1,image_url]"}
    assert content[2] == {"type": "text",
                          "text": "[media:2,image_url]"}


def test_01_disabled_zero_overhead():
    # 开关关闭 → capture 返回 None（零开销：
    # 不建 correlation、不遍历 messages）
    _write_corpus_cfg(enabled=False)
    data = {"model": "GLM-5.3",
            "messages": [{"role": "user", "content": "你好"}]}
    assert CO.capture_correlation(
        "openai", data, client="127.0.0.1") is None
    # maybe_collect 同判据：correlation=None →
    # 不产样本
    assert CO.maybe_collect(
        None, _assistant(), uid="9001", model="GLM-5.3",
        stream=False, partial=False) is None
    assert CO.worker.backlog() == 0


# ─── ② _StreamTap 累积 ─────────────────────────

def test_02_stream_tap_accumulate():
    tap = CO._StreamTap()
    tap.feed({"choices": [{"delta": {"content": "你好"}}]})
    tap.feed({"choices": [{"delta": {"reasoning_content": "想了"}}]})
    tap.feed({"choices": [{"delta": {"reasoning": "想"}}]})
    # tool_calls 分片：首片带 id/name，后续片只带
    # arguments 片段（cline.py:553-584 模式独立实现）
    tap.feed({"choices": [{"delta": {"tool_calls": [
        {"index": 0, "id": "call_1", "type": "function",
         "function": {"name": "lookup",
                      "arguments": "{\"a\":"}}]}}]})
    tap.feed({"choices": [{"delta": {"tool_calls": [
        {"index": 0, "function": {"arguments": "1"}}]}}]})
    tap.feed({"choices": [{"delta": {},
                             "finish_reason": "stop"}]})
    a = tap.assistant()
    assert a["content"] == "你好"
    assert a["thinking"] == "想了想"
    assert a["finish"] == "stop"
    assert len(a["tool_calls"]) == 1
    tc = a["tool_calls"][0]
    assert tc["id"] == "call_1"
    assert tc["type"] == "function"
    assert tc["function"]["name"] == "lookup"
    assert tc["function"]["arguments"] == '{"a":1'


# ─── ③ 决策表三分支 ─────────────────────────────

def test_03_decision_partial_dropped():
    # 失败/截断（partial）→ 不产样本
    s = CO.maybe_collect(
        _corr(), _assistant(), uid="9001", model="GLM-5.3",
        stream=True, partial=True)
    assert s is None
    assert CO.worker.backlog() == 0


def test_03_decision_water_suspect_kept_tagged():
    # water_suspect → 保留 + 打标
    WA.water.passive.append({
        "time": "10-03 12:00:00", "key": "9001|GLM-5.3",
        "user_id": "9001", "model": "GLM-5.3",
        "returned": "other-model", "note": "模型名不符"})
    s = CO.maybe_collect(
        _corr(), _assistant(), uid="9001", model="GLM-5.3",
        usage={"prompt_tokens": 3, "completion_tokens": 2,
               "total_tokens": 5},
        stream=True, partial=False)
    assert s is not None
    assert s["water_suspect"] is True
    assert s["usage"]["total"] == 5


def test_03_decision_normal_kept():
    s = CO.maybe_collect(
        _corr(), _assistant(), uid="9002", model="GLM-5.3",
        usage={"prompt_tokens": 3, "completion_tokens": 2,
               "total_tokens": 5},
        ttft_content_ms=120, total_ms=800,
        stream=True, partial=False)
    assert s is not None
    assert s["water_suspect"] is False
    assert s["stream"] is True
    assert s["partial"] is False
    assert s["ttft_content_ms"] == 120
    assert s["total_ms"] == 800
    assert s["redact_hits"] == 0   # 脱敏前（worker 写入）
    assert s["model"] == "GLM-5.3"
    assert s["role"] == "coder"    # roles 配置反查
    assert s["provider"] == "pool"  # 主池模型
    assert s["protocol"] == "openai"
    assert s["session_id"] == "m:testsession"


# ─── ④ sample_rate 采样 ─────────────────────────

def test_04_sample_rate_zero():
    _write_corpus_cfg(sample_rate=0.0)
    assert CO.maybe_collect(
        _corr(), _assistant(), uid="9001", model="GLM-5.3",
        stream=False, partial=False) is None
    assert CO.worker.backlog() == 0


# ─── ⑤ roles 白名单 ────────────────────────────

def test_05_roles_whitelist():
    _write_corpus_cfg(roles=["coder"])
    # GLM-5.3 → 角色 coder（roles 配置
    # {"coder": "GLM-5.3"} 反查）→ 放行
    s = CO.maybe_collect(
        _corr(), _assistant(), uid="9001", model="GLM-5.3",
        stream=False, partial=False)
    assert s is not None
    # KIMI-K3 无角色映射 → role=None 不在白名单
    # → 丢弃
    assert CO.maybe_collect(
        _corr(), _assistant(), uid="9001", model="KIMI-K3",
        stream=False, partial=False) is None


# ─── ⑥ max_body_tokens 超限丢弃 ─────────────────────

def test_06_max_body_tokens():
    _write_corpus_cfg(max_body_tokens=10)
    # 估算（usage 缺省 → chars/2.5）：assistant
    # 100 字符 → 40 token > 10 → 丢弃
    big = _assistant(content="x" * 100)
    assert CO.maybe_collect(
        _corr(), big, uid="9001", model="GLM-5.3",
        stream=False, partial=False) is None
    assert CO.worker.stats()["oversize"] >= 1


# ─── ⑦ 脱敏后落盘 ─────────────────────────────

def test_07_redact_then_persist():
    corr = _corr(content="邮箱 a@b.com，key "
                         "sk-abcdefghijklmnopqrstuvwxyz")
    corr["messages"].append(
        {"role": "assistant", "content": "收到",
         "api_key": "sk-abcdefghijklmnopqrstuvwxyz"})
    s = CO.maybe_collect(
        corr, _assistant(content="脱敏落盘测试 13812345678 已记录"),
        uid="9001", model="GLM-5.3",
        usage={"prompt_tokens": 3, "completion_tokens": 2,
               "total_tokens": 5},
        stream=False, partial=False)
    assert s is not None
    CO.worker.drain()
    # 唯一内容标记定位本用例样本（防旧样本
    # 抢先匹配）
    sample = _wait_for_sample(
        lambda x: x.get("assistant", {}).get("content")
        == "脱敏落盘测试 " + CO.redact.MASK + " 已记录")
    assert sample is not None
    raw = json.dumps(sample, ensure_ascii=False)
    # 脱敏生效：邮箱/token/电话全打码
    assert "a@b.com" not in raw
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in raw
    assert "13812345678" not in raw
    # 敏感键整值抹除（§7.3 字段名结构化探测）
    assert sample["messages"][1]["api_key"] == CO.redact.MASK
    # redact_hits 脱敏后写入（messages 2 + 敏感键
    # 1 + assistant 电话 1 = 4）
    assert sample["redact_hits"] == 4


# ─── ⑧ 本地 id 规则 ───────────────────────────

def test_08_local_id_rule():
    # 复位序号（单例跨用例共享——本用例验证
    # local-YYYYMMDD-序号 规则）
    CO.worker._seq_day = ""
    CO.worker._seq = 0
    day = time.strftime("%Y%m%d")
    for i in (1, 2):
        s = CO.maybe_collect(
            _corr(content=f"id 规则测试样本{i}"),
            _assistant(), uid="9001", model="GLM-5.3",
            stream=False, partial=False)
        assert s is not None
    CO.worker.drain()
    s1 = _wait_for_sample(
        lambda x: x.get("messages")
        and x["messages"][0].get("content")
        == "id 规则测试样本1")
    s2 = _wait_for_sample(
        lambda x: x.get("messages")
        and x["messages"][0].get("content")
        == "id 规则测试样本2")
    assert s1 is not None and s2 is not None
    assert s1["id"] == f"local-{day}-000001"
    assert s2["id"] == f"local-{day}-000002"


# ─── ⑩ seedguard 告警态暂停（⑨ 线程用例置后）───

def test_10_seedguard_alert_pause():
    from proxy_core import seedguard as SG
    saved_alert = SG._state.get("alert")
    saved_ts = CO._last_alert_hint_ts
    # 复位提示节流（防刷屏间隔 600s）
    CO._last_alert_hint_ts = 0.0
    SG._state["alert"] = True
    try:
        # 告警态 → 暂停采集
        assert CO.maybe_collect(
            _corr(), _assistant(), uid="9001",
            model="GLM-5.3",
            stream=False, partial=False) is None
        assert CO.worker.backlog() == 0
        # activity 提示一次（防刷屏）：第一次提示
        n1 = len([e for e in activity.list()
                  if "语料采集暂停" in e["message"]])
        assert n1 >= 1
        # 第二次触发不重复提示
        CO.maybe_collect(
            _corr(), _assistant(), uid="9001",
            model="GLM-5.3",
            stream=False, partial=False)
        n2 = len([e for e in activity.list()
                  if "语料采集暂停" in e["message"]])
        assert n2 == n1
    finally:
        SG._state["alert"] = saved_alert
        CO._last_alert_hint_ts = saved_ts


# ─── ⑪ include_thinking ────────────────────────

def test_11_include_thinking_false():
    _write_corpus_cfg(include_thinking=False)
    s = CO.maybe_collect(
        _corr(), _assistant(content="正文",
                              thinking="秘密思考过程"),
        uid="9001", model="GLM-5.3",
        stream=False, partial=False)
    assert s is not None
    CO.worker.drain()
    sample = _wait_for_sample(
        lambda x: x.get("assistant", {}).get("content")
        == "正文")
    assert sample is not None
    assert sample["assistant"]["thinking"] == ""
    assert sample["assistant"]["content"] == "正文"


# ─── ⑫ /api/corpus/stats 端点 ───────────────────

def test_12_stats_endpoint():
    CO.maybe_collect(
        _corr(), _assistant(), uid="9001", model="GLM-5.3",
        usage={"prompt_tokens": 3, "completion_tokens": 2,
               "total_tokens": 5},
        stream=False, partial=False)
    CO.worker.drain()
    r = client.get("/api/corpus/stats")
    assert r.status_code == 200
    data = r.get_json()
    assert data["enabled"] is True
    assert data["today"]["count"] >= 1
    assert data["month"]["count"] >= 1
    assert data["total"]["count"] >= 1
    assert data["today"]["tokens"] >= 5
    assert "GLM-5.3" in data["by_model"]
    assert "coder" in data["by_role"]
    for k in ("water_suspect", "redact_hits",
              "discarded", "backlog", "oversize"):
        assert isinstance(data[k], int)


# ─── ⑬ _stream_body 半驱动集成 ───────────────────

def test_13_stream_body_integration():
    chunks = [
        _sse({"choices": [{"delta": {"content": "你好"}}]}),
        _sse({"choices": [{"delta": {"reasoning_content":
                                       "思考中"}}]}),
        _sse({"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "call_1", "type": "function",
             "function": {"name": "lookup",
                          "arguments": "{\"a\":"}}]}}]}),
        _sse({"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": "1"}}]}}]}),
        _sse({"choices": [{"delta": {},
                             "finish_reason": "stop"}]}),
        _sse({"usage": {"prompt_tokens": 10,
                          "completion_tokens": 5,
                          "total_tokens": 15}}),
        b"data: [DONE]\n\n",
    ]

    class _FakeResp:
        def __init__(self, chunks):
            self._iter = iter(chunks)

        def iter_bytes(self):
            for chunk in self._iter:
                yield chunk

    class _FakeOpened:
        """_UpstreamOpen 替身：iter_bytes 续读 +
        close（与 test_stats_quality._drive_stream
        同形）。"""

        def __init__(self, chunks):
            self.resp = _FakeResp(chunks)
            self.closed = False

        def close(self):
            self.closed = True

    # 真实 worker 线程消费（异步落盘——与
    # test_09 同款；幂等 start 防重复启动）
    CO.worker.start()
    corr = CO.capture_correlation(
        "openai",
        {"model": "GLM-5.3",
         "messages": [{"role": "user", "content": "你好"}]},
        client="127.0.0.1")
    assert corr is not None
    opened = _FakeOpened(chunks)
    body = RP._stream_body(opened, "GLM-5.3",
                             "9001", time.perf_counter(),
                             None, None, corpus_ctx=corr)
    for _ in body:
        pass
    assert opened.closed
    # 成功收尾（saw_done 且无异常）→ 样本入队
    # → 落盘可见
    sample = _wait_for_sample(
        lambda x: x.get("stream") is True
        and x.get("assistant", {}).get("content")
        == "你好")
    assert sample is not None
    assert sample["assistant"]["thinking"] == "思考中"
    tc = sample["assistant"]["tool_calls"]
    assert len(tc) == 1
    assert tc[0]["function"]["name"] == "lookup"
    assert tc[0]["function"]["arguments"] == '{"a":1'
    assert sample["assistant"]["finish"] == "stop"
    assert sample["usage"] == {"prompt": 10,
                                  "completion": 5,
                                  "cache_read": 0,
                                  "total": 15}
    assert sample["ttft_content_ms"] is not None
    assert sample["model_served"] is None  # 流内无 model 帧


# ─── ⑨ worker 异常不炸（真实线程，置后：线程
#     一旦启动跨用例存活，后续用例的落盘断言
#     已用 _wait_for_sample 轮询防竞态）────────

def test_09_worker_exception_survives():
    CO.worker.start()
    # 毒样本：messages 内含不可 JSON 序列化对象
    # → _process 抛错
    CO.worker.submit({
        "id": "", "model": "GLM-5.3",
        "messages": [{"role": "user", "content": object()}],
        "assistant": {"content": "毒样本"},
    })
    # 好样本：worker 线程吞掉毒样本异常后仍落盘
    CO.maybe_collect(
        _corr(content="异常不炸测试样本"),
        _assistant(), uid="9001", model="GLM-5.3",
        stream=False, partial=False)
    sample = _wait_for_sample(
        lambda x: x.get("messages")
        and x["messages"][0].get("content")
        == "异常不炸测试样本")
    assert sample is not None
    assert CO.worker.stats()["discarded"] >= 1


# ─── ⑭ 媒体块白名单占位（R6 项目2）──────────────

def test_14_media_tool_result_base64_recursive():
    # tool_result.content 内嵌 base64 图片 → 递归
    # 占位（旧实现只处理顶层 content 图片黑名单）
    data = {
        "model": "GLM-5.3",
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": "看日志"},
                {"type": "tool_result",
                 "tool_use_id": "call_1",
                 "content": [
                     {"type": "text", "text": "日志如下"},
                     {"type": "image",
                      "source": {"type": "base64",
                                 "media_type": "image/png",
                                 "data": "iVBORw0KGgo="}},
                 ]},
            ],
        }],
    }
    corr = CO.capture_correlation(
        "openai", data, client="127.0.0.1")
    assert corr is not None
    content = corr["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "看日志"}
    tr = content[1]
    assert tr["type"] == "tool_result"
    assert tr["tool_use_id"] == "call_1"
    inner = tr["content"]
    assert inner[0] == {"type": "text", "text": "日志如下"}
    assert inner[1] == {"type": "text",
                        "text": "[media:1,image]"}
    # base64 载荷不入语料
    assert "iVBORw0KGgo=" not in json.dumps(
        corr["messages"], ensure_ascii=False)


def test_14_media_document_placeholder():
    # document PDF 块 → 占位（白名单只放行 text）
    data = {
        "model": "GLM-5.3",
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": "读文档"},
                {"type": "document",
                 "source": {"type": "base64",
                            "media_type": "application/pdf",
                            "data": "JVBERi0xLjQ="}},
            ],
        }],
    }
    corr = CO.capture_correlation(
        "openai", data, client="127.0.0.1")
    assert corr is not None
    content = corr["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "读文档"}
    assert content[1] == {"type": "text",
                          "text": "[media:1,document]"}
    assert "JVBERi0xLjQ=" not in json.dumps(
        corr["messages"], ensure_ascii=False)


# ─── ⑮ meta 字段脱敏（R6 项目1）────────────────

def test_15_meta_client_session_redacted():
    # client（来源 IP 明文）与 session_id（affinity
    # 四级 fallback 可嵌 IP/key 名——
    # affinity.py:103-143）落盘前各过一遍文本
    # 规则——IP/key 名不出现在落盘样本
    corr = {
        "protocol": "openai",
        "messages": [{"role": "user",
                       "content": "meta 脱敏探针"}],
        "tools": [],
        "session_id": "k:sk-abcdefghijklmnopqrstuvwxyz",
        "client": "10.0.0.5",
    }
    s = CO.maybe_collect(
        corr, _assistant(content="meta 脱敏回复"),
        uid="9001", model="GLM-5.3",
        stream=False, partial=False)
    assert s is not None
    CO.worker.drain()
    sample = _wait_for_sample(
        lambda x: x.get("messages")
        and x["messages"][0].get("content")
        == "meta 脱敏探针")
    assert sample is not None
    raw = json.dumps(sample, ensure_ascii=False)
    # 内网 IP 与 key 名（sk- 前缀 token）均
    # 不出现在落盘样本
    assert "10.0.0.5" not in raw
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in raw
    assert sample["client"] == CO.redact.MASK
    assert sample["session_id"] == "k:" + CO.redact.MASK


# ─── ⑯ tool_calls.arguments 结构化脱敏（R6 项目6）

def test_16_tool_arguments_structured_redaction():
    # arguments 是 JSON 字符串——键名探测对字符串
    # 失效：解析后结构化打掉 password 键（回落
    # 路径只过文本正则，打不掉键名语义）
    asst = {
        "content": "调工具",
        "thinking": "",
        "tool_calls": [{
            "id": "call_1",
            "type": "function",
            "function": {
                "name": "lookup",
                "arguments": json.dumps(
                    {"password": "明文密码",
                     "query": "正常参数"},
                    ensure_ascii=False),
            },
        }],
        "finish": "stop",
    }
    s = CO.maybe_collect(
        _corr(content="工具调用探针"), asst,
        uid="9001", model="GLM-5.3",
        stream=False, partial=False)
    assert s is not None
    CO.worker.drain()
    sample = _wait_for_sample(
        lambda x: x.get("assistant", {}).get("content")
        == "调工具")
    assert sample is not None
    tc = sample["assistant"]["tool_calls"]
    assert len(tc) == 1
    args = json.loads(tc[0]["function"]["arguments"])
    assert args["password"] == CO.redact.MASK
    assert args["query"] == "正常参数"
    # 明文密码不入盘
    assert "明文密码" not in json.dumps(
        sample, ensure_ascii=False)


# ─── ⑰ _StreamTap 字符软顶（R6 项目7）────────────

def test_17_tap_char_cap():
    # 累积超 _TAP_CHAR_CAP（20 万字符）即停累积
    # 并置 truncated；样本 meta 记 body_truncated
    tap = CO._StreamTap()
    chunk = "x" * 1000
    for _ in range(210):     # 21 万字符 > 20 万
        tap.feed({"choices": [{"delta": {
            "content": chunk}}]})
    assert tap.truncated is True
    assert len("".join(tap.content_parts)) \
        == CO._TAP_CHAR_CAP
    # 触顶后继续喂不再累积
    tap.feed({"choices": [{"delta": {
        "content": "y" * 10}}]})
    assert len("".join(tap.content_parts)) \
        == CO._TAP_CHAR_CAP
    # body_truncated 经 maybe_collect 落入样本 meta
    s = CO.maybe_collect(
        _corr(content="软顶探针"),
        _assistant(content="软顶回复"),
        uid="9001", model="GLM-5.3",
        stream=True, partial=False,
        body_truncated=tap.truncated)
    assert s is not None
    assert s["body_truncated"] is True
    CO.worker.drain()
    sample = _wait_for_sample(
        lambda x: x.get("messages")
        and x["messages"][0].get("content")
        == "软顶探针")
    assert sample is not None
    assert sample["body_truncated"] is True


# ─── ⑱ worker 开关复检（R6 项目8）────────────────

def test_18_worker_switch_off_discard():
    # 入队后运营者关开关 → worker 处理时丢弃+
    # 计数，排空 backlog 不再落新盘。
    # 先停后台 worker 线程（test_13/09 启动的
    # 模块单例——不停则线程抢在关开关前消费
    # 探针，竞态）；结束恢复线程（_started
    # 复位 + _stop 清位 + 重启，start 幂等）
    CO.worker.stop()
    try:
        s = CO.maybe_collect(
            _corr(content="开关复检探针"),
            _assistant(content="开关复检回复"),
            uid="9001", model="GLM-5.3",
            stream=False, partial=False)
        assert s is not None
        assert CO.worker.backlog() == 1
        # 运营者关开关（入队之后）
        _write_corpus_cfg(enabled=False)
        n = CO.worker.drain()
        assert n == 1
        assert CO.worker.stats()["discarded"] >= 1
        # 不落新盘：落盘文件无本探针样本
        for p in CO.CORPUS_DIR.glob("local_*.jsonl"):
            for line in p.read_text(
                    encoding="utf-8").splitlines():
                if line.strip():
                    rec = json.loads(line)
                    assert rec.get("messages")[0].get(
                        "content") != "开关复检探针"
    finally:
        CO.worker._started = False
        CO.worker._stop.clear()
        CO.worker.start()


# ─── ⑲ 入口级关闭逐字节一致（R6 项目10）──────────
# mock 上游（test_stream_failover.py 同款：
# ThreadingHTTPServer + 临时端口；三入口统一
# 转到上游 /v1/chat/completions）。corpus 关闭
# 时响应体逐字节恒定（corpus 管线不改响应——
# test_channel_gate.py:625 同款门关断言）。

_GOLDEN_CHAT_STREAM = (
    'data: {"model":"GLM-5.3","choices":'
    '[{"delta":{"content":"he"}}]}\n\n'
    'data: {"model":"GLM-5.3","choices":'
    '[{"delta":{"content":"llo"}}],'
    '"usage":{"prompt_tokens":11,'
    '"completion_tokens":2,"total_tokens":13}}\n\n'
    'data: [DONE]\n\n'
)
_GOLDEN_RESPONSES = (
    '{"created_at":null,'
    '"id":"resp_msg_0123456789abcdef",'
    '"model":"GLM-5.3","object":"response",'
    '"output":[{"content":[{"annotations":[],'
    '"text":"hello","type":"output_text"}],'
    '"id":"msg_0","role":"assistant",'
    '"status":"completed","type":"message"}],'
    '"output_text":"hello","status":"completed",'
    '"usage":{"input_tokens":11,"output_tokens":2,'
    '"total_tokens":13}}\n'
)
_GOLDEN_MESSAGES = (
    '{"content":[{"text":"hello","type":"text"}],'
    '"id":"msg_0123456789abcdef","model":"GLM-5.3",'
    '"role":"assistant","stop_reason":"end_turn",'
    '"stop_sequence":null,"type":"message",'
    '"usage":{"input_tokens":11,"output_tokens":2}}\n'
)


class _EntryHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *args):
        pass

    def do_POST(self):  # noqa: N802
        raw = self.rfile.read(
            int(self.headers.get("content-length") or 0))
        req = json.loads(raw.decode("utf-8"))
        if not req.get("stream"):
            body = json.dumps({
                "id": "msg_0123456789abcdef",
                "model": "GLM-5.3",
                "choices": [{"index": 0,
                             "message": {"role": "assistant",
                                         "content": "hello"},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 11,
                          "completion_tokens": 2,
                          "total_tokens": 13},
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type",
                             "application/json")
            self.send_header("Content-Length",
                             str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(200)
        self.send_header("Content-Type",
                         "text/event-stream")
        self.end_headers()
        for line in (
            'data: {"model":"GLM-5.3","choices":'
            '[{"delta":{"content":"he"}}]}\n\n',
            'data: {"model":"GLM-5.3","choices":'
            '[{"delta":{"content":"llo"}}],'
            '"usage":{"prompt_tokens":11,'
            '"completion_tokens":2,"total_tokens":13}}\n\n',
            "data: [DONE]\n\n",
        ):
            self.wfile.write(line.encode("utf-8"))
            self.wfile.flush()


_entry_srv = ThreadingHTTPServer(("127.0.0.1", 0),
                                 _EntryHandler)
_entry_srv.daemon_threads = True
threading.Thread(target=_entry_srv.serve_forever,
                 daemon=True).start()
ENTRY_MOCK = (f"http://127.0.0.1:"
              f"{_entry_srv.server_address[1]}")


@pytest.fixture()
def _entry_upstream():
    """上游指向本文件 mock（免真实网络）+ 免换
    key + 账号/冷却状态复位；结束还原（防跨
    测试文件污染）。"""
    saved = (RP.LITELLM_API_BASE,
             WA.LITELLM_API_BASE, api_cache.get)
    RP.LITELLM_API_BASE = ENTRY_MOCK
    WA.LITELLM_API_BASE = ENTRY_MOCK
    api_cache.get = lambda acc: f"key-{acc['user_id']}"
    am.index = 0
    api_cache.cache.clear()
    RP._prov_quota_cooldown.clear()
    for a in am.accounts:
        a["enabled"] = True
        a["budget_exceeded"] = False
        a["budget_cooldown_until"] = 0
        a["cooldown_reason"] = ""
        a["quota_team"] = ""
        a["group"] = ""
    yield
    (RP.LITELLM_API_BASE, WA.LITELLM_API_BASE,
     api_cache.get) = saved


def test_19_entry_chat_disabled_byte_identical(
        _entry_upstream):
    # chat 入口（流式）：corpus 关闭 → 响应体
    # 逐字节恒等于 golden（上游 SSE 原样透传）
    _write_corpus_cfg(enabled=False)
    r = client.post("/v1/chat/completions", json={
        "model": "GLM-5.3", "stream": True,
        "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200, r.status_code
    assert r.get_data(as_text=True) == _GOLDEN_CHAT_STREAM


def test_19_entry_responses_disabled_byte_identical(
        _entry_upstream):
    # responses 入口（非流式）：corpus 关闭 →
    # 响应体逐字节恒等于 golden
    _write_corpus_cfg(enabled=False)
    r = client.post("/v1/responses", json={
        "model": "GLM-5.3", "stream": False,
        "instructions": "系统提示", "input": "问题"})
    assert r.status_code == 200, r.status_code
    assert r.get_data(as_text=True) == _GOLDEN_RESPONSES


def test_19_entry_messages_disabled_byte_identical(
        _entry_upstream):
    # messages 入口（非流式）：corpus 关闭 →
    # 响应体逐字节恒等于 golden
    _write_corpus_cfg(enabled=False)
    r = client.post("/v1/messages", json={
        "model": "GLM-5.3", "stream": False,
        "system": "系统提示",
        "messages": [{"role": "user",
                      "content": "问题"}]})
    assert r.status_code == 200, r.status_code
    assert r.get_data(as_text=True) == _GOLDEN_MESSAGES
