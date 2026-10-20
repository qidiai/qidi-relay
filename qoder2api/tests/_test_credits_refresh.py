"""余额刷新与护栏解耦（issue #43）。

    python tests/_test_credits_refresh.py

判据全在**刷新器本身**（不发真实网络）：只刷用过的号、一个窗口合并一次、
空闲不轮询、失败保留旧快照并退避、手动查询清 pending。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _suite_head import *          # noqa: F401,F403

P, A = bootstrap()
declare_suite(__file__, ["qoder_accounts.py", "qoder_proxy.py", "dashboard.html"])


class FakePool:
    def __init__(self, accounts):
        self.accounts = list(accounts)


def make_account(uid, credits=None, enabled=True):
    acc = A.Account({"uid": uid, "realm": "cn", "accessToken": "tok",
                     "enabled": enabled})
    acc.credits = credits
    return acc


# ---- 用例一：护栏全关（阈值 0）也要能刷「用过的号」
_acc1 = make_account("u1")
_calls = []


def _fetcher(acc):
    _calls.append(acc.uid)
    acc.credits = {"remain": 900, "size": 1000,
                   "updated_at": 1760000000.0,
                   "updated_iso": "2026-10-18 14:00:00"}
    return {"ok": True, "credits": acc.credits}


_r = A.CreditsUsageRefresher(FakePool([_acc1]), interval_seconds=30,
                             fetcher=_fetcher, log=lambda *a, **k: None)
check("#43 护栏阈值全 0 时刷新器仍可构造/工作",
      _acc1.reserve_credits == 0 and _acc1.daily_token_limit == 0)
check("#43 未使用时 tick 不发查询（空闲不轮询）",
      _r.tick_once() == {"refreshed": 0, "failed": 0} and _calls == [], _calls)
check("#43 mark_used 只打标、不触发网络",
      _r.mark_used("u1") is True and _calls == [], _calls)
check("#43 打标后 state=pending", _acc1.credits_refresh_state == "pending")

# ---- 用例二：同一窗口多次使用 -> 合并成一次查询
check("#43 重复 mark_used 不重复排队", _r.mark_used("u1") is False)
_st = _r.tick_once()
check("#43 一个窗口只查一次", _st["refreshed"] == 1 and _calls == ["u1"], _calls)
check("#43 成功后清 pending 且 state 回 idle",
      _acc1.credits_pending is False and _acc1.credits_refresh_state == "idle")
check("#43 tick 后空转不再查询（用完后补完最后一次）",
      _r.tick_once() == {"refreshed": 0, "failed": 0} and _calls == ["u1"])

# ---- 用例四：失败保留旧快照 + 退避重试
_old = {"remain": 500, "size": 1000, "updated_at": 1750000000.0,
        "updated_iso": "2026-09-01 00:00:00"}
_acc2 = make_account("u2", credits=dict(_old))
_fail_calls = []


def _fail_fetcher(acc):
    _fail_calls.append(acc.uid)
    return {"ok": False, "error": "HTTP 403 WAF"}


_r2 = A.CreditsUsageRefresher(FakePool([_acc2]), interval_seconds=30,
                              fetcher=_fail_fetcher, log=lambda *a, **k: None)
_r2.mark_used("u2")
_st2 = _r2.tick_once()
check("#43 失败计入 failed", _st2["failed"] == 1, _st2)
check("#43 失败不动旧快照（余额与时间都保留）",
      _acc2.credits == _old, _acc2.credits)
check("#43 失败写 state=failed + 错误摘要",
      _acc2.credits_refresh_state == "failed"
      and "403" in _acc2.credits_refresh_error, _acc2.credits_refresh_error)
check("#43 失败保留 pending 且排了重试时间",
      _acc2.credits_pending is True and _acc2.credits_refresh_next > 0)
check("#43 退避期内 tick 不再打上游",
      _r2.tick_once() == {"refreshed": 0, "failed": 0}
      and len(_fail_calls) == 1, _fail_calls)

# 退避到期后重试成功 -> 清状态
_acc2.credits_refresh_next = 0
_r2._fetcher = lambda acc: (setattr(acc, "credits", {"remain": 700, "size": 1000,
                                                    "updated_iso": "2026-10-18 15:00:00"})
                            or {"ok": True})
_r2.tick_once()
check("#43 重试成功后清 pending/失败计数",
      _acc2.credits_pending is False
      and _acc2.credits_refresh_state == "idle"
      and _acc2.credits_refresh_failures == 0)

# ---- 用例五：手动查询清 pending
_r.mark_used("u1")
check("#43 手动刷新清掉待刷新", _r.mark_manual("u1") is True
      and _acc1.credits_pending is False)

# ---- 禁用账号不打标
_acc3 = make_account("u3", enabled=False)
_r3 = A.CreditsUsageRefresher(FakePool([_acc3]), interval_seconds=30,
                              fetcher=_fetcher, log=lambda *a, **k: None)
check("#43 禁用账号不进入待刷新", _r3.mark_used("u3") is False
      and _acc3.credits_pending is False)

# ---- 面板/接口暴露新鲜度字段
_acc1.credits = {"remain": 1, "updated_at": 1.0, "updated_iso": "2026-10-18 14:00:00"}
_pub = _acc1.public()
check("#43 public() 暴露最后成功时间与刷新状态",
      _pub.get("creditsUpdatedIso") == "2026-10-18 14:00:00"
      and _pub.get("creditsRefreshState") == "idle"
      and _pub.get("creditsPending") is False, _pub)

# ---- 静态：record_usage 打标 + main 启动 + 面板新鲜度行
_src = open(os.path.join(ROOT, "qoder_proxy.py"), encoding="utf-8").read()
_js = open(os.path.join(ROOT, "dashboard.html"), encoding="utf-8").read()
check("#43 record_usage 里只打标（不联网）",
      "CREDITS_USAGE_REFRESHER.mark_used(account)" in _src)
check("#43 main() 启动刷新器", "CreditsUsageRefresher(" in _src)
check("#43 面板显示最后成功查询时间/状态",
      "creditsUpdatedIso" in _js and "查询失败，显示旧快照" in _js)

# ---- issue #40 后续：签到成功后立刻刷新余额 ----
_http_calls = []


def _fake_http_checkin(url, data=None, method=None, headers=None, **kw):
    _http_calls.append(url)
    if "daily-check-in/status" in url:
        return {"status": "CLAIMABLE", "currentStreakDays": 1}
    if "daily-check-in/claim" in url:
        return {"success": True, "rewardCredits": 100}
    if "quota/usage" in url:
        return {"userQuota": {"total": 1000, "used": 0, "remaining": 1000},
                "addOnQuota": {"total": 100, "used": 0, "remaining": 100},
                "isQuotaExceeded": False}
    raise AssertionError("unexpected url " + url)


_real_http = A.http_json
try:
    A.http_json = _fake_http_checkin
    _acc_ci = A.Account({"uid": "u-ci", "realm": "cn", "accessToken": "tok"})
    _res_ci = _acc_ci.checkin()
finally:
    A.http_json = _real_http
check("#40 签到成功后立刻刷新余额（不再停留在旧快照）",
      _res_ci.get("ok") and (_acc_ci.credits or {}).get("remain") == 1100,
      _acc_ci.credits)
check("#40 刷新确实打了 quota 接口",
      any("quota/usage" in u for u in _http_calls), _http_calls)

# ---- issue #40 后续：全 0 退化体 -> 降级标记（面板显示 0*，不是空白）----
_DEGENERATE = {
    "userType": "personal_standard",
    "isQuotaExceeded": True,
    "userQuota": {"total": 0.0, "used": 0.0, "remaining": 0.0},
}
try:
    A.http_json = lambda *a, **k: dict(_DEGENERATE)
    _acc_dg = A.Account({"uid": "u-dg", "realm": "intl",
                         "accessToken": "tok"})
    _res_dg = _acc_dg.fetch_credits()
finally:
    A.http_json = _real_http
check("#40 退化体不写假快照（credits 保持 None）",
      _acc_dg.credits is None and not _res_dg.get("ok"), _res_dg)
check("#40 退化体写降级标记（at + 具体 reason）",
      isinstance(_acc_dg.credits_degraded, dict)
      and _acc_dg.credits_degraded.get("at")
      and bool(_acc_dg.credits_degraded.get("reason")),
      _acc_dg.credits_degraded)
check("#40 public() 暴露降级标记（面板据此显示 0*）",
      _acc_dg.public().get("creditsDegraded") is not None)
# 正常体清掉降级标记
try:
    A.http_json = lambda *a, **k: {
        "userQuota": {"total": 10, "used": 0, "remaining": 10},
        "addOnQuota": {"total": 0, "used": 0, "remaining": 0},
        "isQuotaExceeded": False,
    }
    _acc_dg.fetch_credits()
finally:
    A.http_json = _real_http
check("#40 正常体清掉降级标记", _acc_dg.credits_degraded is None
      and (_acc_dg.credits or {}).get("remain") == 10)

_js40 = open(os.path.join(ROOT, "dashboard.html"), encoding="utf-8").read()
check("#40 面板对空额度显示 0*（不是「未查询」）",
      "空额度" in _js40 and "creditsDegraded" in _js40)

finish()
