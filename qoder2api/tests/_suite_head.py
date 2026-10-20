# -*- coding: utf-8 -*-
"""套件共享头部：import、计数器、check/skip、结果输出（P0-2 迁移专用）。

套件文件只要两行：

    import os, sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _suite_head import *        # noqa: F401,F403

    P, A = bootstrap()               # 返回 (qoder_proxy, qoder_accounts)
    check("...", cond, extra)
    finish()                         # 打印 SUMMARY/RESULT 并按 FAIL 退出

为什么要显式抽出：第一次迁移 [35]/[39] 时每个套件各自抄一遍头部，结果两份都漏了
import（一份漏 json、一份漏 P）→ 两个套件直接 NameError 全红。共享模块从根上消掉这类抄漏。
"""
import base64
import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import struct
import sys
import tarfile
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PASS = FAIL = SKIP = 0

__all__ = ["base64", "contextlib", "hashlib", "io", "json", "os", "re", "shutil",
           "struct", "sys", "tarfile", "tempfile", "time", "ROOT",
           "bootstrap", "check", "skip", "finish", "declare_suite",
           "PASS", "FAIL", "SKIP"]


def bootstrap(env=None):
    """把 ROOT 放进 sys.path、设好 ACCOUNTS_DIR，导入 P / A 并返回。"""
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    os.environ.setdefault("ACCOUNTS_DIR", os.path.join(ROOT, "_acc"))
    for k, v in (env or {}).items():
        os.environ.setdefault(k, str(v))
    import qoder_accounts as A
    import qoder_proxy as P
    return P, A


def check(label, cond, extra=None):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] " + label)
    else:
        FAIL += 1
        print("  [FAIL] %s  %r" % (label, extra))


def skip(label, reason=""):
    global SKIP
    SKIP += 1
    print("  [SKIP] %s  -- %s" % (label, reason))


def finish():
    print("")
    print("SUMMARY: TOTAL %d checks, %d passed, %d failed, %d skipped"
          % (PASS + FAIL + SKIP, PASS, FAIL, SKIP))
    print("RESULT: %s (exit %d)" % ("RED" if FAIL else "GREEN", 1 if FAIL else 0))
    sys.exit(1 if FAIL else 0)


# ---------------------------------------------------------------------------
# 指纹行（与前端六个 _test_dashboard_*.js 完全同格式）
#
# 为什么要它：本轮出过一次「同一份代码在不同人手里是不同版本」的误报 —— 前端因此给每个
# 套件加了 SUITE: / FILE: / as-of 三件套。后端当时是**对称性缺口**：Python 套件不打印
# 被测文件指纹，所以同样的误报形状在后端只是「还没发生」，不是「不会发生」。
#
# 规矩（已写进 tests/README.md）：每个套件列出它**主要守护**的被测文件（1~4 个），
# 不追求列全；判据是「本套件失败时，看指纹行就知道跑的是哪份产物」。
# ---------------------------------------------------------------------------
def _fp(path):
    raw = open(path, "rb").read()
    return hashlib.md5(raw).hexdigest(), len(raw)


def declare_suite(suite_file, files=()):
    """打印 SUITE:（套件自身）+ FILE:（被测文件）指纹行。

    格式与前端一致：md5 用全文 32 位、bytes 用磁盘字节数、as-of 用 ISO-8601 UTC 带 Z
    （跨机器/时区可直接比大小，避免「同名不同量纲」）。
    """
    try:
        md5, size = _fp(suite_file)
        print("SUITE: %s md5=%s" % (os.path.basename(suite_file), md5))
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        for rel in files:
            p = os.path.join(ROOT, rel)
            try:
                md5, size = _fp(p)
                print("FILE: %s md5=%s bytes=%d as-of=%s" % (rel, md5, size, stamp))
            except OSError:
                print("FILE: %s md5=<missing> bytes=0 as-of=%s" % (rel, stamp))
    except Exception as exc:      # 指纹本身绝不能让套件红
        print("SUITE: %s fingerprint unavailable (%s)" % (os.path.basename(suite_file), exc))
