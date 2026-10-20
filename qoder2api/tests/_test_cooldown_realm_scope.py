# -*- coding: utf-8 -*-
"""#29 双区池冷却口径套件（task-85）。

    python tests/_test_cooldown_realm_scope.py

钉住四组数字 + 一口径：
  1) 双区池 10605 排队 -> 冷却 30->3；
  2) 同场景 wait 0->3（补丁 1 单独不够的证据）；
  3) 真 429 长窗 -> 仍不等（交回 429 路径）；
  4) 号 A 长频控 + 号 B 短冷却 -> 不因 A 放弃 B；
  5) _realm_ready_total 的口径（本区域 vs 全局）。
"""
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("ACCOUNTS_DIR", os.path.join(ROOT, "_acc"))
import qoder_accounts as A
import qoder_proxy as P

PASS = FAIL = SKIP = 0


def check(label, cond, extra=None):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] " + label)
    else:
        FAIL += 1
        print("  [FAIL] %s  %r" % (label, extra))


MODEL = "qwen3.8-flash"
DETAIL = ("{\"code\":\"10605\",\"isQueued\":true,\"retryAfterSeconds\":30,"
          "\"message\":\"queue is full\"}")


def build(realms):
    accs = []
    for i, realm in enumerate(realms):
        a = A.Account({"uid": "%s-%d" % (realm, i), "realm": realm,
                       "accessToken": "dt-%s%d" % (realm, i)})
        a.expires_at = time.time() + 86400
        accs.append(a)
    pool = A.AccountPool(tempfile.mkdtemp(), log=lambda *a, **k: None)
    pool.accounts = accs
    return pool, accs


# ---- 指纹行（与前端 _test_dashboard_*.js 同格式；实现见 tests/_suite_head.py）----
import os as _os_fp
import sys as _sys_fp
_sys_fp.path.insert(0, _os_fp.path.dirname(_os_fp.path.abspath(__file__)))
from _suite_head import declare_suite
declare_suite(__file__, ["qoder_proxy.py", "qoder_accounts.py"])
print("[29] 双区池冷却口径（#29：本区域就绪数，而不是全局账号数）")

# ---- 1) 双区池（1 cn + 1 intl）+ 10605 排队 ----
_pool, _accs = build(["cn", "intl"])
P.POOL = _pool
P._handle_envelope_account_cooldown(_accs[0], P.UpstreamStatus(403, DETAIL),
                                    model=MODEL, session_key=None)
_cd = _accs[0].model_cooldowns.get(MODEL, 0.0) - time.time()
check("双区池：10605 排队冷却 30 -> 3（本区域唯一号 = 单号池）",
      0 < _cd <= 4.0, _cd)
check("双区池：_short_error_cooldown_wait 0 -> 3（补丁 1 单独不够的证据）",
      0 < P._short_error_cooldown_wait("cn", MODEL, exclude=set()) <= 4.0,
      P._short_error_cooldown_wait("cn", MODEL, exclude=set()))

# ---- 2) 单区池回归保护 ----
_pool2, _accs2 = build(["cn"])
P.POOL = _pool2
P._handle_envelope_account_cooldown(_accs2[0], P.UpstreamStatus(403, DETAIL),
                                    model=MODEL, session_key=None)
_cd2 = _accs2[0].model_cooldowns.get(MODEL, 0.0) - time.time()
check("单区池：10605 冷却仍是 3（回归保护）", 0 < _cd2 <= 4.0, _cd2)

# ---- 3) 真 429 长窗：必须保持「不等 + 交回 429」 ----
_pool3, _accs3 = build(["cn"])
P.POOL = _pool3
P._handle_envelope_account_cooldown(
    _accs3[0], P.UpstreamStatus(429, "{\"retryAfterSeconds\":60}"),
    model=MODEL, session_key=None)
_wait3 = P._short_error_cooldown_wait("cn", MODEL, exclude=set())
check("429 长窗（60s）：wait 仍为 0（超预算不吞，交回 429 路径）",
      _wait3 == 0.0, _wait3)
check("429 长窗：realm_model_throttled 仍判为真（429 语义不变）",
      P.realm_model_throttled("cn", MODEL)[0] is True)

# ---- 4) 多号坑：号 A 长频控不应害号 B 也放弃等待 ----
_pool4, _accs4 = build(["cn", "cn"])
P.POOL = _pool4
_accs4[0].model_cooldowns[MODEL] = time.time() + 60
_accs4[1].cooldown_until = time.time() + 3
_wait4 = P._short_error_cooldown_wait("cn", MODEL, exclude=set())
check("多号坑：A 长频控 60s + B 账号冷却 3s -> wait 约 3（不因 A 放弃 B）",
      0 < _wait4 <= 4.0, _wait4)

# ---- 5) 口径单点：_realm_ready_total ----
_pool5, _accs5 = build(["cn", "intl"])
P.POOL = _pool5
check("口径：_realm_ready_total(cn)=1 而全局账号数是 2（正是 #29 的分叉点）",
      P._realm_ready_total("cn", MODEL) == 1
      and len(P.POOL.accounts) == 2,
      (P._realm_ready_total("cn", MODEL), len(P.POOL.accounts)))
check("口径：open_upstream 与冷却函数用同一 helper（静态钉住）",
      open(os.path.join(ROOT, "qoder_proxy.py"), encoding="utf-8")
      .read().count("_realm_ready_total(") >= 3)

print()
print("SUMMARY: TOTAL %d checks, %d passed, %d skipped"
      % (PASS + FAIL + SKIP, PASS, SKIP))
print("RESULT: %s (exit %d)" % ("GREEN" if FAIL == 0 else "RED",
                                1 if FAIL else 0))
sys.exit(1 if FAIL else 0)