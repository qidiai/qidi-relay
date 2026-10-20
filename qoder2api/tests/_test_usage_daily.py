# -*- coding: utf-8 -*-
"""7 天 / 30 天 Token 用量图的数据端点（/usage/daily）。

    python tests/_test_usage_daily.py

钉住的判据：
  ① 桶按**本地自然日**切（不是「现在往前推 N×86400 秒」），日期连续升序无重复；
  ② 四种分组（model / realm / key / account）口径一致：
     Σ groups == Σ series == Σ 每日 groups 明细 == totals；
  ③ 倒读提前收工**不能漏窗口内的行**（窗口外的行必须被判掉）；
  ④ error 行只进 errors，不进 requests 也不进 token 合计；
  ⑤ days / group 参数兜底（0→1、999→90、非法分组→model）；
  ⑥ 空日志也返回完整 N 天空壳（前端 x 轴要连着，不能断档）；
  ⑦ 缓存按 (realm, days, group, 窗口起点) 分键，不同档位不串。
"""
import json
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _suite_head import *        # noqa: F401,F403

P, A = bootstrap()

declare_suite(__file__, ["qoder_proxy.py"])

tmp = tempfile.mkdtemp(prefix="qd-daily-")
orig_log = P.USAGE_LOG
today = P._local_day_start(time.time())


def write_rows(rows, sort=True):
    """写真日志：默认按 at 升序（与 record_usage 的 append 顺序一致）。

    sort=False 用来造「被外部重排」的脏日志，验证倒读失明时的顺序兜底。
    """
    path = os.path.join(tmp, "usage.jsonl")
    ordered = sorted(rows, key=lambda r: r.get("at") or 0) if sort else rows
    with open(path, "w", encoding="utf-8") as fh:
        for r in ordered:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    P.USAGE_LOG = path
    return path


def row(at, model="Qwen3.8-Flash", realm="cn", tokens=1000, credit=0.1,
        account="acct-cn", key_id="k0", error=False):
    base = {"at": at, "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(at)),
            "model": model, "realm": realm, "account": account, "key_id": key_id}
    if error:
        base.update({"error": True, "status": 500, "message": "boom"})
    else:
        base.update({"prompt_tokens": tokens, "completion_tokens": 10,
                     "total_tokens": tokens + 10, "credit": credit})
    return base


def clear_cache():
    with P._daily_lock:
        P._daily_cache.clear()


