"""pytest 兜底重定向：会话粘性记忆文件（conversations.json）
一律落到临时目录，绝不写仓库根数据文件。

各测试文件 preamble 若自带 CONVERSATIONS_FILE 重定向
（如 test_affinity.py——方案 §7.7 settings 路径常量
注入模式），其后 import 优先生效——本 conftest 只是
兜底：保证任何测试文件（含 T4 之前的老文件，它们
不知道这个新常量）运行时，粘性后台线程的原子落盘
都有处可去（双实例纪律：tmp 名带 pid，互不共写）。
"""

import pathlib
import tempfile

import proxy_core.settings as S

_S_TMP = pathlib.Path(tempfile.mkdtemp(prefix="qidi-affinity-conftest-"))
S.CONVERSATIONS_FILE = _S_TMP / "conversations.json"
