"""一键签到文案同源套件（#25 遗留 · accounts 侧）。

    python tests/_test_campaign_copy.py

判据：**已领事实先于 action_type 过滤**——三档 action_type（VIEW_DETAILS /
CLAIM_BENEFIT / 空）在已领状态下文案一致；纯详情（amount=0）仍不算已领；
未领的 VIEW_DETAILS 不发 claim；CN 侧为对照组（逐字核对另见改前快照）。
"""
import os
import shutil
import sys
import tempfile

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


D = tempfile.mkdtemp(prefix="qd-copy-")
_orig_camps = A.Account.campaigns
_orig_claim = A.Account.claim_campaign
_orig_ident = A.native_machine_identity


def _camps(*items):
    def fake(self, force=False):
        return {"ok": True, "campaigns": list(items), "show_campaign": True,
                "claimable": False, "campaign_url": "", "identity": "derived",
                "errors": []}
    return fake


def _item(name, action_type, status="CLAIMED", amount=100, kind="CREDITS"):
    return {"campaign_id": "c-" + name, "campaign_key": "act-" + name,
            "action_type": action_type, "claim_status": status,
            "benefit": {"kind": kind, "amount": amount},
            "unavailable_reason": "", "achievement_completed": True,
            "start_at": 0, "end_at": 0, "placements": []}


def _run(items, realm="intl"):
    acc = A.Account({"uid": "copy-" + realm, "realm": realm,
                     "accessToken": "dt-x"})
    acc.enabled = True
    acc.expires_at = 0
    A.native_machine_identity = lambda *a, **k: {}
    A.Account.campaigns = _camps(*items)
    claims = []
    A.Account.claim_campaign = lambda self, cid: (
        claims.append(cid), {"ok": False, "error": "stub"})[1]
    try:
        res = acc.campaign_checkin(gap=0, only_kinds=("", "CREDITS"))
    finally:
        A.Account.campaigns = _orig_camps
        A.Account.claim_campaign = _orig_claim
        A.native_machine_identity = _orig_ident
    return res, claims


# ---- 指纹行（与前端 _test_dashboard_*.js 同格式；实现见 tests/_suite_head.py）----
import os as _os_fp
import sys as _sys_fp
_sys_fp.path.insert(0, _os_fp.path.dirname(_os_fp.path.abspath(__file__)))
from _suite_head import declare_suite
declare_suite(__file__, ["qoder_accounts.py"])
print("[campaign-copy] #25 遗留 · accounts 文案同源")

# P1 三档 action_type 在「已领」状态下文案一致
msgs = {}
for at in ("VIEW_DETAILS", "CLAIM_BENEFIT", ""):
    res, _ = _run([_item("daily", at)])
    msgs[at] = res.get("message") or ""
    check("P1 已领(%s) -> already 且 msg 含「已领取」" % (at or "空档"),
          bool(res.get("already")) and "已领取" in msgs[at],
          (at, msgs[at], res.get("already")))
check("P1b 三档 action_type 的文案完全一致（同源）",
      len(set(msgs.values())) == 1, msgs)

# P3 纯详情（amount=0 + VIEW_DETAILS）仍不算已领
res3, _ = _run([_item("detail", "VIEW_DETAILS", amount=0)])
check("P3 纯详情（amount=0）不算已领（防过度修）",
      not res3.get("already") and "已领取" not in (res3.get("message") or ""),
      (res3.get("already"), res3.get("message")))

# P4 未领的 VIEW_DETAILS 不发 claim、不算已领
res4, claims4 = _run([_item("pending", "VIEW_DETAILS", status="CLAIMABLE",
                            amount=100)])
check("P4 未领 VIEW_DETAILS -> 不发 claim、不进已领",
      claims4 == [] and not res4.get("already"), (claims4, res4.get("already")))

# P2 CN 对照组（不改戏）：已领 CLAIM_BENEFIT + 可领 Daily
res_cn, claims_cn = _run([_item("cn-claimed", "CLAIM_BENEFIT"),
                          _item("cn-open", "CLAIM_BENEFIT",
                                status="CLAIMABLE")], realm="cn")
check("P2 CN 对照组：已领报「已领取」、可领走 claim（照旧）",
      "已领取" in (res_cn.get("message") or "")
      and bool(res_cn.get("already")) and claims_cn == ["c-cn-open"],
      (res_cn.get("message"), res_cn.get("already"), claims_cn))

shutil.rmtree(D, ignore_errors=True)

print("")
print("SUMMARY: TOTAL %d checks, %d passed, %d failed" % (PASS + FAIL, PASS, FAIL))
print("RESULT: %s (exit %d)" % ("RED" if FAIL else "GREEN", 1 if FAIL else 0))
sys.exit(1 if FAIL else 0)
