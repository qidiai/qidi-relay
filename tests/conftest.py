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
import sys
import tempfile

# 跨目录修复（任务 0）：从其他目录跑
# pytest <绝对路径>/tests 时 CWD 不是仓库根，
# 必须先把仓库根塞进 sys.path 再 import，
# 否则 import proxy_core.settings 直接
# ModuleNotFoundError。
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import proxy_core.settings as S

_S_TMP = pathlib.Path(tempfile.mkdtemp(prefix="qidi-affinity-conftest-"))
S.CONVERSATIONS_FILE = _S_TMP / "conversations.json"
S.COOLDOWNS_FILE = _S_TMP / "cooldowns.json"   # 冷却状态持久化（吸收-1 §R-A）
