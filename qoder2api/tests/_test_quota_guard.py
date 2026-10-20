"""额度守卫 + 签到盖章套件（#25 的 d/e）。

    python tests/_test_quota_guard.py

d) fetch_credits plausibility guard：退化 body 不覆盖健康快照、ok=False、
   落盘 raw 摘要；且**不误伤真·归零**（保守性由 Q2 钉住）。
e) _stamp_checkin 补调：CLAIMED/already 分支同步盖章，令 can_checkin 收口
   （naonao 的因果链：lastCheckin 恒 null -> 每轮重走）。
"""
import json
import os
import shutil
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("ACCOUNTS_DIR", os.path.join(ROOT, "_acc"))
import qoder_accounts as A

PASS = FAIL = 0


def check(label, ok, extra=None):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  [PASS] " + label)
    else:
        FAIL += 1
        print("  [FAIL] %s  %r" % (label, extra))


D = tempfile.mkdtemp(prefix="qd-qguard-")
_orig_http = A.http_json


def _mk(uid):
    path = os.path.join(D, uid + ".json")
    acc = A.Account({"uid": uid, "realm": "cn", "accessToken": "dt-x"}, path)
    acc.enabled = True
    acc.expires_at = 0
    return acc


def _stub_json(payload):
    A.http_json = lambda *a, **k: payload


HEALTHY = {"remain": 399, "used": 1, "size": 400, "packages": []}
DEGEN = {"isQuotaExceeded": True, "expiresAt": 253402214400,
         "totalUsagePercentage": 1.0,
         "userQuota": {"total": 0, "used": 0, "remaining": 0},
         "addOnQuota": {"total": 0, "used": 0, "remaining": 0}}
REAL_ZERO = {"isQuotaExceeded": True, "expiresAt": 1792065206,
             "totalUsagePercentage": 100.0,
             "userQuota": {"total": 1000, "used": 1000, "remaining": 0},
             "addOnQuota": {"total": 300, "used": 300, "remaining": 0}}
MISSING = {"isQuotaExceeded": True, "expiresAt": 253402214400}
NORMAL = {"isQuotaExceeded": False, "expiresAt": 1792065206,
          "userQuota": {"total": 1000, "used": 601, "remaining": 399},
          "addOnQuota": {"total": 300, "used": 50, "remaining": 250}}

# ---- 指纹行（与前端 _test_dashboard_*.js 同格式；实现见 tests/_suite_head.py）----
import os as _os_fp
import sys as _sys_fp
_sys_fp.path.insert(0, _os_fp.path.dirname(_os_fp.path.abspath(__file__)))
from _suite_head import declare_suite
declare_suite(__file__, ["qoder_accounts.py"])
print("[quota-guard] #25 d/e")

# ---------------------------------------------------------------- d)
a1 = _mk("d1")
a1.credits = dict(HEALTHY)
_stub_json(DEGEN)
r1 = a1.fetch_credits()
check("Q1 退化 body -> 不覆盖健康快照（remain 仍 399）+ ok=False",
      (a1.credits or {}).get("remain") == 399 and r1.get("ok") is False,
      (a1.credits, r1))
check("Q1b 退化 -> last_quota_anomaly 含 raw 摘要与 reason",
      isinstance(getattr(a1, "last_quota_anomaly", None), dict)
      and "expiresAt" in str(a1.last_quota_anomaly.get("raw") or "")
      and a1.last_quota_anomaly.get("reason"),
      getattr(a1, "last_quota_anomaly", None))

a2 = _mk("d2")
a2.credits = dict(HEALTHY)
_stub_json(REAL_ZERO)
r2 = a2.fetch_credits()
check("Q2 保守性：真·归零（size>0, remain=0）-> 照常覆盖 + ok=True",
      (a2.credits or {}).get("remain") == 0 and r2.get("ok") is True,
      (a2.credits, r2))

a3 = _mk("d3")
a3.credits = dict(HEALTHY)
_stub_json(MISSING)
r3 = a3.fetch_credits()
check("Q3 两个 quota 键都缺 -> 不覆盖 + ok=False",
      (a3.credits or {}).get("remain") == 399 and r3.get("ok") is False,
      (a3.credits, r3))

a4 = _mk("d4")
a4.credits = dict(HEALTHY)
_stub_json([1, 2, 3])
try:
    r4 = a4.fetch_credits()
except Exception as exc:
    r4 = {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc),
          "_raised": True}
check("Q4 非 dict 响应（解析失败形态）-> 不覆盖 + ok=False（不抛）",
      (a4.credits or {}).get("remain") == 399 and r4.get("ok") is False
      and not r4.get("_raised"),
      (a4.credits, r4))

a5 = _mk("d5")
a5.credits = {"remain": 1, "used": 0, "size": 1, "packages": []}
_stub_json(NORMAL)
r5 = a5.fetch_credits()
check("Q5 正常 body -> 照常覆盖 + ok=True（回归）",
      (a5.credits or {}).get("remain") == 649 and r5.get("ok") is True,
      (a5.credits, r5))

