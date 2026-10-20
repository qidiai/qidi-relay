"""失败治理三族接线套件（task-74）。

    python tests/_test_failure_families.py

判据设计：**每条调用点一条断言**——
  · 该发生：分类正确（直连 429 -> soft_rate、5xx/传输 -> failure、未知 -> unknown、
    成功 -> success）；
  · 不该发生（比"该发生"更能防住将来的误改）：直连 401/403、信封 429（正常排队）、
    其它 4xx、**402（余额不足）**、内容审核（DataInspectionFailed）、单号池 ——
    三族**零调用**。
不联网：桩 urllib.request.urlopen / SESSIONS / time.sleep，只跑错误分类路径。
"""
import io
import os
import ssl
import sys
import time
import types
import shutil
import tempfile
import urllib.error

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("ACCOUNTS_DIR", os.path.join(ROOT, "_acc"))
import qoder_accounts as A
import qoder_proxy as P

PASS = FAIL = 0


def check(label, ok, detail=None):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  [PASS] " + label)
    else:
        FAIL += 1
        print("  [FAIL] %s  %r" % (label, detail))


# ---------------------------------------------------------------- 桩设施
_KEYS = ("soft", "failure", "unknown", "success", "note_error")
_CALLS = dict((k, 0) for k in _KEYS)
_ORIG = {}


def _install_spies():
    """类级 spy：三族 + note_error 记录调用并**真执行**（行为真实）。"""
    global _CALLS
    _CALLS = dict((k, 0) for k in _KEYS)

    def _wrap(name):
        orig = getattr(A.Account, name)

        def spy(self, *a, **k):
            _CALLS[name.replace("note_", "").replace("error", "note_error")
                   if name == "note_error" else {
                       "note_soft_rate": "soft",
                       "note_failure": "failure",
                       "note_unknown_failure": "unknown",
                       "note_success": "success"}[name]] += 1
            return orig(self, *a, **k)
        return orig, spy

    for name in ("note_soft_rate", "note_failure", "note_unknown_failure",
                 "note_success", "note_error"):
        if name in _ORIG:
            continue
        orig, spy = _wrap(name)
        _ORIG[name] = orig
        setattr(A.Account, name, spy)


def _restore_spies():
    for name, orig in _ORIG.items():
        setattr(A.Account, name, orig)
    _ORIG.clear()


class _Sess(object):
    def headers(self, *a, **k):
        return {"Content-Type": "application/json"}


class _Resp(object):
    def close(self):
        pass

    def read(self, n=None):
        return b"{}"


def _mkpool(n=2, realm="cn"):
    tmp = tempfile.mkdtemp(prefix="fam74-")
    pool = A.AccountPool(tmp, log=lambda *a, **k: None)
    for i in range(n):
        acc = A.Account({"uid": "fam%d" % i, "realm": realm,
                         "accessToken": "dt-x"})
        acc.enabled = True
        acc.expires_at = 0
        pool.accounts.append(acc)
    return pool, tmp


def _http_error(code, detail):
    fp = io.BytesIO((detail or "").encode("utf-8"))
    return urllib.error.HTTPError("https://u.invalid", code, "err", {}, fp)


def _raiser(exc):
    def _f(*a, **k):
        raise exc
    return _f


def _returner(resp):
    def _f(*a, **k):
        return resp
    return _f


_PAYLOAD = {"model": "Qwen3.8-Flash", "messages": [{"role": "user", "content": "hi"}]}


def _run_open_upstream(pool, urlopen_impl, realm="cn"):
    """桩环境跑一次 open_upstream（错误分类路径）；返回捕获的异常或 None。"""
    real_urllib = P.urllib
    real_sessions = P.SESSIONS
    real_time = P.time
    real_pool = P.POOL
    fake_time = types.SimpleNamespace(
        **dict((k, getattr(real_time, k)) for k in dir(real_time)
               if not k.startswith("_")))
    fake_time.sleep = lambda s: None
    P.urllib = types.SimpleNamespace(
        request=types.SimpleNamespace(Request=real_urllib.request.Request,
                                      urlopen=urlopen_impl),
        error=real_urllib.error,
        parse=real_urllib.parse)
    P.SESSIONS = types.SimpleNamespace(get=lambda acct: _Sess())
    P.time = fake_time
    P.POOL = pool
    try:
        P.open_upstream(_PAYLOAD, session_key="fam-test", target_realm=realm)
        err = None
    except Exception as exc:
        err = exc
    finally:
        P.urllib = real_urllib
        P.SESSIONS = real_sessions
        P.time = real_time
        P.POOL = real_pool
    return err


def _run_case(urlopen_impl, n=2, pre=None):
    _install_spies()
    pool, tmp = _mkpool(n=n)
    try:
        if pre:
            pre(pool)
        err = _run_open_upstream(pool, urlopen_impl)
        got = dict(_CALLS)
        state = {"mc": [dict(a.model_cooldowns) for a in pool.accounts]}
    finally:
        _restore_spies()
        shutil.rmtree(tmp, ignore_errors=True)
    return got, err, state


