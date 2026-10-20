"""流式出口 tool_call id 保证（issue #47）。

    python tests/_test_stream_tool_ids.py

判据：**出口**（recover_leaked_tool_calls）产出的每个 tool_call 增量都有非空
id；上游给了 id 就原样透传；非 tool_call 帧字节级不变。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _suite_head import *          # noqa: F401,F403

P, A = bootstrap()
declare_suite(__file__, ["qoder_proxy.py"])


def frame(delta, finish=None, cid="c1", model="m"):
    return ("data: " + json.dumps(
        {"id": cid, "object": "chat.completion.chunk", "created": 1,
         "model": model,
         "choices": [{"index": 0, "delta": delta,
                      "finish_reason": finish}]},
        ensure_ascii=False) + "\n\n").encode("utf-8")


def parse(raw):
    text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
    data = P.strip_data_prefix(text)
    if not data or data == "[DONE]":
        return None
    return json.loads(data)


def calls_of(raw):
    obj = parse(raw)
    if not obj:
        return []
    out = []
    for ch in obj.get("choices") or []:
        d = ch.get("delta") or {}
        out.extend(d.get("tool_calls") or [])
    return out


# ---- 1) 上游给了 id -> 字节级原样透传
_ok = frame({"tool_calls": [{"index": 0, "id": "call_upstream_1",
                             "type": "function",
                             "function": {"name": "Read", "arguments": ""}}]})
_out = list(P._sse_fill_tool_call_ids([_ok]))
check("#47 上游 id 存在 -> 帧字节级不变", _out[0] == _ok, _out[0])

# ---- 2) 缺 id -> 补齐非空且带 call_ 前缀
_noid = frame({"tool_calls": [{"index": 0, "type": "function",
                               "function": {"name": "Read",
                                            "arguments": "{\"p\":1}"}}]})
_c = calls_of(list(P._sse_fill_tool_call_ids([_noid]))[0])
check("#47 缺 id -> 补成非空", len(_c) == 1 and bool(_c[0].get("id")), _c)
check("#47 补出来的 id 带 call_ 前缀",
      _c and str(_c[0]["id"]).startswith("call_"), _c)
check("#47 补齐时保留原有 name/arguments",
      _c and _c[0]["function"]["name"] == "Read"
      and _c[0]["function"]["arguments"] == "{\"p\":1}", _c)

# ---- 3) 同一 index 分三帧到达（事故真实形态）-> 三个 id 完全相同
_seq = [
    frame({"tool_calls": [{"index": 0, "type": "function",
                           "function": {"name": "Bash", "arguments": ""}}]}),
    frame({"tool_calls": [{"index": 0,
                           "function": {"arguments": "{\"command\":"}}]}),
    frame({"tool_calls": [{"index": 0,
                           "function": {"arguments": "\"ls\"}"}}]}),
]
_ids = [c.get("id") for f in P._sse_fill_tool_call_ids(_seq) for c in calls_of(f)]
check("#47 同 index 三帧 -> 三个 id 完全相同",
      len(_ids) == 3 and len(set(_ids)) == 1 and bool(_ids[0]), _ids)

# ---- 4) 两个 index -> 各自非空且互不相同
_two = frame({"tool_calls": [
    {"index": 0, "type": "function", "function": {"name": "A", "arguments": ""}},
    {"index": 1, "type": "function", "function": {"name": "B", "arguments": ""}},
]})
_ids2 = [c.get("id") for c in calls_of(list(P._sse_fill_tool_call_ids([_two]))[0])]
check("#47 两个 index -> 两个非空且不同的 id",
      len(_ids2) == 2 and all(_ids2) and _ids2[0] != _ids2[1], _ids2)

# ---- 5) 非 tool_call 流量字节级透传
_plain = frame({"role": "assistant", "content": "hello"})
_done = b"data: [DONE]\n\n"
_beat = b": ping\n\n"
_junk = b"data: not-json\n\n"
check("#47 纯正文帧字节级不变",
      list(P._sse_fill_tool_call_ids([_plain]))[0] == _plain)
check("#47 [DONE] 字节级不变",
      list(P._sse_fill_tool_call_ids([_done]))[0] == _done)
check("#47 心跳注释字节级不变",
      list(P._sse_fill_tool_call_ids([_beat]))[0] == _beat)
check("#47 非 JSON 帧字节级不变（fail-open）",
      list(P._sse_fill_tool_call_ids([_junk]))[0] == _junk)

# ---- 6) 端到端：泄漏回读合成的 tool_calls 也带非空 id
_GREP = json.dumps([{"name": "Read", "arguments": json.dumps(
    {"path": "D:/x.txt"})}], ensure_ascii=False)
_leak = [frame({"content": P.LEAK_MARKER + "\n" + _GREP}, "stop")]
_e2e = [c for f in P.recover_leaked_tool_calls(iter(_leak), {"Read"})
        for c in calls_of(f)]
check("#47 端到端（回读路径）tool_calls 带非空 id",
      len(_e2e) == 1 and bool(_e2e[0].get("id")), _e2e)

# ---- 7) 静态钉住：出口确实是包装器（防重构绕过）
_src = open(os.path.join(ROOT, "qoder_proxy.py"), encoding="utf-8").read()
check("#47 recover_leaked_tool_calls 确实包了 _sse_fill_tool_call_ids",
      "_sse_fill_tool_call_ids(" in _src
      and "_sse_recover_dsml(" in _src
      and "_recover_leaked_tool_calls_inner(frames, allowed_names)" in _src)

finish()