with open(os.path.join(D, "d1.json"), encoding="utf-8") as fh:
    disk = json.load(fh)
check("Q6 退化后落盘：账号 JSON 含 lastQuotaAnomaly.raw（供事后回溯）",
      isinstance(disk.get("lastQuotaAnomaly"), dict)
      and disk["lastQuotaAnomaly"].get("raw"),
      disk.get("lastQuotaAnomaly"))

# ---------------------------------------------------------------- e)
def _status_stub(today_checked_in=True):
    def fake_status(self):
        return True, {"status": "CLAIMED", "active": True,
                      "today_checked_in": today_checked_in, "streak_days": 3,
                      "reward_credits": 10, "total_claim_days": 3,
                      "reward_expires_at": 0, "next_claim_at": 0,
                      "total_reward_credits": 30, "last_claimed_at": 0}
    return fake_status


def _camps_stub(action_type="", amount=10):
    def fake_camps(self, force=False):
        return {"ok": True, "campaigns": [{
            "campaign_id": "c1", "campaign_key": "k1",
            "claim_status": "CLAIMED", "action_type": action_type,
            "benefit": {"kind": "CREDITS", "amount": amount},
            "unavailable_reason": "", "achievement_completed": True,
            "start_at": 0, "end_at": 0, "placements": [],
        }], "show_campaign": True, "claimable": False, "campaign_url": "",
        "identity": "derived", "errors": []}
    return fake_camps


_orig_status = A.Account.checkin_status
_orig_camps = A.Account.campaigns
_orig_ident = A.native_machine_identity

e1 = _mk("e1")
A.Account.checkin_status = _status_stub(True)
try:
    res = e1.checkin()
finally:
    A.Account.checkin_status = _orig_status
check("E1 sash「今日已领」分支 -> 同步盖章（lastCheckin 非空 + can_checkin False）",
      res.get("already") is True and e1.last_checkin
      and e1.can_checkin() is False, (res, e1.last_checkin))

e2 = _mk("e2")
A.native_machine_identity = lambda *a, **k: {}
A.Account.campaigns = _camps_stub("")
try:
    camp = e2.campaign_checkin(only_kinds=("", "CREDITS"))
finally:
    A.Account.campaigns = _orig_camps
    A.native_machine_identity = _orig_ident
check("E2 campaign 已领（CLAIMED 非 VIEW_DETAILS）-> 同步盖章",
      bool(camp.get("already")) and e2.last_checkin
      and e2.can_checkin() is False,
      (camp.get("already"), e2.last_checkin))

e3 = _mk("e3")
A.native_machine_identity = lambda *a, **k: {}
A.Account.campaigns = _camps_stub("VIEW_DETAILS")
try:
    camp3 = e3.campaign_checkin(only_kinds=("", "CREDITS"))
finally:
    A.Account.campaigns = _orig_camps
    A.native_machine_identity = _orig_ident
check("E3a #25 遗留新语义：VIEW_DETAILS 但 amount>0（降级的已领）-> 算已领、盖章",
      bool(camp3.get("already")) and e3.last_checkin is not None,
      (camp3.get("already"), camp3.get("views"), e3.last_checkin))

e3b = _mk("e3b")
A.native_machine_identity = lambda *a, **k: {}
A.Account.campaigns = _camps_stub("VIEW_DETAILS", amount=0)
try:
    camp3b = e3b.campaign_checkin(only_kinds=("", "CREDITS"))
finally:
    A.Account.campaigns = _orig_camps
    A.native_machine_identity = _orig_ident
check("E3b 防过度：纯详情（amount=0）不算已领（不盖章，归 views）",
      not camp3b.get("already") and e3b.last_checkin is None
      and [c["campaign_key"] for c in camp3b.get("views") or []] == ["k1"],
      (camp3b.get("views"), e3b.last_checkin))

e4 = _mk("e4")
e4.last_checkin = time.strftime("%Y-%m-%d %H:%M:%S")
saves = {"n": 0}
_orig_save = A.Account.save


def _count_save(self, *a, **k):
    saves["n"] += 1
    return _orig_save(self, *a, **k)


A.Account.checkin_status = _status_stub(True)
A.Account.save = _count_save
try:
    e4.checkin()
finally:
    A.Account.checkin_status = _orig_status
    A.Account.save = _orig_save
check("E4 已盖章的号重复走「已领」分支 -> 不再写盘（短路）",
      saves["n"] == 0, saves)

A.http_json = _orig_http
shutil.rmtree(D, ignore_errors=True)

print("")
print("SUMMARY: TOTAL %d checks, %d passed, %d failed" % (PASS + FAIL, PASS, FAIL))
print("RESULT: %s (exit %d)" % ("RED" if FAIL else "GREEN", 1 if FAIL else 0))
sys.exit(1 if FAIL else 0)
