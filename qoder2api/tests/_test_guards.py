"""护栏套件（P0-1 事实性限额护栏的核心不变量）。

    python tests/_test_guards.py

判据一律拦在**判定函数本身**（reserve_blocked / daily_limit_blocked / ... ），
不依赖网络、不依赖选号路径 —— 这是 C3 那五轮学到的教训：观测点要选在能被
「守卫是否生效」直接决定的那一层。
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


def acc(**kw):
    a = A.Account({"uid": kw.pop("uid", "g1"), "realm": "cn",
                   "accessToken": "dt-x"})
    a.enabled = True
    a.expires_at = time.time() + 3600
    for k, v in kw.items():
        setattr(a, k, v)
    return a


import os as _os_fp, sys as _sys_fp
_sys_fp.path.insert(0, _os_fp.path.dirname(_os_fp.path.abspath(__file__)))
from _suite_head import declare_suite
declare_suite(__file__, ["qoder_accounts.py", "qoder_settings.py"])
print("[guards] 限额护栏不变量")
check("reserve 含等号：remain==reserve -> 拦",
      acc(reserve_credits=100, credits={"remain": 100}).reserve_blocked() is True)
check("reserve 未到地板：remain>reserve -> 放行",
      acc(reserve_credits=100, credits={"remain": 101}).reserve_blocked() is False)
check("reserve None 一律放行（fail-open）",
      acc(reserve_credits=100, credits=None).reserve_blocked() is False)
check("credits 非数值一律放行",
      acc(reserve_credits=100, credits={"remain": "abc"}).reserve_blocked() is False)
check("daily_token 含等号：tokens==limit -> 拦",
      acc(daily_token_limit=1000, daily_tokens_today=1000).daily_limit_blocked() is True)
check("daily_token 999 -> 放行",
      acc(daily_token_limit=1000, daily_tokens_today=999).daily_limit_blocked() is False)
check("daily_credit float 也参与比较（10.0 -> 拦）",
      acc(daily_credit_limit=10, daily_credits_today=10.0).credit_limit_reached() is True)
check("free 豁免：model=None -> 不拦（模型级守卫放行）",
      acc(daily_credit_limit=10, daily_credits_today=10,
          free_models={"m"}).credit_limit_blocked(None) is False)
check("free 豁免生效：免费模型放行、付费模型拦",
      (acc(daily_credit_limit=10, daily_credits_today=10,
           free_models={"m"}).credit_limit_blocked("m") is False
       and acc(daily_credit_limit=10, daily_credits_today=10,
               free_models={"m"}).credit_limit_blocked("paid") is True))
check("阈值全 0 时四条守卫全短路（默认全关零行为变化）",
      (acc(credits={"remain": 0}, daily_tokens_today=99999,
           daily_credits_today=99999).reserve_blocked() is False)
      and acc(daily_token_limit=0, daily_tokens_today=99999).daily_limit_blocked() is False
      and acc(daily_credit_limit=0, daily_credits_today=99999).credit_limit_reached() is False)

print("")
print("SUMMARY: TOTAL %d checks, %d passed, %d failed" % (PASS + FAIL, PASS, FAIL))
print("RESULT: %s (exit %d)" % ("RED" if FAIL else "GREEN", 1 if FAIL else 0))
sys.exit(1 if FAIL else 0)
