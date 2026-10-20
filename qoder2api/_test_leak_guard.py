"""回读守卫回归：真实泄漏形态走完整流式管线后，客户端到底看到什么。

五个泄漏形态取自 2026-10-04 的生产会话（Hermes 长会话 → 本网关 → Qwen3.8-Flash，
msgs 180–215，输入 95–105k token）；三条误伤用例对应修 B1 时暴露的重复输出缺陷
（收尾帧重发路径存的是原帧）。

与 `_test_qoder.py` 的分工：那边多数断言直接调判据函数（离线判定），本文件把同样的
形态喂进 `recover_leaked_tool_calls()` 走完整 SSE 管线、按客户端视角拼回正文 —— 判据
写对了但管线接线错了时，只有这一层会红。可在发布镜像内直接跑（纯标准库，无 fixture）：

    python _test_leak_guard.py                       # 退出码 0=GREEN，1=RED
    docker exec -i qoder-proxy python3 - < _test_leak_guard.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import qoder_proxy as qp                                     # noqa: E402

PASS = FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] %s" % label)
    else:
        FAIL += 1
        print("  [FAIL] %s%s" % (label, ("  <- %r" % (extra,)) if extra != "" else ""))


NAMES = {"terminal", "write_file", "read_file", "search_files", "patch"}
MARK = qp.LEAK_MARKER
PROSE = "edge 端点有响应（400/406 = 服务器活着，只是协议不对）。调整参数重试。\n"

# 截断落在字符串中间（最普通的截断）
T_MID = '[{"name": "write_file", "arguments": "{\\"content\\":\\"DoH v2 edge probe'
# 截断落在未写完的 \uXXXX 转义里（生产样本正文的结尾正是 "\u63a2"）
T_ESC = '[{"name": "write_file", "arguments": "{\\"content\\":\\"DoH v2 edge \\u63a2'
# 模型回显「工具结果」这套写入格式（含自造的开闭对）
CASE_C = ('[工具结果]\n{"output": "https://edge.example/probe GET: 0.38s rcode=0 len=41"}\n'
          '[工具结果结束]\n<system_warning>Silence token detected</system_warning>')

CASES = [
    ("A  marker 开头 + 普通截断", [MARK + "\n" + T_MID], ""),
    ("B1 散文 + marker 同帧", [PROSE + MARK + "\n" + T_MID], PROSE),
    ("B2 散文与 marker 分帧", [PROSE, MARK, "\n", T_MID], PROSE),
    ("B3 marker 开头 + 截断落在 \\uXXXX 转义里", [MARK + "\n" + T_ESC], ""),
    ("C  [工具结果] 回声", [CASE_C], ""),
]

# 误伤用例：正常文本，绝不该被吞、也不该重复输出
FALSE_POSITIVES = [
    ("D1 正常数组文本（以 [ 结尾 + 下一帧续写）",
     ["看这个数组：\n[", "1,2,3]"], "看这个数组：\n[1,2,3]"),
    ("D2 正文含完整数组", ["结果是 [1,2,3]，已完成。"], "结果是 [1,2,3]，已完成。"),
    ("D3 讨论该标记的散文（marker 后接自然语言）",
     ["我们在讨论 " + MARK + " 这个标记本身，不要吞。"],
     "我们在讨论 " + MARK + " 这个标记本身，不要吞。"),
]


def frames(chunks):
    out = ["data: " + json.dumps(
        {"id": "x", "model": "qfmodel", "created": 1,
         "choices": [{"index": 0, "delta": {"content": c}, "finish_reason": None}]},
        ensure_ascii=False) + "\n\n" for c in chunks]
    out.append('data: {"id":"x","model":"qfmodel","created":1,'
               '"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n')
    out.append("data: [DONE]\n\n")
    return [f.encode("utf-8") for f in out]


def client_sees(chunks):
    """按客户端视角拼回正文，并数出 tool_calls 增量帧。"""
    text, calls = "", 0
    for raw in qp.recover_leaked_tool_calls(frames(chunks), NAMES):
        s = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
        if not s.startswith("data: "):
            continue
        p = s[6:].strip()
        if p == "[DONE]":
            continue
        try:
            d = json.loads(p)
        except Exception:
            continue
        dl = ((d.get("choices") or [{}])[0].get("delta") or {})
        if dl.get("content"):
            text += dl["content"]
        if dl.get("tool_calls"):
            calls += len(dl["tool_calls"])
    return text, calls


print("[1] 泄漏形态：网关内部协议文本不得透传给客户端")
for tag, chunks, want_prose in CASES:
    text, _calls = client_sees(chunks)
    leaked = MARK in text or qp.TOOL_RESULT_MARKER in text
    check("%s → 不泄漏" % tag, not leaked, text[:80])
    if want_prose:
        check("%s → 标记前的散文逐字保留" % tag, text == want_prose, text)

print()
print("[2] 误伤方向：正常文本必须逐字不变（含重复输出）")
for tag, chunks, want in FALSE_POSITIVES:
    text, _calls = client_sees(chunks)
    check("%s → 逐字一致" % tag, text == want, (want, text))

print()
print("SUMMARY: TOTAL %d checks, %d passed, %d failed" % (PASS + FAIL, PASS, FAIL))
print("RESULT: %s (exit %d)" % ("RED" if FAIL else "GREEN", 1 if FAIL else 0))
sys.exit(1 if FAIL else 0)
