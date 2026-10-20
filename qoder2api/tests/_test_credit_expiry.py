"""额度到期数据源套件（S1 回归守卫）。

    python tests/_test_credit_expiry.py

背景（task-69 审查 S1）：F 层临期分派依赖 soonest_expiring_days，而它只读
packages 内的 days_left / expires_at / expireAt；实测上游 userQuota /
addOnQuota 内部**没有任何**到期字段——旧实现下真实产物永远算不出非 None，
功能静默不可用。本套件钉三件事：
  E1 真实形状（无包级到期）→ 产物可算且为 None、窗口不触发（现状如实，安全侧）；
  E2 上游补包级字段那天 → fetch_credits 透传 → 立即算得出（透传管道守卫）；
  E3 顶层 expiresAt（归属未证实）不被误贴到包（防反向消耗策略）。
不联网：桩掉 http_json。
"""
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("ACCOUNTS_DIR", os.path.join(ROOT, "_acc"))
import qoder_accounts as A

PASS = FAIL = 0


def check(label, cond, extra=None):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] " + label)
    else:
        FAIL += 1
        print("  [FAIL] %s  %r" % (label, extra))


def _quota_shape(addon_extra=None):
    """探针抓到的真实响应形状（脱敏：数值为无关样例）。

    顶层 expiresAt 用「永不过期」占位（253402214400，实测 CN 号会返回该值），
    顺带验证它不会被误映射到包级。
    """
    aq = {"total": 800.0, "used": 0.0, "remaining": 800.0, "percentage": 0.0,
          "unit": "credits", "detailUrl": "https://example.invalid/detail"}
    if addon_extra:
        aq.update(addon_extra)
    return {
        "userId": "u", "userType": "personal_standard", "usageType": "credits",
        "totalUsagePercentage": 0.14, "isQuotaExceeded": False,
        "expiresAt": 253402214400,
        "userQuota": {"total": 300.0, "used": 147.0, "remaining": 153.0,
                      "percentage": 0.5, "unit": "credits"},
        "addOnQuota": aq,
    }


def _fetch(shape):
    a = A.Account({"uid": "exp1", "realm": "cn", "accessToken": "dt-x"})
    a.enabled = True
    a.path = None
    orig = A.http_json
    A.http_json = lambda *aa, **kk: shape
    try:
        a.fetch_credits()
    finally:
        A.http_json = orig
    return a


# ---- 指纹行（与前端 _test_dashboard_*.js 同格式；实现见 tests/_suite_head.py）----
import os as _os_fp
import sys as _sys_fp
_sys_fp.path.insert(0, _os_fp.path.dirname(_os_fp.path.abspath(__file__)))
from _suite_head import declare_suite
declare_suite(__file__, ["qoder_accounts.py"])
print("[S1] credit expiry data-source guards")

a1 = _fetch(_quota_shape())
check("E1 真实形状（无包级到期）→ soonest 为 None（如实：当前算不出）",
      a1.soonest_expiring_days() is None,
      a1.credits.get("packages"))
check("E1b 真实形状 → 窗口判定恒 False（安全侧不误触发）",
      a1.in_expiring_window() is False)
check("E1c packages 不引入臆造字段（仅 name/remain/used/size）",
      all(set(p) == {"name", "remain", "used", "size"} for p in a1.credits["packages"]),
      a1.credits["packages"])

a2 = _fetch(_quota_shape(addon_extra={"expiresAt": time.time() + 3 * 86400}))
soon2 = a2.soonest_expiring_days()
check("E2 上游补包级 expiresAt → fetch_credits 透传 → soonest ≈ 3 天（透传管道）",
      soon2 is not None and 2.5 < soon2 < 3.5, (soon2, a2.credits.get("packages")))
a2.expiring_window_days = 7
check("E2b 端到端：真实产物 + 窗口 7 天 → in_expiring_window True（真判据）",
      a2.in_expiring_window() is True)

check("E3 顶层 expiresAt（永不过期占位）不被映射到包级（防反向消耗）",
      all("expires_at" not in p for p in a1.credits["packages"])
      and all("expires_at" not in p for p in a2.credits["packages"]
              if p["name"] == "基础额度"))

print("")
print("SUMMARY: TOTAL %d checks, %d passed, %d failed" % (PASS + FAIL, PASS, FAIL))
print("RESULT: %s (exit %d)" % ("RED" if FAIL else "GREEN", 1 if FAIL else 0))
sys.exit(1 if FAIL else 0)