def _run_envelope(status, detail, n=2):
    _install_spies()
    pool, tmp = _mkpool(n=n)
    real_pool = P.POOL
    P.POOL = pool
    try:
        acc = pool.accounts[0]
        exc = types.SimpleNamespace(status=status, detail=detail)
        P._handle_envelope_account_cooldown(acc, exc, model="Qwen3.8-Flash",
                                            session_key=None)
        got = dict(_CALLS)
    finally:
        P.POOL = real_pool
        _restore_spies()
        shutil.rmtree(tmp, ignore_errors=True)
    return got


# ---- 指纹行（与前端 _test_dashboard_*.js 同格式；实现见 tests/_suite_head.py）----
import os as _os_fp
import sys as _sys_fp
_sys_fp.path.insert(0, _os_fp.path.dirname(_os_fp.path.abspath(__file__)))
from _suite_head import declare_suite
declare_suite(__file__, ["qoder_proxy.py", "qoder_accounts.py"])
print("[failure-families] task-74 三族接线")

# ---- A 段：信封层（_handle_envelope_account_cooldown）----
g = _run_envelope(429, "10605 isQueued retryAfterSeconds=30")
check("A1 信封 429（排队）-> 三族零调用（防一次排队冷全账号）",
      g["soft"] == 0 and g["failure"] == 0 and g["unknown"] == 0
      and g["note_error"] >= 1, g)

g = _run_envelope(502, "unknown envelope failure")
check("A2 信封其它非 200 -> note_unknown_failure x1",
      g["unknown"] == 1 and g["soft"] == 0 and g["failure"] == 0, g)

g = _run_envelope(400, "InternalError.Algo.DataInspectionFailed: input text")
check("A3 信封内容审核 -> 三族零调用（内容的错不算账号头上）",
      g["soft"] == 0 and g["failure"] == 0 and g["unknown"] == 0, g)

g = _run_envelope(403, "forbidden")
check("A4 信封 401/403 -> 三族零调用（已有专门路径）",
      g["soft"] == 0 and g["failure"] == 0 and g["unknown"] == 0, g)

g = _run_envelope(502, "unknown envelope failure", n=1)
check("A5 单号池信封其它 -> 三族零调用（total<=1 条件）",
      g["soft"] == 0 and g["failure"] == 0 and g["unknown"] == 0, g)

# ---- B 段：直连层（open_upstream 错误分类）----
got, err, _ = _run_case(_raiser(_http_error(429, "too many requests")))
check("B1 直连 429 -> note_soft_rate x2（每号一次）且 model 级 note_error 仍在",
      got["soft"] == 2 and got["failure"] == 0 and got["unknown"] == 0
      and got["note_error"] >= 1, (got, type(err).__name__ if err else None))

got, err, _ = _run_case(_raiser(_http_error(500, "boom")))
check("B2 HTTP 500（瞬时重试耗尽）-> note_failure x2",
      got["failure"] == 2 and got["soft"] == 0 and got["unknown"] == 0,
      (got, type(err).__name__ if err else None))

got, err, _ = _run_case(_raiser(urllib.error.URLError(ssl.SSLError("EOF"))))
check("B3 传输抖动（TLS EOF 重试耗尽）-> note_failure x2",
      got["failure"] == 2 and got["soft"] == 0 and got["unknown"] == 0,
      (got, type(err).__name__ if err else None))

got, err, _ = _run_case(_raiser(ValueError("weird")))
check("B4 未知异常 -> note_unknown_failure x2",
      got["unknown"] == 2 and got["soft"] == 0 and got["failure"] == 0,
      (got, type(err).__name__ if err else None))

got, err, _ = _run_case(_raiser(_http_error(401, "token expired")))
check("B5 直连 401 -> 三族零调用（防误接）",
      got["soft"] == 0 and got["failure"] == 0 and got["unknown"] == 0
      and got["note_error"] >= 1, (got, type(err).__name__ if err else None))

got, err, _ = _run_case(_raiser(_http_error(402, "insufficient balance")))
check("B6 直连 402（余额不足）-> 三族零调用（最易被顺手写错的一处）",
      got["soft"] == 0 and got["failure"] == 0 and got["unknown"] == 0
      and got["note_error"] >= 1, (got, type(err).__name__ if err else None))


# 注：不给账号预置 model_cooldowns——那会把两号都挡在 ready 之外、选号先失败，
# 成功路径根本走不到（初版曾因此误红）。note_success 的清场语义由
# tests/_test_guards.py 的单测覆盖，这里只钉「成功收尾确实调用复位」。
got, err, state = _run_case(_returner(_Resp()))
check("B7 成功 -> note_success x1（复位关键：退避计数靠它归零）",
      got["success"] == 1 and err is None
      and got["soft"] == 0 and got["failure"] == 0 and got["unknown"] == 0,
      (got, type(err).__name__ if err else None))

got, err, _ = _run_case(_raiser(_http_error(429, "too many requests")), n=1)
check("B8 单号池直连 429 -> 三族零调用（不打死唯一账号）",
      got["soft"] == 0 and got["failure"] == 0 and got["unknown"] == 0, got)

print("")
print("SUMMARY: TOTAL %d checks, %d passed, %d failed" % (PASS + FAIL, PASS, FAIL))
print("RESULT: %s (exit %d)" % ("RED" if FAIL else "GREEN", 1 if FAIL else 0))
sys.exit(1 if FAIL else 0)
