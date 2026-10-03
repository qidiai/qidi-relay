# -*- coding: utf-8 -*-
"""T9 backlog sweep（方案 §13）新用例：P1/P2/P3 八项落地的
独立验证。全程本地 mock（127.0.0.1，零真实网络），
路径常量注入 TMP（方案 §7.7 纪律），不碰真实数据文件。

覆盖（每项独立成节，与派工 T9 八节一一对应）：
① key label 撞名核算（P1-1）：显式同 label 去重 +
   缺省 label（前 12 位截断）撞名同规则；
② est/used cost 拆输入输出口径（P1-2）：record_key
   拆记 + used 成本四参 cost_of + 旧条目回落；
③ 同行 json.loads 共享（P2-3）：复合帧三消费者
   （usage/模型名/TTFT）共享一次解析全提取；
⑤ >500MB/月语料自动降采样（P2-5）：大文件生效采样率
   封顶 0.3 + 小文件保持配置值 + stats 曝光；
⑥ last_request.json 互斥警示（P2-6）：限频只提示一次；
⑦ peek 期 registry 登记（P3-7）：判负 finish("gate")
   无残留 rid + 判健康 finish("peek")；
⑧ nits（P3-8）：CONFIG_DUPLICATE_KEY_WARNINGS 整体替换
   + 完全重复键告警文案去冗余。

注：P2-4（真 luhn/身份证校验位）与派工规格存在冲突
（既有 test_redact.py 正例 fixture 为 luhn-无效卡号），
已报 SPEC GAP 交指挥官裁决，本文件不含其用例。

运行：py -3.12 -m pytest tests/test_t9_backlog.py -v
"""

import json
import logging
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
os.environ.setdefault("CODELY_CLI_VERSION", "0.0.0-test")

TMP = pathlib.Path(tempfile.mkdtemp(prefix="qidi-t9-test-"))
(TMP / "config.json").write_text(json.dumps({
    "port": 0,
    "features": {"affinity": False},
    "pool_models": ["GLM-5.3"],
    "model_groups": {},
    "providers": {},
    "roles": {},
    "vision_model": "GLM-5.3",
    "vision_capable_models": [],
    # P1-2：模型 A 输入价 1 / 输出价 3 元每百万 token
    "pricing": {"A": {"input": 1, "output": 3},
                "GLM-5.3": {"input": 4, "output": 16}},
    "embedding_upstream": {"base_url": None, "timeout": 5},
}), encoding="utf-8")
(TMP / "accounts.json").write_text(json.dumps({"accounts": [
    {"user_id": uid, "username": f"t{uid}", "org_id": "0",
     "access_token": "a.b.c", "enabled": True,
     "has_glm53": False, "budget_exceeded": False, "group": ""}
    for uid in ("9001", "9002", "9003")]}), encoding="utf-8")

# 路径重定向（与 test_stream_sniff.py 同纪律：全量收集时
# proxy_core.state 已被更早文件实例化 → import 时覆写无效，
# 靠下方 module 级 autouse fixture 切换；单独运行本文件时
# state 尚未实例化，必须在 import routes.proxy 前指到 TMP）
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
from proxy_core import corpus as CO  # noqa: E402
from proxy_core import budget as BUDGET  # noqa: E402

import routes.proxy as RP  # noqa: E402
from proxy_core.state import am, api_cache, token_stats  # noqa: E402
from proxy_core.keystore import session_pool  # noqa: E402

api_cache.get = lambda acc: f"key-{acc['user_id']}"      # 免向上游换 key

from flask import Flask  # noqa: E402

_app = Flask(__name__)
_app.register_blueprint(RP.bp)
client = _app.test_client()

# ─── mock 上游（仅 "200" 流式/非流式两种形态）──────────────

