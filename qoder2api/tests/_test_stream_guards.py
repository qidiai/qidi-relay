# ---- 指纹行（与前端 _test_dashboard_*.js 同格式；实现见 tests/_suite_head.py）----
import os as _os_fp
import sys as _sys_fp
_sys_fp.path.insert(0, _os_fp.path.dirname(_os_fp.path.abspath(__file__)))
from _suite_head import declare_suite
declare_suite(__file__, ["qoder_proxy.py", "qoder_anthropic.py"])
# -*- coding: utf-8 -*-
"""上游异常形状守卫套件（非 dict 帧不得掀翻流式管线）。

    python tests/_test_stream_guards.py

背景：上游 SSE 信封里的 body **不保证是 JSON object**——实测有裸数组/标量形态
（`[1,2,3]`、`42`）。旧实现直接 `obj.get(...)` / `inner.get("usage")`，抛出的
AttributeError 会穿出迭代器，把整个请求打成 500 或直接断流。三处守卫
（`clean_chunk` / `iter_inner_sse` / `aggregate_stream`）把非 dict 帧当作
「不清洗、原样透传」或「跳过」，本套件逐处钉住三件事：

  1. 守卫本体：非 dict 不再抛异常；
  2. 不吞帧、不串味：异常帧前后的正常帧照常产出；
  3. 既有语义不回退：噪声清洗、usage 捕获（首个优先）、正文折叠。

判据层：直接调用三个函数本体（离线纯函数 + 假迭代器），不起 HTTP——异常形状
的问题就出在这一层，拦在 `recover_leaked_tool_calls` 之上会看不到真因。
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _suite_head import *  # noqa: F401,F403

# usage/ 落盘隔离到临时目录：仓库里的 usage/ 在并发编排下是跨进程共享路径，
# 套件不得往里写（见 tests/README.md「迁移期纪律」第 3 条）。
_tmp_guard = tempfile.mkdtemp(prefix="qd-stream-guards-")
os.environ["QD_PROXY_USAGE_DIR"] = _tmp_guard

P, A = bootstrap()


def _envelope(body, status=200):
    """上游信封帧：{"body": <内层 JSON 字符串>, "statusCodeValue": 200}。"""
    return ("data: " + json.dumps({"body": body, "statusCodeValue": status})
            + "\n\n").encode("utf-8")


def _chunk(content):
    return json.dumps({"id": "c", "choices": [
        {"index": 0, "delta": {"content": content}}]})


def _usage_frame(prompt, total):
    return _envelope(json.dumps({
        "id": "c", "choices": [],
        "usage": {"prompt_tokens": prompt, "completion_tokens": 0,
                  "total_tokens": total}}))


def _no_raise(label, fn, *args, **kwargs):
    """跑一个「不该抛异常」的调用，抛了算 FAIL 并继续（而不是整场崩掉）。

    守卫缺失时的表现就是 AttributeError 直接穿出来；若不用这个包装，第一处
    断言就会中断整个套件，看不到后面几处守卫是不是也一起回归了。
    """
    try:
        return True, fn(*args, **kwargs)
    except Exception as exc:                       # noqa: BLE001 - 故意全捕
        check(label, False, "%s: %s" % (type(exc).__name__, exc))
        return False, None


class _Stream:
    """最小上游迭代器：iter_inner_sse 只要求可迭代。"""

    def __init__(self, frames):
        self._frames = list(frames)

    def __iter__(self):
        return iter(self._frames)


try:
    print("[1] clean_chunk：非 dict JSON 原样返回（守卫本体）")
    for _raw in ("[1,2,3]", "42", '"str"', "null", "true", "[]"):
        _ok, _out = _no_raise("clean_chunk(%r) 不抛异常且原样返回" % _raw,
                              P.clean_chunk, _raw)
        if _ok:
            check("clean_chunk(%r) 不抛异常且原样返回" % _raw, _out == _raw,
                  _out)
    check("clean_chunk 非法 JSON 原样返回（既有行为不回退）",
          P.clean_chunk("{not json") == "{not json")

    print()
    print("[2] clean_chunk：正常路径不回退（空噪声仍被清洗）")
    _noisy = json.dumps({"choices": [{"delta": {
        "function_call": {"name": "", "arguments": ""},
        "reasoning_content": ""}}]})
    check("空 function_call 占位 -> 整帧丢弃（返回空串）",
          P.clean_chunk(_noisy) == "", P.clean_chunk(_noisy))
    _mixed = json.dumps({"choices": [{
        "delta": {"content": "hi", "reasoning_content": ""},
        "finish_reason": "stop"}]})
    _mixed_obj = json.loads(P.clean_chunk(_mixed))
    check("非空 delta 只摘噪声键、保留正文与 finish_reason",
          _mixed_obj["choices"][0]["delta"] == {"content": "hi"}
          and _mixed_obj["choices"][0]["finish_reason"] == "stop",
          _mixed_obj)

    print()
    print("[3] iter_inner_sse：非 dict 内层 body 不掀翻管线")
    _ok3, _lines = _no_raise(
        "iter_inner_sse 遇非 dict 内层帧不抛异常（守卫本体）",
        lambda: list(P.iter_inner_sse(
            _Stream([_envelope(_chunk("A")), _envelope("[1,2,3]"),
                     _envelope(_chunk("B"))]), holder={})))
    if _ok3:
        check("异常帧不吞后续帧：三帧都产出（A / 异常 / B）", len(_lines) == 3,
              len(_lines))
        check("非 dict 内层帧原样透传（语义=不清洗，而非丢弃）",
              _lines[1] == b"data: [1,2,3]\n\n", _lines[1])

    print()
    print("[4] iter_inner_sse：非 dict 外层帧跳过（既有行为钉住）")
    _lines2 = list(P.iter_inner_sse(
        [b"data: [1,2,3]\n\n", _envelope(_chunk("Z"))], holder={}))
    check("非 dict 外层只跳过自己，正常帧照常产出",
          len(_lines2) == 1 and b"Z" in _lines2[0], _lines2)

    print()
    print("[5] iter_inner_sse：usage 捕获不回退")
    _h = {}
    list(P.iter_inner_sse(_Stream([_usage_frame(3, 5)]), holder=_h))
    check("usage 记入 holder", (_h.get("usage") or {}).get("total_tokens") == 5)
    _h_anom = {}
    _ok5, _ = _no_raise(
        "非 dict 帧不掀翻 usage 捕获路径",
        lambda: list(P.iter_inner_sse(_Stream([_envelope("[1,2,3]")]),
                                      holder=_h_anom)))
    if _ok5:
        check("非 dict 帧不污染 holder", _h_anom.get("usage") is None)
    _h_after = {}
    list(P.iter_inner_sse(_Stream([_envelope("[1,2,3]"), _usage_frame(3, 5)]),
                          holder=_h_after))
    check("异常帧之后的 usage 仍被捕获",
          (_h_after.get("usage") or {}).get("total_tokens") == 5)
    _h_first = {}
    list(P.iter_inner_sse(_Stream([_usage_frame(3, 5), _usage_frame(9, 18)]),
                          holder=_h_first))
    check("首个 usage 优先（既有语义不回退）",
          (_h_first.get("usage") or {}).get("total_tokens") == 5)
    check("holder=None 时非 dict 帧同样安全",
          len(list(P.iter_inner_sse(_Stream([_envelope("[1,2,3]")]),
                                    holder=None))) == 1)

    print()
    print("[6] aggregate_stream：非 dict chunk 跳过而不中断折叠")
    _ok6, _agg = _no_raise(
        "aggregate_stream 遇非 dict chunk 不抛异常（守卫本体）",
        lambda: P.aggregate_stream(
            _Stream([_envelope(_chunk("A")), _envelope("[1,2,3]"),
                     _envelope(_chunk("B"))]), "m", None, holder={}))
    if _ok6:
        check("异常帧夹在中间：正文仍是 AB、finish=stop",
              _agg["choices"][0]["message"]["content"] == "AB"
              and _agg["choices"][0]["finish_reason"] == "stop",
              _agg["choices"][0])
    _ok6b, _agg_only = _no_raise(
        "aggregate_stream 整条流只有异常帧也不抛异常",
        lambda: P.aggregate_stream(_Stream([_envelope("[1,2,3]")]), "m", None,
                                   holder={}))
    if _ok6b:
        check("整条流只有一个异常帧：空正文 + stop，不抛异常",
              _agg_only["choices"][0]["message"]["content"] == ""
              and _agg_only["choices"][0]["finish_reason"] == "stop",
              _agg_only["choices"][0])

    print()
    print("[7] 集成：异常帧走完整 Anthropic 流式管线"
          "（顺序与 _handle_anthropic 一致）")
    import qoder_anthropic as AN

    _inner = P.sse_with_heartbeat(
        P.recover_leaked_tool_calls(
            P.iter_inner_sse(
                _Stream([_envelope(_chunk("A")), _envelope("[1,2,3]"),
                         _envelope(_chunk("B"))]), holder={}),
            allowed_names=set()),
        lambda b: None)
    _ok7, _out = _no_raise(
        "异常帧走完整 Anthropic 流式管线不抛异常",
        lambda: "".join(f.decode("utf-8")
                        for f in AN.stream_anthropic_events(_inner, "m", {})))
    if _ok7:
        check("管线不抛异常，且异常帧前后的正文都送达客户端",
              '"A"' in _out and '"B"' in _out, _out[:120])
finally:
    shutil.rmtree(_tmp_guard, ignore_errors=True)

finish()
