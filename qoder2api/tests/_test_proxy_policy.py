"""出站代理策略套件（#27 / task-83）。

    python tests/_test_proxy_policy.py

判据：**代理只看环境变量、忽略 Windows 注册表；且不固化——每请求现读**。
能红证据（本机可复现，不碰系统设置）：同一进程内构造「全局 opener 固化到
死端口」的状态——旧路径（urllib.request.urlopen）请求必失败（10061 形态）；
修复路径（_opener_for_request）直连可达；设/清 HTTP_PROXY 对修复路径的
**下一次**请求立即生效（无重启）。
"""
import http.server
import os
import sys
import threading
import urllib.request as U

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


class _H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
PORT = srv.server_address[1]
URL = "http://127.0.0.1:%d/" % PORT

_ENV_KEYS = ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy",
             "ALL_PROXY", "all_proxy")
_env_backup = dict((k, os.environ.get(k)) for k in _ENV_KEYS)
for k in _ENV_KEYS:
    os.environ.pop(k, None)

_map = getattr(A, "_proxy_map_from_env", None)
_opf = getattr(A, "_opener_for_request", None)

# ---- 指纹行（与前端 _test_dashboard_*.js 同格式；实现见 tests/_suite_head.py）----
import os as _os_fp
import sys as _sys_fp
_sys_fp.path.insert(0, _os_fp.path.dirname(_os_fp.path.abspath(__file__)))
from _suite_head import declare_suite
declare_suite(__file__, ["qoder_accounts.py"])
print("[proxy-policy] #27 出站代理策略")

check("P1 无 env -> 空映射（直连；不读注册表）",
      _map is not None and _map() == {}, _map() if _map else "<missing>")
os.environ["HTTPS_PROXY"] = "http://127.0.0.1:9"
check("P1b HTTPS_PROXY -> https 映射（env 生效，零代码用法保留）",
      _map is not None and _map() == {"https": "http://127.0.0.1:9"},
      _map() if _map else "<missing>")
os.environ.pop("HTTPS_PROXY")
os.environ["ALL_PROXY"] = "http://127.0.0.1:8"
check("P1c ALL_PROXY -> http+https 双落",
      _map is not None
      and _map() == {"http": "http://127.0.0.1:8",
                     "https": "http://127.0.0.1:8"},
      _map() if _map else "<missing>")
os.environ.pop("ALL_PROXY")

_p2 = None
try:
    with _opf().open(URL, timeout=5) as r:
        _p2 = (r.status, r.read())
except Exception as exc:
    _p2 = "%s: %s" % (type(exc).__name__, exc)
check("P2 修复路径（无 env）直连可达本地服务",
      _p2 == (200, b'{"ok": true}'), _p2)

old_opener = U._opener
U.install_opener(U.build_opener(U.ProxyHandler({"http": "http://127.0.0.1:9"})))
# 环境探测：Windows 上 ProxyHandler 对每个目标先查 proxy_bypass(host)——
# 它读注册表系统代理的 bypass 列表。本机代理工具开着且 bypass 含 <local>/
# localhost 时，127.0.0.1 目标会被绕开死代理 -> urlopen 直连成功 ->
# P3 的「旧路径必败」在这类环境不可测。如实 SKIP（SKIP != PASS）。
_env_bypass = False
try:
    _env_bypass = bool(U.proxy_bypass("127.0.0.1"))
except Exception:
    pass
legacy_ok = False
try:
    U.urlopen(URL, timeout=3)
    legacy_ok = True
except Exception:
    pass
if _env_bypass:
    print("  [SKIP] P3 本机系统代理 bypass 含 loopback（proxy_bypass=1），"
          "旧路径的 10061 判定在该环境不可测")
else:
    check("P3 模拟修复前：全局 opener 固化到死端口 -> 旧路径(urllib.urlopen)"
          "失败（10061 形态）", legacy_ok is False, None)
try:
    with _opf().open(URL, timeout=5) as r2:
        _p3b = (r2.status, r2.read())
except Exception as exc:
    _p3b = "%s: %s" % (type(exc).__name__, exc)
check("P3b 同一固化状态下，修复路径仍直连可达（隔离全局 opener）",
      _p3b == (200, b'{"ok": true}'), _p3b)
U._opener = old_opener

os.environ["HTTP_PROXY"] = "http://127.0.0.1:9"
dead_failed = False
try:
    _opf().open(URL, timeout=3)
except Exception:
    dead_failed = True
os.environ.pop("HTTP_PROXY")
alive = False
try:
    with _opf().open(URL, timeout=5) as r3:
        alive = r3.status == 200
except Exception:
    pass
check("P4 不固化：设死代理 -> 下一次失败；清掉 -> 下一次立即恢复（无重启）",
      dead_failed and alive, (dead_failed, alive))

A.validate_public_http_url = lambda u: None   # 本地回环：测试内放行公网校验
got = None
try:
    got = A.http_json(URL, timeout=5, retries=1)
    hj_ok = True
except Exception:
    hj_ok = False
check("P5 http_json 走修复路径：本地直连成功",
      hj_ok and got == {"ok": True}, got)
os.environ["HTTP_PROXY"] = "http://127.0.0.1:9"
hj_dead_failed = False
try:
    A.http_json(URL, timeout=3, retries=1)
except Exception:
    hj_dead_failed = True
os.environ.pop("HTTP_PROXY")
check("P5b http_json：设死代理 -> 请求失败（env 代理生效证明）", hj_dead_failed, None)

srv.shutdown()
for k, v in _env_backup.items():
    if v is None:
        os.environ.pop(k, None)
    else:
        os.environ[k] = v

print("")
print("SUMMARY: TOTAL %d checks, %d passed, %d failed" % (PASS + FAIL, PASS, FAIL))
print("RESULT: %s (exit %d)" % ("RED" if FAIL else "GREEN", 1 if FAIL else 0))
sys.exit(1 if FAIL else 0)