HITS = []
BEHAVIOR = {}


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
            if not req.get("stream"):
                body = json.dumps({
                    "model": "GLM-5.3",
                    "choices": [{"message": {"role": "assistant",
                                             "content": "hello"},
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

TODAY = time.strftime("%Y-%m-%d")


@pytest.fixture(scope="module", autouse=True)
def _use_t9_tmp():
    """把上游与磁盘路径切到本文件的临时目录（用例后还原）。

    与 test_stream_sniff.py 的 fixture 同构：全量收集时
    import 阶段的覆写"后到者赢"，本 fixture 保证本文件
    用例期间全局指向本文件 mock/TMP，用例结束还原。"""
    saved = (S.BASE_DIR, S.CONFIG_FILE, S.ACCOUNTS_FILE,
             S.STATS_FILE, S.WATER_FILE, S.LOG_FILE,
             A.ACCOUNTS_FILE, ST.STATS_FILE, WA.WATER_FILE,
             PV.CONFIG_FILE, RP.LITELLM_API_BASE, WA.LITELLM_API_BASE)
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
    RP.LITELLM_API_BASE = MOCK
    WA.LITELLM_API_BASE = MOCK
    token_stats.key_stats.clear()
    BUDGET._inflight.clear()
    yield
    (S.BASE_DIR, S.CONFIG_FILE, S.ACCOUNTS_FILE,
     S.STATS_FILE, S.WATER_FILE, S.LOG_FILE,
     A.ACCOUNTS_FILE, ST.STATS_FILE, WA.WATER_FILE,
     PV.CONFIG_FILE, RP.LITELLM_API_BASE, WA.LITELLM_API_BASE) = saved
    PV._cache = None
    WA.water.perf, WA.water.passive, WA.water.probes = saved_water
    token_stats.key_stats.clear()
    BUDGET._inflight.clear()


@pytest.fixture(autouse=True)
def _reset():
    """每用例前清空内存记账/在途表/水检内存态（单例跨用例共享）。"""
    HITS.clear()
    BEHAVIOR.clear()
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
    token_stats.key_stats.clear()
    token_stats.stats.clear()
    BUDGET._inflight.clear()
    WA.water.perf.clear()
    WA.water.passive.clear()
    WA.water.probes.clear()
    yield
    token_stats.key_stats.clear()
    token_stats.stats.clear()
    BUDGET._inflight.clear()
    WA.water.perf.clear()
    WA.water.passive.clear()
    WA.water.probes.clear()


# ─── ① P1-1 key label 撞名核算 ─────────────────────────────

def test_api_key_label_collision_deduped():
    """①两条目同 label → 解析期去重命名（首个保持原名，
    后续加 #2/#3 后缀），label 唯一且可区分，确定性
    （两次解析结果一致）。"""
    raw = [{"key": "sk-aaaa" + "1" * 20, "label": "撞名"},
           {"key": "sk-bbbb" + "2" * 20, "label": "撞名"},
           {"key": "sk-cccc" + "3" * 20, "label": "撞名"}]
    out1 = S._parse_api_keys(raw)
    out2 = S._parse_api_keys(raw)
    labels = [v["label"] for v in out1.values()]
    assert labels == ["撞名", "撞名#2", "撞名#3"]
    assert len(set(labels)) == 3          # 同一 label 唯一映射一个 key
    assert [v["label"] for v in out2.values()] == labels  # 确定性


def test_api_key_default_label_collision_deduped():
    """①缺省 label（key 前 12 位截断）撞名同规则——
    两个长 key 前 12 位相同 → 后者加 #2 后缀。"""
    prefix = "sk-sameprefix"              # 14 位 > 12 → 缺省 label 截断
    raw = [prefix + "aaaaaaaaaaaa",
           prefix + "bbbbbbbbbbbb"]
    out = S._parse_api_keys(raw)
    labels = [v["label"] for v in out.values()]
    assert labels[0] == prefix[:12] + "…"
    assert labels[1] == prefix[:12] + "…#2"
    assert labels[0] != labels[1]


# ─── ② P1-2 est/used cost 拆输入输出口径 ───────────────────

def test_record_key_splits_prompt_completion():
    """②record_key 拆记 prompt/completion（从 usage 提取，
    缺失回落 0）；get_key_stats 暴露拆分，旧条目无新字段
    时回落 p=total, c=0。"""
    token_stats.record_key("拆分甲", "A", {"prompt_tokens": 7,
                                          "completion_tokens": 3,
                                          "total_tokens": 10})
    token_stats.record_key("拆分甲", "A", {"total_tokens": 5})  # 缺 p/c → +0
    rows = {r["label"]: r for r in token_stats.get_key_stats()}
    assert rows["拆分甲"]["prompt_tokens"] == 7
    assert rows["拆分甲"]["completion_tokens"] == 3
    assert rows["拆分甲"]["total_tokens"] == 15
    # 旧条目（升级前 JSON，无拆分字段）回落：p=total, c=0
    token_stats.key_stats["旧条目乙"] = {
        "date": TODAY, "total_tokens": 9, "requests": 1,
        "models": {}, "cache_tokens": 0}
    rows = {r["label"]: r for r in token_stats.get_key_stats()}
    assert rows["旧条目乙"]["prompt_tokens"] == 9
    assert rows["旧条目乙"]["completion_tokens"] == 0


def test_used_cost_split_input_output():
    """②used 成本按拆记口径折算：pricing A(in=1, out=3)，
    p=100, c=200 → cost = 100*1e-6 + 200*3e-6 = 7e-4
    （输出 token 按输出价，不再被输入价稀释）。"""
    token_stats.record_key("拆分丙", "A", {"prompt_tokens": 100,
                                          "completion_tokens": 200,
                                          "total_tokens": 300})
    used_tokens, used_cost = BUDGET._used_for(
        "拆分丙", {"cache_reads": False})
    assert used_tokens == 300
    assert used_cost == pytest.approx(100 / 1e6 * 1 + 200 / 1e6 * 3)


def test_used_cost_old_entry_fallback():
    """②旧条目回落：p=total, c=0——成本全部按输入价折算
    （与升级前口径逐字节一致）。"""
    token_stats.key_stats["旧条目丁"] = {
        "date": TODAY, "total_tokens": 300, "requests": 1,
        "models": {"A": 300}, "cache_tokens": 0}
    used_tokens, used_cost = BUDGET._used_for(
        "旧条目丁", {"cache_reads": False})
    assert used_tokens == 300
    assert used_cost == pytest.approx(300 / 1e6 * 1)


# ─── ③ P2-3 同行 json.loads 共享 ───────────────────────────

def test_consume_chunk_composite_frame_shared_parse():
    """③一行含 usage+model+content 的复合帧——三消费者
    （usage/模型名/TTFT）共享一次 JSON 解析，全部正确
    提取（防共享漏提取）。

    观测面（半驱动 _stream_body，学 test_stats_quality⑭）：
    - usage → token_stats.record 记账；
    - 模型名 → water.observe 分类（请求 GLM-5.3 vs 返回
      GLM-5.3-Flash → mismatch → 被动事件 recorded）；
    - content delta → 双口径 TTFT 采集（同帧提取）。"""
    class _FakeResp:
        def __init__(self, chunks):
            self._iter = iter(chunks)

        def iter_bytes(self):
            return self._iter

    class _FakeOpened:
        def __init__(self, chunks):
            self.resp = _FakeResp(chunks)
            self.closed = False

        def close(self):
            self.closed = True

    # 复合帧：model + content delta + usage 同在一行
    frame = ('data: {"model":"GLM-5.3-Flash","choices":'
             '[{"delta":{"content":"hi"}}],'
             '"usage":{"prompt_tokens":5,"completion_tokens":'
             '6,"total_tokens":11}}\n\n')
    chunks = [frame.encode("utf-8"), b"data: [DONE]\n\n"]
    uid = "uid-t9-cmp"
    # started 回拨 50ms：内存迭代器瞬时完成，否则
    # ttft_ms/total_ms 均为 0ms → water.observe 的
    # perf 建桶条件（usage and (ttft_ms or total_ms)）
    # 不成立（真实网络请求恒 >1ms）
    body = RP._stream_body(_FakeOpened(chunks), "GLM-5.3", uid,
                           time.perf_counter() - 0.05, None, None)
    for _ in body:
        pass
    # ① usage 提取 → 记账
    e = token_stats.stats.get(f"{uid}|GLM-5.3")
    assert e is not None
    assert e["prompt_tokens"] == 5
    assert e["completion_tokens"] == 6
    assert e["total_tokens"] == 11
    # ② 模型名提取 → 注水检测 mismatch 被动事件
    assert WA.water.passive, "模型名未提取（mismatch 事件缺失）"
    assert WA.water.passive[-1]["returned"] == "GLM-5.3-Flash"
    assert WA.water.passive[-1]["key"] == f"{uid}|GLM-5.3"
    # ③ TTFT 提取（同帧 content delta）→ 双口径桶有值
    p = WA.water.perf.get(f"{uid}|GLM-5.3")
    assert p is not None and p.get("ttft_content"), \
        "TTFT 未从复合帧提取"


# ─── ⑤ P2-5 >500MB/月语料自动降采样 ────────────────────────

def _write_corpus_cfg(enabled: bool, sample_rate: float):
    cfg = json.loads((TMP / "config.json").read_text(encoding="utf-8"))
    cfg["corpus"] = {"enabled": enabled, "sample_rate": sample_rate}
    (TMP / "config.json").write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def test_big_month_file_caps_effective_sample_rate(tmp_path, monkeypatch):
    """⑤月度文件超阈值（注入 TMP：大文件）→ 生效采样率
    封顶 0.3（配置值 1.0 也被封顶）；stats 曝光
    month_size_bytes 与 sample_rate_effective。"""
    corpus_dir = tmp_path / "corpus_local"
    corpus_dir.mkdir()
    monkeypatch.setattr(CO, "CORPUS_DIR", corpus_dir)
    monkeypatch.setattr(CO, "_MAX_MONTH_BYTES", 8)  # 注入"大文件"阈值
    month = time.strftime("%Y%m")
    f = corpus_dir / f"local_{month}.jsonl"
    f.write_text("x" * 64, encoding="utf-8")        # 64B > 8B → 超限
    _write_corpus_cfg(True, 1.0)
    cfg = CO._corpus_cfg()
    assert cfg["sample_rate"] == 1.0
    assert cfg["sample_rate_effective"] == CO._BIG_MONTH_SAMPLE_CAP
    st = CO.worker.stats()
    assert st["month_size_bytes"] == 64
    assert st["sample_rate_effective"] == CO._BIG_MONTH_SAMPLE_CAP


def test_small_month_file_keeps_config_sample_rate(tmp_path, monkeypatch):
    """⑤小文件（远低于 500MB 常量）→ 生效采样率保持
    配置值，不封顶。"""
    corpus_dir = tmp_path / "corpus_local"
    corpus_dir.mkdir()
    monkeypatch.setattr(CO, "CORPUS_DIR", corpus_dir)
    month = time.strftime("%Y%m")
    (corpus_dir / f"local_{month}.jsonl").write_text(
        "x", encoding="utf-8")
    _write_corpus_cfg(True, 0.7)
    cfg = CO._corpus_cfg()
    assert cfg["sample_rate"] == 0.7
    assert cfg["sample_rate_effective"] == 0.7
    st = CO.worker.stats()
    assert st["month_size_bytes"] == 1
    assert st["sample_rate_effective"] == 0.7


# ─── ⑥ P2-6 last_request.json 互斥警示 ─────────────────────

def test_debug_last_request_warn_once_rate_limited(caplog):
    """⑥互斥警示限频——连调两次只产生一条 warning
    （600s 窗口内至多一次）。"""
    RP._last_debug_last_req_hint_ts = 0.0
    with caplog.at_level(logging.WARNING, logger="qidi-relay"):
        RP._debug_last_request_warn_once()
        RP._debug_last_request_warn_once()
    msgs = [r.getMessage() for r in caplog.records
            if "debug_last_request" in r.getMessage()]
    assert len(msgs) == 1
    assert "互斥" in msgs[0] and "仅限本地排查" in msgs[0]


# ─── ⑦ P3-7 peek 期 registry 登记 ──────────────────────────

def _reset_gate(behavior):
    HITS.clear()
    BEHAVIOR.clear()
    BEHAVIOR.update(behavior)
    am.index = 0
    api_cache.cache.clear()
    for a in am.accounts:
        a["enabled"] = True
        a["budget_exceeded"] = False
        a["budget_cooldown_until"] = 0
        a["cooldown_reason"] = ""
        a["quota_team"] = ""
        a["group"] = ""


def _post_gate(stream=True):
    return client.post("/v1/chat/completions", json={
        "model": "GLM-5.3", "stream": stream,
        "messages": [{"role": "user", "content": "hi"}]})


def test_peek_bad_registers_and_finishes_gate_no_residual(monkeypatch):
    """⑦peek 判负路径：peek 调用前 registry.register 登记
    （uid=账号 load key，stream=True），判负时
    finish(rid, "gate") 后换号——registry 无残留 rid。"""
    orig_register, orig_finish = (RP.registry.register,
                                  RP.registry.finish)
    regs, fins = [], []

    def _spy_register(model, user_id, stream=False):
        rid = orig_register(model, user_id, stream=stream)
        regs.append((rid, model, user_id, stream))
        return rid

    def _spy_finish(rid, status="done"):
        fins.append((rid, status))
        return orig_finish(rid, status)

    monkeypatch.setattr(RP.registry, "register", _spy_register)
    monkeypatch.setattr(RP.registry, "finish", _spy_finish)

    def _fake_peek(*args, **kwargs):
        return ("bad", [], "peek_test")

    monkeypatch.setattr(RP, "_peek_stream", _fake_peek)
    _reset_gate({f"key-{u}": "200" for u in ("9001", "9002", "9003")})
    r = _post_gate()
    assert r.status_code == 502          # 三次 attempt 全判负
    # 每账号一次 attempt：peek 前登记（uid=账号 user_id，stream=True）
    peek_regs = [x for x in regs if x[2] in ("9001", "9002", "9003")]
    assert len(peek_regs) == 3
    assert all(x[3] is True for x in peek_regs)
    # 登记的 model 是守卫改写后的模型：请求 GLM-5.3 被
    # 模型守卫无条件改写为 codely-core（proxy.py:1243-1247，
    # 上游收回直连权限）——peek 登记发生在改写之后
    assert all(x[1] == "codely-core" for x in peek_regs)
    # 判负 rid 全部以 "gate" 终结
    gate_fins = {rid for rid, st in fins if st == "gate"}
    assert {x[0] for x in peek_regs} <= gate_fins
    # 无残留：registry 无该批账号的进行中条目
    assert not [x for x in RP.registry.inflight()
                if x["user_id"] in ("9001", "9002", "9003")]


def test_peek_healthy_finishes_peek_rid_before_stream(monkeypatch):
    """⑦peek 判健康路径：立即 finish(rid, "peek") 再
    return Response（吐流阶段由 _stream_body 自己的 rid
    登记——"done" 与 "peek" 并存，不双重计数），客户端
    收到完整 SSE，registry 无残留。"""
    orig_register, orig_finish = (RP.registry.register,
                                  RP.registry.finish)
    regs, fins = [], []

    def _spy_register(model, user_id, stream=False):
        rid = orig_register(model, user_id, stream=stream)
        regs.append((rid, model, user_id, stream))
        return rid

    def _spy_finish(rid, status="done"):
        fins.append((rid, status))
        return orig_finish(rid, status)

    monkeypatch.setattr(RP.registry, "register", _spy_register)
    monkeypatch.setattr(RP.registry, "finish", _spy_finish)

    def _fake_peek(*args, **kwargs):
        return ("ok", [], "")

    monkeypatch.setattr(RP, "_peek_stream", _fake_peek)
    _reset_gate({"key-9001": "200"})
    r = _post_gate()
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert '"content":"he"' in body and '"content":"llo"' in body
    assert "data: [DONE]" in body
    # peek rid 以 "peek" 终结；吐流 rid 以 "done" 终结
    assert any(st == "peek" for _, st in fins)
    assert any(st == "done" for _, st in fins)
    # 无残留
    assert not [x for x in RP.registry.inflight()
                if x["user_id"] == "9001"]


# ─── ⑧ P3-8 nits ───────────────────────────────────────────

def _write_raw_config(text: str):
    (TMP / "config.json").write_text(text, encoding="utf-8")


def test_dup_warnings_atomic_full_replace():
    """⑧CONFIG_DUPLICATE_KEY_WARNINGS 先收集局部表、成功
    后整体替换——第二次加载（无重复键）后旧告警不残留
    （并发读者不见陈旧表/半表）。"""
    _write_raw_config('{"port": 0, "pool_models": ["GLM-5.3"], '
                      '"pricing": {"a": {"input": 1}, '
                      '"A": {"input": 2}}}')
    S.load_config()
    assert S.CONFIG_DUPLICATE_KEY_WARNINGS
    _write_raw_config('{"port": 0, "pool_models": ["GLM-5.3"], '
                      '"pricing": {"a": {"input": 1}}}')
    S.load_config()
    assert S.CONFIG_DUPLICATE_KEY_WARNINGS == []


def test_dup_warning_identical_spelling_message():
    """⑧两处拼写完全相同的重复键——告警明示重复出现
    （不再发冗余的 "X 与 X" 文案）。"""
    # 原始 JSON 文本（json.dumps 会折叠重复键，必须手写）
    _write_raw_config('{"port": 0, "pool_models": ["GLM-5.3"], '
                      '"pricing": {"dup": {"input": 1}, '
                      '"dup": {"input": 2}}}')
    S.load_config()
    ws = S.CONFIG_DUPLICATE_KEY_WARNINGS
    assert len(ws) == 1
    assert "pricing.dup" in ws[0]
    assert "重复出现" in ws[0] and "两处拼写完全相同" in ws[0]
    assert " 与 " not in ws[0]
