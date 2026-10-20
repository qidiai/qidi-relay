"""前缀缓存稳定性（issue #48）。

    python tests/_test_prefix_stability.py

判据：**同一条历史消息，在任何请求形态下都必须序列化成完全相同的字节。**
旧实现按「当前轮起点」滑动，用户插一条消息就会把全部历史工具结果重削一遍，
同一条结果在不同轮次字节不同 -> 上游前缀缓存整段失效。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _suite_head import *          # noqa: F401,F403

P, A = bootstrap()
declare_suite(__file__, ["qoder_proxy.py"])

BIG = ("line-%04d: " + "x" * 60 + "\n")


def big_tool(seed, n=140):
    return "".join(BIG % (seed * 1000 + i) for i in range(n))


def history(extra_user=False):
    """3 轮工具调用历史；extra_user=True 模拟「用户插一句话」。"""
    msgs = [{"role": "system", "content": "sys"},
            {"role": "user", "content": "start"}]
    for i in (1, 2, 3):
        msgs.append({"role": "assistant", "content": "",
                     "tool_calls": [{"id": "call_%d" % i, "type": "function",
                                     "function": {"name": "Bash",
                                                  "arguments": "{}"}}]})
        msgs.append({"role": "tool", "tool_call_id": "call_%d" % i,
                     "name": "Bash", "content": big_tool(i)})
    if extra_user:
        msgs.append({"role": "user", "content": "next question"})
    return msgs


def flat(messages, structured=False):
    _sys, out, _imgs = P.flatten_messages(messages, structured=structured)
    return out


def tool_bodies(msgs):
    return [m.get("content") for m in msgs if m.get("role") in ("tool", "user")
            and "line-" in (m.get("content") or "")]


# ---- 1) 两种请求形态下，历史工具结果的字节必须完全一致
for _structured in (False, True):
    a = flat(history(False), structured=_structured)
    c = flat(history(True), structured=_structured)
    # 对比「两边都存在」的工具结果正文（C 多了最后一条 user，不影响前面的）
    a_tools = [m for m in a if "line-" in (m.get("content") or "")]
    c_tools = [m for m in c if "line-" in (m.get("content") or "")]
    same = (len(a_tools) == len(c_tools)
            and all(x.get("content") == y.get("content")
                    for x, y in zip(a_tools, c_tools)))
    check("#48 structured=%s：连续调用 vs 用户插话，历史工具结果字节一致"
          % _structured, same,
          [(len(x.get("content") or ""), len(y.get("content") or ""))
           for x, y in zip(a_tools, c_tools)])

# ---- 2) 省略标记是固定串（不含字数，不随内容变化）
_shrunk = P._shrink_tool_result(big_tool(9), 2000)
check("#48 省略标记不含数字统计", "已省略中间内容" in _shrunk
      and "字符历史工具结果" not in _shrunk, _shrunk[:120])
check("#48 同内容两次削减字节一致",
      P._shrink_tool_result(big_tool(9), 2000) == _shrunk)
check("#48 不同长度内容 -> 标记仍逐字相同（只差首尾正文）",
      P._shrink_tool_result(big_tool(9, 200), 2000)
      == P._shrink_tool_result(big_tool(9, 200), 2000))

# ---- 3) 默认 tail 策略：只保护最后 1 条，其余削
_bodies = tool_bodies(flat(history(False)))
_full = sum(1 for b in _bodies if len(b) > 8000)
check("#48 默认策略下只有最后 1 条工具结果保持完整（其余被削）",
      _full == 1, [len(b) for b in _bodies])

# ---- 4) all 策略：全部削（缓存最稳）
os.environ["QD_TOOL_RESULT_SCOPE"] = "all"
try:
    _all = tool_bodies(flat(history(False)))
    _full_all = sum(1 for b in _all if len(b) > 8000)
    _a2 = tool_bodies(flat(history(False)))
finally:
    os.environ.pop("QD_TOOL_RESULT_SCOPE", None)
check("#48 scope=all：所有工具结果都被削（含最后一条）", _full_all == 0,
      [len(b) for b in _all])
check("#48 scope=all：两次序列化字节一致", _all == _a2)

# ---- 5) turn 策略保留旧行为（兼容开关），并能复现「形态相关」
os.environ["QD_TOOL_RESULT_SCOPE"] = "turn"
try:
    _t_a = tool_bodies(flat(history(False)))
    _t_c = tool_bodies(flat(history(True)))
finally:
    os.environ.pop("QD_TOOL_RESULT_SCOPE", None)
check("#48 scope=turn 仍可复现旧行为（兼容开关有效）",
      any(x != y for x, y in zip(_t_a, _t_c)), [len(b) for b in _t_a])

# ---- 6) keep=0 关闭削减：全文回灌、字节恒定
os.environ["QD_TOOL_RESULT_KEEP"] = "0"
try:
    _off_a = tool_bodies(flat(history(False)))
    _off_c = tool_bodies(flat(history(True)))
finally:
    os.environ.pop("QD_TOOL_RESULT_KEEP", None)
check("#48 keep=0：完全关闭削减且两种形态字节一致", _off_a == _off_c
      and all(len(b) > 8000 for b in _off_a), [len(b) for b in _off_a])

# ---- 7) 静态：滑动规则不再是默认路径
_src = open(os.path.join(ROOT, "qoder_proxy.py"), encoding="utf-8").read()
check("#48 _current_turn_start 只在 scope=turn 分支使用",
      '_scope == "turn"' in _src
      and "_protect = set(range(_current_turn_start" in _src)
check("#48 默认 scope 是 tail", 'or "tail"' in _src)

finish()