try:
    # 造 3 天数据：今天 / 昨天 / 前天，另有一行 40 天前（窗口外）
    rows = [
        row(today + 3600, model="Qwen3.8-Flash", tokens=1000, credit=0.10),
        row(today + 7200, model="Auto", tokens=2000, credit=0.20),
        row(today + 7300, model="Auto", realm="intl", tokens=500, credit=0.05,
            account="acct-intl", key_id="k1"),
        row(today + 9000, model="Auto", error=True),                     # 失败请求
        row(today - 86400 + 3600, model="Qwen3.8-Max", tokens=3000, credit=0.30),
        row(today - 2 * 86400 + 3600, model="Qwen3.8-Max", tokens=4000, credit=0.40),
        row(today - 40 * 86400, model="Ancient", tokens=99999, credit=9.9),  # 窗口外
    ]
    write_rows(rows)
    clear_cache()

    d7 = P.usage_daily(days=7, group="model", realm="cn")
    check("7 天窗口返回 7 个桶", len(d7["series"]) == 7, len(d7["series"]))
    days = [s["day"] for s in d7["series"]]
    check("日期升序且无重复", days == sorted(days) and len(set(days)) == 7, days)
    check("首桶是 6 天前、末桶是今天",
          days[0] == time.strftime("%Y-%m-%d", time.localtime(today - 6 * 86400))
          and days[-1] == time.strftime("%Y-%m-%d", time.localtime(today)), days)

    # ② 口径一致（model 分组，realm=cn）
    s_groups = sum(g["total_tokens"] for g in d7["groups"])
    s_series = sum(s["total_tokens"] for s in d7["series"])
    s_inner = sum(sum((s["groups"] or {}).values()) for s in d7["series"])
    total = d7["totals"]["total_tokens"]
    check("Σ groups == Σ series == Σ 每日明细 == totals",
          s_groups == s_series == s_inner == total, (s_groups, s_series, s_inner, total))
    check("cn 区合计 = 1000+2000+3000+4000（各 +10 completion）",
          total == 1010 + 2010 + 3010 + 4010, total)

    # ③ 窗口外的行没进来
    ids = {g["id"] for g in d7["groups"]}
    check("40 天前的行被窗口挡住（Ancient 不在分组里）", "Ancient" not in ids, ids)
    check("窗口外那行的 tokens 没算进合计", total < 99999, total)

    # ④ error 行只进 errors
    check("error 行计入 errors，不计入 requests",
          d7["totals"]["errors"] == 1 and d7["totals"]["requests"] == 4,
          (d7["totals"]["requests"], d7["totals"]["errors"]))

    # 其它分组
    d7r = P.usage_daily(days=7, group="realm", realm="all")
    check("realm 分组能分出 cn / intl",
          {g["id"] for g in d7r["groups"]} >= {"cn", "intl"},
          [g["id"] for g in d7r["groups"]])
    d7k = P.usage_daily(days=7, group="key", realm="all")
    check("key 分组能分出 k0 / k1", {g["id"] for g in d7k["groups"]} >= {"k0", "k1"},
          [g["id"] for g in d7k["groups"]])
    d7a = P.usage_daily(days=7, group="account", realm="all")
    check("account 分组能分出两个账号",
          {g["id"] for g in d7a["groups"]} >= {"acct-cn", "acct-intl"},
          [g["id"] for g in d7a["groups"]])

    # realm 过滤
    only_intl = P.usage_daily(days=7, group="model", realm="intl")
    check("realm=intl 只算国际版的行",
          only_intl["totals"]["total_tokens"] == 510,
          only_intl["totals"]["total_tokens"])

    # ⑤ 参数兜底
    check("days=0 -> 1", P.usage_daily(days=0, realm="cn")["days"] == 1)
    check("days=999 -> 90", P.usage_daily(days=999, realm="cn")["days"] == 90)
    check("days 非数字 -> 7", P.usage_daily(days="abc", realm="cn")["days"] == 7)
    check("非法 group -> model",
          P.usage_daily(days=7, group="bogus", realm="cn")["group_by"] == "model")
    check("bucket_seconds 固定 86400",
          P.usage_daily(days=7, realm="cn")["bucket_seconds"] == 86400)

    # ⑦ 缓存分键
    clear_cache()
    first = P.usage_daily(days=7, group="model", realm="cn")
    again = P.usage_daily(days=7, group="model", realm="cn")
    other = P.usage_daily(days=7, group="realm", realm="cn")
    check("同参数命中缓存（返回同一对象）", first is again)
    check("不同分组不串缓存", other is not first)

    # ⑥ 空日志：完整空壳
    P.USAGE_LOG = os.path.join(tmp, "empty.jsonl")
    open(P.USAGE_LOG, "w").close()
    clear_cache()
    empty = P.usage_daily(days=30, group="model", realm="cn")
    check("空日志仍返回 30 个桶", len(empty["series"]) == 30, len(empty["series"]))
    check("空日志合计全 0",
          empty["totals"]["total_tokens"] == 0 and empty["groups"] == [])

    # 日志文件不存在也不炸
    P.USAGE_LOG = os.path.join(tmp, "does-not-exist.jsonl")
    clear_cache()
    missing = P.usage_daily(days=7, realm="cn")
    check("日志缺失时优雅返回", missing["ok"] and len(missing["series"]) == 7)

    # 乱序日志（被外部重排 / 时钟回拨）：倒读会在末尾一行就提前收工，
    # 必须靠顺序兜底把窗口内的行找回来。
    write_rows(rows, sort=False)
    clear_cache()
    shuffled = P.usage_daily(days=7, group="model", realm="cn")
    check("乱序日志仍能读全（倒读失明时顺序兜底生效）",
          shuffled["totals"]["total_tokens"] == total,
          (shuffled["totals"]["total_tokens"], total))

    # 反例：窗口内**确实**没数据时不该触发全量兜底（否则每次轮询全扫整份日志）
    write_rows([row(today - 40 * 86400, model="Old", tokens=777)], sort=True)
    clear_cache()
    empty_window = P.usage_daily(days=7, group="model", realm="cn")
    check("窗口内无数据 -> 0 且不误触兜底",
          empty_window["totals"]["total_tokens"] == 0
          and empty_window["groups"] == [],
          (empty_window["totals"]["total_tokens"], empty_window["groups"]))

    # 脏行（历史行里 token 是字符串）：只能跳过，不能让端点 500
    write_rows([row(today + 3600, tokens=100),
                {"at": today + 7200, "model": "Bad", "realm": "cn",
                 "account": "acct-cn", "total_tokens": "123",
                 "prompt_tokens": "1", "completion_tokens": "2",
                 "credit": "0.1"}], sort=True)
    clear_cache()
    dirty = P.usage_daily(days=7, group="model", realm="cn")
    check("脏行被跳过而非 500", dirty["totals"]["total_tokens"] == 110,
          dirty["totals"]["total_tokens"])

    # 缓存洪汰是 LRU 而非 FIFO：持续轮询的热点 key 不能被挤掉
    clear_cache()
    hot = P.usage_daily(days=7, group="model", realm="cn")
    for i in range(40):
        P.usage_daily(days=(i % 30) + 1, group="key", realm="cn")
    check("热点缓存不被 FIFO 淘汰（命中刷新时间戳）",
          P.usage_daily(days=7, group="model", realm="cn") is hot)

    # 夏令时时区：30 天窗口的桶必须整段对齐本地零点，不能因 DST 丢前半段
    old_tz = os.environ.get("TZ")
    try:
        os.environ["TZ"] = "America/New_York"
        if hasattr(time, "tzset"):
            time.tzset()
        dst_today = P._local_day_start(time.time())
        write_rows([row(dst_today - d * 86400 + 3600) for d in range(30)], sort=True)
        clear_cache()
        dst = P.usage_daily(days=30, group="model", realm="cn")
        check("DST 时区 30 天窗口不丢行",
              dst["totals"]["total_tokens"] == 30 * 1010,
              dst["totals"]["total_tokens"])
        check("DST 时区桶键全对齐（30 个非空桶）",
              sum(1 for s in dst["series"] if s["total_tokens"] > 0) == 30,
              sum(1 for s in dst["series"] if s["total_tokens"] > 0))
    finally:
        if old_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old_tz
        if hasattr(time, "tzset"):
            time.tzset()

    # realm 归一化：all / ALL / "" 应共享同一份缓存（不能各自扫一遍）
    clear_cache()
    a1 = P.usage_daily(days=7, group="model", realm="all")
    a2 = P.usage_daily(days=7, group="model", realm="ALL")
    check("realm 大小写归一（共享缓存）", a1 is a2)

    # 端点已注册（路由存在，不是 404）
    src = open(os.path.join(ROOT, "qoder_proxy.py"), encoding="utf-8").read()
    check("路由 /usage/daily 已注册", 'if path == "/usage/daily":' in src)
    check("端点带 _authorized() 门禁",
          'if path == "/usage/daily":' in src
          and src.split('if path == "/usage/daily":')[1][:80].find("_authorized()") >= 0)
finally:
    P.USAGE_LOG = orig_log
    with P._daily_lock:
        P._daily_cache.clear()
    shutil.rmtree(tmp, ignore_errors=True)

finish()
