# -*- coding: utf-8 -*-
"""#25 遗留其二：批量刷新入口的账号集合守卫（task 续）。

    python tests/_test_credits_guard.py

钉住三点：① 只对 enabled 且有凭证的号打上游；② 被跳过者在响应里**可区分**；
③ 禁用号/坏凭证号的本地账本**不再被零化**（这是 #25 报的现象）。
"""
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("ACCOUNTS_DIR", os.path.join(ROOT, "_acc"))
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


# ---- 指纹行（与前端 _test_dashboard_*.js 同格式；实现见 tests/_suite_head.py）----
import os as _os_fp
import sys as _sys_fp
_sys_fp.path.insert(0, _os_fp.path.dirname(_os_fp.path.abspath(__file__)))
from _suite_head import declare_suite
declare_suite(__file__, ["qoder_proxy.py"])
print("[#25] 批量刷新入口的账号集合守卫")


class _A:
    def __init__(self, uid, enabled=True, token="dt-x", remain=100, realm="cn"):
        self.uid, self.enabled, self.access_token = uid, enabled, token
        self.realm = realm
        self.credits = {"remain": remain, "used": 1, "size": 200,
                        "updated_at": time.time()}
        self.plan = "Pro"
        self.last_error = ""
        self.calls = 0

    def fetch_credits(self):
        self.calls += 1
        if not self.enabled or not self.access_token:
            self.credits = {"remain": 0, "used": 0, "size": 0,
                            "updated_at": time.time()}   # 上游给的空账本
        else:
            self.credits = {"remain": 999, "used": 1, "size": 1000,
                            "updated_at": time.time()}
        return {"ok": True, "credits": self.credits}

    def fetch_plan(self):
        return {"ok": True}

    def refresh(self):
        return True

    def save(self, d):
        pass


class _Pool:
    def __init__(self, accs):
        self.accounts = accs
        self.affinity = type("X", (), {"unbind": lambda *a: None})()

    def get(self, uid):
        for a in self.accounts:
            if a.uid == uid:
                return a
        return None


_ok = _A("u-ok", remain=100)
_dis = _A("u-disabled", enabled=False, remain=100)
_bad = _A("u-no-cred", token="", remain=100)
P.POOL = _Pool([_ok, _dis, _bad])
P.API_KEY = "k"
P.configured_keys = lambda: []
P.auth_required = lambda: True
P.account_views = lambda *a, **k: []
P.Handler._panel_ok = lambda self: True

# ---- 0) helper 语义 ----
_tg, _sk = P._refreshable_accounts([_ok, _dis, _bad, None])
check("helper：只放行 enabled + 有凭证（None 静默略过，不进 skipped）",
      [a.uid for a in _tg] == ["u-ok"] and len(_sk) == 2, (_tg, _sk))
check("helper：跳过原因可区分（disabled / no_credential）",
      {s["uid"]: s["reason"] for s in _sk}
      == {"u-disabled": "disabled", "u-no-cred": "no_credential"}, _sk)

_srv = P.ThreadingHTTPServer(("127.0.0.1", 0), P.Handler)
_port = _srv.server_address[1]
threading.Thread(target=_srv.serve_forever, daemon=True).start()


def _call(path, body=None, get=False):
    data = None if get else json.dumps(body or {}).encode()
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (_port, path),
                                 data=data,
                                 headers={"Authorization": "Bearer k"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return e.code, {}


# ---- 1) POST /accounts/credits ----
_st, _b = _call("/accounts/credits", {})
_after = {a.uid: a.credits["remain"] for a in (_ok, _dis, _bad)}
check("POST /accounts/credits：只刷新就绪号（results 只含 u-ok）",
      [r["uid"] for r in _b.get("results", [])] == ["u-ok"], _b.get("results"))
check("POST /accounts/credits：skipped 可区分",
      {s["uid"]: s["reason"] for s in _b.get("skipped", [])}
      == {"u-disabled": "disabled", "u-no-cred": "no_credential"})
check("**账本不再被零化**（禁用号/坏凭证号保持原值）",
      _after == {"u-ok": 999, "u-disabled": 100, "u-no-cred": 100}, _after)
check("禁用号与坏凭证号**根本没被打上游**（calls=0）",
      _dis.calls == 0 and _bad.calls == 0, (_dis.calls, _bad.calls))

# ---- 2) GET /accounts/credits ----
_st3, _b3 = _call("/accounts/credits", get=True)
check("GET /accounts/credits：skipped 可区分 + refreshed 计数",
      {s["uid"]: s["reason"] for s in _b3.get("skipped", [])}
      == {"u-disabled": "disabled", "u-no-cred": "no_credential"}
      and _b3.get("refreshed") == 1, _b3)

# ---- 3) POST /accounts/refresh（第三个同类入口） ----
_st4, _b4 = _call("/accounts/refresh", {})
check("POST /accounts/refresh：results 只含就绪号 + skipped 可区分",
      [r["uid"] for r in _b4.get("results", [])] == ["u-ok"]
      and len(_b4.get("skipped", [])) == 2, _b4)

# ---- 4) 显式 uid 指向禁用号 -> 落到 skipped（而不是打上游） ----
_st5, _b5 = _call("/accounts/credits", {"uid": "u-disabled"})
check("显式 uid=禁用号：不打上游、在 skipped 里说明原因",
      not _b5.get("results")
      and _b5.get("skipped", [{}])[0].get("reason") == "disabled", _b5)

_srv.shutdown()
_srv.server_close()

print()
print("SUMMARY: TOTAL %d checks, %d passed, %d skipped"
      % (PASS + FAIL + SKIP, PASS, SKIP))
print("RESULT: %s (exit %d)" % ("GREEN" if FAIL == 0 else "RED",
                                1 if FAIL else 0))
sys.exit(1 if FAIL else 0)