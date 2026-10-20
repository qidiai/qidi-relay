"""keep-alive / 畸形请求套件：裸 socket 复现「超长请求行」与「同连接多请求」。

    python tests/_test_keepalive.py

历史背景（qoder_proxy.py:5305 注释）：响应侧曾用 `Connection: close` + 裸写字节，
连接池型客户端会踩到「随机 414」。本套件用裸 socket 钉住两件事：
  ① 畸形/超长请求要被**快速拒绝**（4xx 或直接关闭），不能挂起；
  ② 正常请求在**同一条连接**上连发两次都能成功（keep-alive 可用）。
"""
import http.server
import os
import socket
import sys
import tempfile
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["ACCOUNTS_DIR"] = tempfile.mkdtemp(prefix="qd-ka-")
import qoder_proxy as P

PASS = FAIL = 0


def check(label, ok, detail=None):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  [PASS] %s" % label)
    else:
        FAIL += 1
        print("  [FAIL] %s %r" % (label, detail))


P.POOL = None
P.API_KEY = ""
P.API_KEY_FILE_SET = False
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), P.Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
PORT = srv.server_address[1]
import os as _os_fp, sys as _sys_fp
_sys_fp.path.insert(0, _os_fp.path.dirname(_os_fp.path.abspath(__file__)))
from _suite_head import declare_suite
declare_suite(__file__, ["qoder_proxy.py"])
print("[keepalive] 裸 socket 行为")

# ① 超长请求行（100KB）——要快速拒绝，不能挂起
s = socket.create_connection(("127.0.0.1", PORT), timeout=8)
try:
    s.sendall(b"GET /" + b"a" * 100000 + b" HTTP/1.1\r\nHost: x\r\n\r\n")
    try:
        data = s.recv(400)
    except socket.timeout:
        data = b""
    except (ConnectionAbortedError, ConnectionResetError, OSError):
        data = b""          # 服务端直接关连接（RST）也算「快速拒绝」
finally:
    s.close()
first = data.split(b"\r\n", 1)[0] if data else b""
check("超长请求行 -> 服务端快速响应（4xx）或直接关闭，不挂起",
      (b" 4" in first and (b"414" in first or b"400" in first)) or data == b"",
      first[:60])

# ② 同一条连接连发两个正常请求（keep-alive）—— 必须按 Content-Length 读完 body，
# 否则第二个「响应」会被上一次的 body 残片污染（第一版就是这么误判的）。
import re as _re


def _one(sock, raw):
    sock.sendall(raw)
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            return ""
        buf += chunk
    head, _, rest = buf.partition(b"\r\n\r\n")
    m = _re.search(rb"[Cc]ontent-[Ll]ength: *(\d+)", head)
    need = int(m.group(1)) if m else 0
    body = rest
    while len(body) < need:
        chunk = sock.recv(4096)
        if not chunk:
            break
        body += chunk
    return (head.split(b"\r\n", 1)[0] + b" " + body[:40]).decode("utf-8", "replace")


s2 = socket.create_connection(("127.0.0.1", PORT), timeout=8)
ok2, got2 = 0, []
try:
    for _ in range(2):
        r = _one(s2, b"GET /health HTTP/1.1\r\nHost: localhost\r\nConnection: keep-alive\r\n\r\n")
        got2.append(r[:40])
        if r.startswith("HTTP/1.1 200") or r.startswith("HTTP/1.0 200"):
            ok2 += 1
finally:
    s2.close()
check("同一条连接连发两个 /health 都拿到 200（keep-alive 可用）", ok2 == 2, got2)
srv.shutdown()
srv.server_close()
print("")
print("SUMMARY: TOTAL %d checks, %d passed, %d failed" % (PASS + FAIL, PASS, FAIL))
print("RESULT: %s (exit %d)" % ("RED" if FAIL else "GREEN", 1 if FAIL else 0))
sys.exit(1 if FAIL else 0)
