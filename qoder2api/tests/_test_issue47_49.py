"""#47 边界（id 兜底四则）与 #49（长会话回声三形态）。

    python tests/_test_issue47_49.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _suite_head import *          # noqa: F401,F403

P, A = bootstrap()
declare_suite(__file__, ["qoder_proxy.py"])

MK = P.LEAK_MARKER
VB = chr(0xFF5C)


def frame(delta, finish=None):
    return ("data: " + json.dumps({"id": "c", "choices": [{"index": 0,
             "delta": delta, "finish_reason": finish}]},
             ensure_ascii=False) + "\n\n").encode()


def calls_of(raw):
    d = P.strip_data_prefix(raw.decode("utf-8", "replace"))
    if not d or d == "[DONE]":
        return []
    o = json.loads(d)
    out = []
    for ch in o.get("choices") or []:
        out.extend((ch.get("delta") or {}).get("tool_calls") or [])
    return out


# ---------------------------------------------------------------- #47 ①
print("[47-1] index 不可哈希不得抛异常")
for bad in ([0], {"a": 1}, "0", None, 0.5, True):
    fr = frame({"tool_calls": [{"index": bad, "type": "function",
                                "function": {"name": "A", "arguments": ""}}]})
    try:
        got = calls_of(list(P._sse_fill_tool_call_ids([fr]))[0])
        ok = got and bool(got[0].get("id"))
    except Exception as exc:
        ok = False
        got = type(exc).__name__
    check("#47 index=%r 不抛异常且补出非空 id" % (bad,), ok, got)

# ---------------------------------------------------------------- #47 ②
print("[47-2] 并行/串行缺 index 不得撞号")
_two = frame({"tool_calls": [
    {"type": "function", "function": {"name": "A", "arguments": ""}},
    {"type": "function", "function": {"name": "B", "arguments": ""}}]})
_i = [c["id"] for c in calls_of(list(P._sse_fill_tool_call_ids([_two]))[0])]
check("#47 同帧两个无 index 调用 -> 两个不同 id",
      len(_i) == 2 and all(_i) and _i[0] != _i[1], _i)

_seq = [frame({"tool_calls": [{"type": "function",
                               "function": {"name": "A", "arguments": ""}}]}),
        frame({"tool_calls": [{"function": {"arguments": "{\"x\":1}"}}]}),
        frame({"tool_calls": [{"type": "function",
                               "function": {"name": "B", "arguments": ""}}]})]
_i2 = [c["id"] for f in P._sse_fill_tool_call_ids(_seq) for c in calls_of(f)]
check("#47 串行两调用（各自带 name、都无 index）-> 不同 id，续帧沿用",
      len(_i2) == 3 and _i2[0] == _i2[1] and _i2[2] != _i2[0], _i2)

# ---------------------------------------------------------------- #47 ③
print("[47-3] 后到的真 id 必须收敛")
_f = [frame({"tool_calls": [{"index": 0, "function": {"name": "A",
                                                     "arguments": ""}}]}),
      frame({"tool_calls": [{"index": 0, "id": "call_late",
                             "function": {"arguments": "{}"}}]}),
      frame({"tool_calls": [{"index": 0,
                             "function": {"arguments": " {}"}}]})]
_ids = [c.get("id") for fr in P._sse_fill_tool_call_ids(_f)
        for c in calls_of(fr)]
check("#47 中途收到真 id 后，后续帧沿用真 id（不回落到旧补值）",
      _ids[1] == "call_late" and _ids[2] == "call_late", _ids)

# 无 index 帧 + index=0 帧归一到同一 key
_mix = [frame({"tool_calls": [{"function": {"name": "A", "arguments": ""}}]}),
        frame({"tool_calls": [{"index": 0,
                               "function": {"arguments": "{}"}}]})]
_mids = [c.get("id") for fr in P._sse_fill_tool_call_ids(_mix)
         for c in calls_of(fr)]
check("#47 缺 index 与 index=0 归一到同一记忆键", _mids[0] == _mids[1], _mids)

# ---------------------------------------------------------------- #49
print("[49] 三形态")
S1 = ("17 张表全部通过。清理临时文件并交付：\n\n\n" + MK + "\n\n" + MK + "\n"
      + '[{"name": "Bash", "arguments": "{\\"command\\":\\"ls\\"}"}]\n'
      + "</" + VB*2 + "DSML" + VB*2 + " parameter>\n"
      + "</" + VB*2 + "DSML" + VB*2 + " calls>\n\n\n")
S2 = ("<" + VB*2 + "DSML" + VB*2 + " calls>\n"
      "<" + VB*2 + "DSML" + VB*2 + ' invoke name="web_search">\n'
      "<" + VB*2 + "DSML" + VB*2 + ' parameter name="query" string="true">q</'
      + VB*2 + "DSML" + VB*2 + " parameter>\n"
      "</" + VB*2 + "DSML" + VB*2 + " invoke>\n"
      "</" + VB*2 + "DSML" + VB*2 + " calls>")
S3 = "用户问：\n\n" + (MK + "\n<terminal></terminal>\n\n") * 54

_c1, _clean1 = P.parse_leaked_tool_calls(S1, {"Bash"})
check("#49 形态1（散文+双 marker+数组+DSML）：恢复为 1 个 Bash",
      bool(_c1) and _c1[0]["function"]["name"] == "Bash", _c1)
check("#49 形态1：clean 只含散文，不含 marker",
      _clean1.strip().startswith("17 张表") and MK not in _clean1,
      repr(_clean1[:60]))
_c2, _ = P.parse_dsml_tool_calls(S2)
check("#49 形态2（纯 DSML 块）：恢复为 web_search",
      bool(_c2) and (_c2[0].get("name") == "web_search"), _c2)
_c3, _ = P.parse_leaked_tool_calls(S3, {"terminal"})
check("#49 形态3（54 组空壳）：无法恢复（保持 fail-open）", not _c3)


def _frames(text, n):
    out = [frame({"content": text[i:i+n]}) for i in range(0, len(text), n)]
    out.append(frame({}, "stop"))
    return out


for _n in (8, 64, 512):
    _got, _txt, _fins = [], [], []
    for _f in P.recover_leaked_tool_calls(iter(_frames(S1, _n)), {"Bash"}):
        _c = calls_of(_f)
        _got.extend(_c)
        _d = P.strip_data_prefix(_f.decode("utf-8", "replace"))
        if _d and _d != "[DONE]":
            _ch = (json.loads(_d).get("choices") or [{}])[0]
            _txt.append((_ch.get("delta") or {}).get("content") or "")
            if _ch.get("finish_reason"):
                _fins.append(_ch["finish_reason"])
    _body = "".join(_txt)
    check("#49 形态1 流式 n=%d：恢复 1 调用、零泄漏、finish=tool_calls" % _n,
          len(_got) == 1 and MK not in _body and "DSML" not in _body
          and _fins == ["tool_calls"], (len(_got), MK in _body, _fins))

# 形态3：不可恢复但必须留痕（节流后至少一条）
P._LEAK_WARN_STATE.update({"last": 0.0, "suppressed": 0, "total": 0})
_warns = []
_orig_log = P.log


def _cap(msg, level="INFO", tag=""):
    if level == "WARN" and "issue #49" in str(msg):
        _warns.append(str(msg))
    try:
        return _orig_log(msg, level=level, tag=tag)
    except Exception:
        return None


P.log = _cap
try:
    list(P.recover_leaked_tool_calls(iter(_frames(S3, 64)), {"terminal"}))
finally:
    P.log = _orig_log
check("#49 形态3：fail-open 透传但留 WARN（方向 C）", len(_warns) >= 1, _warns[:1])
check("#49 WARN 带计数（total/suppressed），不会无限刷屏",
      bool(_warns) and "total=" in _warns[0] and "suppressed=" in _warns[0],
      _warns[:1])

# 严格边界不能被放宽误伤
check("#49 普通散文提到 marker（无数组）仍不恢复",
      P.parse_leaked_tool_calls("说明 " + MK + " 的用法", {"Bash"})[0] is None)
check("#49 无 marker 的 JSON 示例仍不恢复",
      P.parse_leaked_tool_calls('A JSON example: [{"name":"Bash"}]',
                                {"Bash"})[0] is None)
check("#49 未声明工具名仍不恢复",
      P.parse_leaked_tool_calls(MK + '\n[{"name":"Nope","arguments":"{}"}]',
                                {"Bash"})[0] is None)

finish()
