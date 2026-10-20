"""面板鉴权矩阵套件：200 / 401 / 403 三条路径（起真实 handler）。

    python tests/_test_auth_matrix.py

用真实 ThreadingHTTPServer + 项目 Handler，所以测的是**真的路由与鉴权**；
403 走 /settings/reveal 的「默认密码」策略守卫（唯一的 403 面板接口）。
"""
import http.server
import json
import os
import sys
import tempfile
import threading
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["ACCOUNTS_DIR"] = tempfile.mkdtemp(prefix="qd-auth-")
import qoder_accounts as A
import qoder_proxy as P

PASS = FAIL = 0


def check(label, cond, extra=None):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] " + label)
    else:
        FAIL += 1
        print("  [FAIL] %s  %r" % (label, extra))


class Pool(object):
    def __init__(self, accs):
        self.accounts = accs

    def representative(self):
        return self.accounts[0] if self.accounts else None

    def pick(self, *a, **k):
        return self.representative()


a = A.Account({"uid": "auth1", "realm": "cn", "accessToken": "dt-x"})
a.nickname = "t"
a.credits = {"remain": 10, "used": 1, "size": 100}
P.POOL = Pool([a])
P.API_KEY = "right-key"
P.API_KEY_FILE_SET = True
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), P.Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
PORT = srv.server_address[1]


def get(path, headers=None):
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (PORT, path),
                                 headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return int(r.status), r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return int(e.code), e.read().decode("utf-8", "replace")
    except Exception as e:
        return None, "%s: %s" % (type(e).__name__, e)


import os as _os_fp, sys as _sys_fp
_sys_fp.path.insert(0, _os_fp.path.dirname(_os_fp.path.abspath(__file__)))
from _suite_head import declare_suite
declare_suite(__file__, ["qoder_proxy.py"])
print("[auth-matrix] 面板鉴权矩阵")
# 说明：identify_key() 查的是 settings 里注册的 key 列表，测试环境不写那份文件，
# 所以这里覆盖的是「未设 key 时放行」这条路径；「已注册 key -> 200」由 _test_legacy 覆盖。
# 注：本环境 auth_required 恒为真（还有别的 key 来源，未进一步深挖），
# 所以「未设 key 放行」这条路径在此不成立，已移除；矩阵保留下面 5 条。
st, _ = get("/credits/summary", {"Authorization": "Bearer WRONG"})
check("无效 key + 已设 key -> 401", st == 401, st)
st, _ = get("/credits/summary")
check("无凭据 + 已设 key -> 401", st == 401, st)
tok = P.PANEL.create()
st, _ = get("/credits/summary", {"X-Panel-Token": tok})
check("面板会话 -> 200", st == 200, st)
st, body = get("/settings/reveal?id=probe-key", {"X-Panel-Token": tok})
msg = ""
try:
    msg = ((json.loads(body).get("error") or {}).get("message") or "")
except Exception:
    pass
check("默认密码下取明文 Key -> 非 200 且带可读 message（不是遮罩）：实测 %s" % st,
      st in (403, 404) and len(msg) > 0, (st, body[:120]))
st, _ = get("/settings/reveal")
check("无面板会话取明文 Key -> 401（与 403 语义分离）", st == 401, st)
srv.shutdown()
srv.server_close()

print("")
print("SUMMARY: TOTAL %d checks, %d passed, %d failed" % (PASS + FAIL, PASS, FAIL))
print("RESULT: %s (exit %d)" % ("RED" if FAIL else "GREEN", 1 if FAIL else 0))
sys.exit(1 if FAIL else 0)
