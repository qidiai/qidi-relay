# -*- coding: utf-8 -*-
"""P1-3：by_key ≡ by_account 同源不变量 + 三类边界（夹具来自 natie §14/§15）。

    python tests/_test_analytics_parity.py

口径（natie 实测，照抄即可）：
  · parity 只比 requests / 各 token / credit —— models 是「前 5 名截断」的展示字段，求和必假失败；
  · error 行 by_key 与 by_account **两侧都跳过**；count_usage_rows() 是「非空文本行数」
    （不解析 JSON，含 error 行与坏 JSON 行），不能当成功请求数；
  · 窗口按 row["at"]（epoch）过滤，**完全不看 iso**；缺 at 的行被当 0 → 夹具必须给 at；
  · 边界是闭区间（at==since / at==until 都保留）；
  · **桶数不等是正常语义**（一个 Key 可打多个账号 → keys < accounts），只比合计；
  · 一律用固定夹具（真实日志仍在追加 error 行，合计类断言用真实日志会漂移）。
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _suite_head import *  # noqa: F401,F403

P, A = bootstrap()
declare_suite(__file__, ["qoder_proxy.py"])

print("[analytics-parity] by_key ≡ by_account 同源不变量")


def _fixture(rows, tag):
    """把 rows 写进临时 usage.jsonl，返回 (日志路径, 临时目录)。"""
    d = tempfile.mkdtemp(prefix="qd-%s-" % tag)
    log = os.path.join(d, "usage.jsonl")
    with open(log, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        fh.write("\n")            # 空行：所有消费者都跳过
        fh.write("{not json}\n")  # 坏行：只有 count_usage_rows 会计
    return log, d


def _views(log, since=None):
    """切到夹具日志取两个视图（_uncached 版本，绕开 ttl=10 的窗口缓存）。"""
    orig = P.USAGE_LOG
    P.USAGE_LOG = log
    try:
        k = {b["key_id"]: b for b in P._usage_by_key_uncached(since=since)}
        a = {b["account"]: b for b in P._usage_by_account_uncached(since=since)}
    finally:
        P.USAGE_LOG = orig
    return k, a


def _local_midnight():
    lt = time.localtime()
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))


def _sum_tokens(buckets):
    return sum(b["total_tokens"] for b in buckets.values())


def _sum_requests(buckets):
    return sum(b["requests"] for b in buckets.values())


# ---- 夹具 A：我的简版（跨日 + error + 缺 key_id + 空行/坏行）----
_today = time.time()
_yday = _today - 86400


def _iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t))


_rows_a = [
    {"at": _today, "iso": _iso(_today), "model": "M1", "account": "u1", "key_id": "k00001",
     "prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110, "credit": 1.0},
    {"at": _today, "iso": _iso(_today), "model": "M2", "account": "u2", "key_id": "k00002",
     "prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25, "credit": 0.5},
    {"at": _today, "iso": _iso(_today), "model": "M1", "account": "u1",
     "prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10, "credit": 0.1},
    {"at": _today, "iso": _iso(_today), "model": "M1", "account": "u1", "key_id": "k00001",
     "error": "boom", "prompt_tokens": 999, "total_tokens": 999, "credit": 9.9},
    {"at": _yday, "iso": _iso(_yday), "model": "M-old", "account": "u1", "key_id": "k00001",
     "prompt_tokens": 500, "total_tokens": 500, "credit": 5.0},
]
_log_a, _dir_a = _fixture(_rows_a, "bykeyA")
_mid = _local_midnight()
allk, alla = _views(_log_a)
dayk, daya = _views(_log_a, since=_mid)
shutil.rmtree(_dir_a, ignore_errors=True)

check("by_key: 缺 key_id 的行归 (no-key) 桶且不丢弃",
      set(allk) == {"k00001", "k00002", "(no-key)"}, sorted(allk))
check("by_key: error 行不计入（无窗口 k00001 = 今天 110 + 昨日 500 = 2 条/610）",
      allk["k00001"]["requests"] == 2 and allk["k00001"]["total_tokens"] == 610
      and abs(allk["k00001"]["credit"] - 6.0) < 1e-9,
      (allk["k00001"]["requests"], allk["k00001"]["total_tokens"]))
check("by_key: 跨日行被 day 窗口排除（无窗口 645 -> day 145；k00001 2 条 -> 1 条）",
      _sum_tokens(allk) == 645 and _sum_tokens(dayk) == 145
      and dayk["k00001"]["requests"] == 1,
      (_sum_tokens(allk), _sum_tokens(dayk), dayk.get("k00001", {}).get("requests")))
for _tag, _k, _a in (("全量", allk, alla), ("day", dayk, daya)):
    check("by_key ≡ by_account（%s 窗口：token 与 requests 合计都相等）" % _tag,
          _sum_tokens(_k) == _sum_tokens(_a)
          and _sum_requests(_k) == _sum_requests(_a),
          (_tag, _sum_tokens(_k), _sum_tokens(_a)))

# ---- 夹具 B：natie §15（7 类行；期望值写死）----
_t_today = _mid + 1800      # 今天 00:30 —— 必在 day 窗口内
_t_yday = _mid - 1800       # 昨天 23:30 —— 必在 day 窗口外
_rows_b = [
    {"at": _t_today, "iso": _iso(_t_today), "model": "M1", "account": "u1", "key_id": "k1",
     "prompt_tokens": 6, "completion_tokens": 4, "total_tokens": 10, "credit": 1.0},
    {"at": _t_today, "iso": _iso(_t_today), "model": "M1", "account": "u2", "key_id": "k1",
     "prompt_tokens": 6, "completion_tokens": 5, "total_tokens": 11, "credit": 1.1},
    {"at": _t_today, "iso": _iso(_t_today), "model": "M1", "account": "u1", "key_id": "k1",
     "error": "boom", "prompt_tokens": 999, "total_tokens": 999, "credit": 9.9},
    {"iso": _iso(_t_today), "model": "M1", "account": "u1", "key_id": "k1",
     "prompt_tokens": 20, "total_tokens": 20, "credit": 2.0},
    {"at": _t_today, "iso": _iso(_t_today), "model": "M1", "account": "u1",
     "prompt_tokens": 30, "total_tokens": 30, "credit": 3.0},
    {"at": _t_today, "iso": _iso(_t_today), "model": "M1", "key_id": "k1",
     "prompt_tokens": 40, "total_tokens": 40, "credit": 4.0},
    {"at": _t_yday, "iso": _iso(_t_yday), "model": "M1", "account": "u1", "key_id": "k1",
     "prompt_tokens": 100, "total_tokens": 100, "credit": 10.0},
]
_log_b, _dir_b = _fixture(_rows_b, "bykeyB")
allk_b, alla_b = _views(_log_b)
dayk_b, daya_b = _views(_log_b, since=_mid)
shutil.rmtree(_dir_b, ignore_errors=True)

check("§15 桶数可不等而合计相等（一 Key 打多账号：keys < accounts）",
      len(allk_b) < len(alla_b) and _sum_tokens(allk_b) == _sum_tokens(alla_b),
      (len(allk_b), len(alla_b), _sum_tokens(allk_b)))
check("§15 缺 at 行：day 窗口按 at=0 排除，且两侧同判（k1=61 / u1=40）",
      dayk_b["k1"]["total_tokens"] == 61 and daya_b["u1"]["total_tokens"] == 40,
      (dayk_b["k1"]["total_tokens"], daya_b["u1"]["total_tokens"]))
check("§15 day 合计 91 == 91；无窗口 211 == 211（error 行两侧都跳过）",
      _sum_tokens(dayk_b) == 91 and _sum_tokens(daya_b) == 91
      and _sum_tokens(allk_b) == 211 and _sum_tokens(alla_b) == 211,
      (_sum_tokens(dayk_b), _sum_tokens(allk_b)))
check("§15 无窗口 by_key k1 = (5, 181)（含缺 at 的 20 与昨日 100）",
      allk_b["k1"]["requests"] == 5 and allk_b["k1"]["total_tokens"] == 181,
      (allk_b["k1"]["requests"], allk_b["k1"]["total_tokens"]))

finish()
