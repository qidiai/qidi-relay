# -*- coding: utf-8 -*-
"""issue #37：工具协议回声「内层 arguments 非法」-> fail-closed 回归套件。

    & '.\python\python.exe' tests\_test_issue37_fail_closed.py

判据一句话：**能确定是网关自己的工具协议回声（marker + 合法外层数组 + 已声明工具名），
只是内层 arguments 坏了** -> 必须明确失败（InvalidToolArguments），既不把协议文本漏给
终端用户，也不伪造一个可执行调用，更不能伪装成正常收尾（stop / tool_calls / completed）。

覆盖四个出口（报告者只在流式里发现，另外两个出口同样会走到这条判定）：
  · recover_leaked_tool_calls   流式 chat（含 1/4/16/64/128/512 字符/帧与整段一帧的边界）
  · aggregate_stream            非流式 chat
  · stream_responses_events     Responses 流
  · should_retry_envelope / _handle_envelope_account_cooldown  不许被当成账号问题
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _suite_head import *          # noqa: F401,F403

P, A = bootstrap()
declare_suite(__file__, ["qoder_proxy.py"])

# 三个反引号（代码围栏）：源码里不写字面反引号，避免被各种工具链改写。
FENCE = chr(96) * 3
DSML_TAIL = "</｜DSML｜parameter>"
DSML_TAIL2 = "</｜DSML｜invoke>\n</｜DSML｜calls>"

# 判据里的「用户内容」标记：绝不能出现在固定错误串 / 异常 detail 里。
SECRET = "SECRET_PATH_do_not_leak.py"


# ---------------------------------------------------------------------------
# 帧构造（与 tests/_test_stream_tool_ids.py 同形）
# ---------------------------------------------------------------------------
def frame(delta, finish=None):
    return ("data: " + json.dumps(
        {"id": "c1", "object": "chat.completion.chunk", "created": 1,
         "model": "m",
         "choices": [{"index": 0, "delta": delta,
                      "finish_reason": finish}]},
        ensure_ascii=False) + "\n\n").encode("utf-8")


def frames_for(text, size=None):
    """把正文切成 size 字符一帧；size=None = 整段一帧（服务端分块的两种极端）。"""
    step = size or max(1, len(text))
    chunks = [text[i:i + step] for i in range(0, len(text), step)] or [""]
    out = [frame({"content": c}, "stop" if i == len(chunks) - 1 else None)
           for i, c in enumerate(chunks)]
    out.append(b"data: [DONE]\n\n")
    return out


def echo(items, marker=None):
    """网关 flatten_messages 写进历史的形态：marker + JSON 数组。"""
    return (marker or P.LEAK_MARKER) + json.dumps(items, ensure_ascii=False)


def bad_echo(args_text, name="Write", marker=None):
    return echo([{"name": name, "arguments": args_text}], marker)


def describe(raised, frames, exc=None):
    """把一次运行的产出折成判据用得上的事实集合。"""
    blob = b"".join(frames).decode("utf-8", "replace")
    fins, tcs, events = [], 0, []
    for raw in frames:
        text = raw.decode("utf-8", "replace")
        data = P.strip_data_prefix(text)
        obj = None
        if data and data != "[DONE]":
            try:
                obj = json.loads(data)
            except Exception:
                obj = None
        if isinstance(obj, dict):
            for ch in obj.get("choices") or []:
                if not isinstance(ch, dict):
                    continue
                if (ch.get("delta") or {}).get("tool_calls"):
                    tcs += 1
                if ch.get("finish_reason"):
                    fins.append(ch["finish_reason"])
        for line in text.splitlines():
            if line.startswith("event: "):
                events.append(line[7:].strip())
    return {
        "raised": raised, "exc": exc, "frames": frames, "blob": blob,
        "marker": (P.LEAK_MARKER in blob) or (P.LEAK_MARKER_ALT in blob),
        "finish": fins, "tool_calls": tcs, "events": events,
        "normal_finish": any(x in ("stop", "tool_calls") for x in fins)
                         or ("response.completed" in events),
        "failed_event": "response.failed" in events,
    }


def run_stream(text, size=None, names=("Write",)):
    """跑一遍流式出口：帧照常收集，异常只记事实（不吞掉别的异常）。"""
    got, raised, exc = [], False, None
    allowed = set(names) if names else None
    try:
        for f in P.recover_leaked_tool_calls(iter(frames_for(text, size)), allowed):
            got.append(f)
    except P.InvalidToolArguments as e:
        raised, exc = True, e
    return describe(raised, got, exc)


def envelope(obj):
    """上游信封帧：data: {"statusCodeValue":200,"body":"<内层 chunk JSON>"}。"""
    return ("data: " + json.dumps(
        {"statusCodeValue": 200, "body": json.dumps(obj, ensure_ascii=False)},
        ensure_ascii=False) + "\n\n").encode("utf-8")


def inner_chunk(delta, finish=None):
    return {"id": "c1", "object": "chat.completion.chunk", "created": 1,
            "model": "m",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


def resp_lines(text):
    return [envelope(inner_chunk({"content": text}, "stop")),
            b'data: {"statusCodeValue":200,"body":"[DONE]"}\n\n']


def run_aggregate(text, names=("Write",)):
    raised, exc, obj = False, None, None
    try:
        obj = P.aggregate_stream(iter(resp_lines(text)), "m", None, holder={},
                                 allowed_names=set(names) if names else None)
    except P.InvalidToolArguments as e:
        raised, exc = True, e
    frames = [b"data: " + json.dumps(obj, ensure_ascii=False).encode("utf-8")] if obj else []
    return describe(raised, frames, exc)


def data_line(obj):
    return ("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode("utf-8")


def run_responses(text, names=("Write",)):
    got, raised, exc = [], False, None
    holder = {"allowed_names": set(names) if names else None}
    try:
        for f in P.stream_responses_events(
                iter([data_line(inner_chunk({"content": text}, "stop")),
                      b"data: [DONE]\n\n"]), "m", holder):
            got.append(f)
    except P.InvalidToolArguments as e:
        raised, exc = True, e
    return describe(raised, got, exc)


# ---------------------------------------------------------------------------
# 1) 错误类与固定串
# ---------------------------------------------------------------------------
_exc = P.InvalidToolArguments()
check("#37 P.InvalidToolArguments 存在，是 P.UpstreamStatus 子类",
      isinstance(_exc, P.UpstreamStatus)
      and issubclass(P.InvalidToolArguments, P.UpstreamStatus),
      type(_exc).__mro__)
check("#37 实例 status == 502", _exc.status == 502, _exc.status)
check("#37 实例 detail == P.BAD_ARGS_ERROR_DETAIL",
      _exc.detail == P.BAD_ARGS_ERROR_DETAIL, _exc.detail)
check("#37 detail 是固定串：含 invalid JSON arguments",
      "invalid JSON arguments" in P.BAD_ARGS_ERROR_DETAIL, P.BAD_ARGS_ERROR_DETAIL)
check("#37 detail 里没有任何用户内容（路径 / 工具名 / marker 都不出现）",
      SECRET not in P.BAD_ARGS_ERROR_DETAIL
      and "Write" not in P.BAD_ARGS_ERROR_DETAIL
      and P.LEAK_MARKER not in P.BAD_ARGS_ERROR_DETAIL,
      P.BAD_ARGS_ERROR_DETAIL)
_secret_run = run_stream(bad_echo('{"path": "%s", "body": "x"' % SECRET))
check("#37 抛出的异常 detail 仍是固定串（不把实际参数带出去）",
      _secret_run["raised"]
      and _secret_run["exc"] is not None
      and _secret_run["exc"].detail == P.BAD_ARGS_ERROR_DETAIL
      and SECRET not in _secret_run["exc"].detail
      and SECRET not in _secret_run["blob"],
      _secret_run["exc"].detail if _secret_run["exc"] else None)

# ---------------------------------------------------------------------------
# 2) 五种非法形态 x 七种分块：必须 raise（且不得正常收尾 / 泄漏 marker）
# ---------------------------------------------------------------------------
BAD_FORMS = [
    ("内层真实 LF", '{"body": "line1\nline2"}'),
    ("未转义引号", '{"path": "a"b.py"}'),
    ("内层截断（缺右花括号）", '{"path": "a.py"'),
    ("尾部多余文本", '{"a": 1} trailing'),
    ("非法反斜杠转义", '{"a": \\q}'),
]
CHUNKINGS = [1, 4, 16, 64, 128, 512, None]       # None = 整段一帧

_bad_runs = {}
for _label, _args in BAD_FORMS:
    _text = bad_echo(_args)
    for _size in CHUNKINGS:
        _r = run_stream(_text, _size)
        _bad_runs[(_label, _size)] = _r
        check("#37 raise：%s / %s" % (_label, "整段一帧" if _size is None
                                      else "%d 字符/帧" % _size),
              _r["raised"], {"finish": _r["finish"], "marker": _r["marker"],
                             "tool_calls": _r["tool_calls"]})

for _label, _args in BAD_FORMS:
    _runs = [_bad_runs[(_label, s)] for s in CHUNKINGS]
    check("#37 抛出前无一帧正常收尾（stop/tool_calls）：%s" % _label,
          all(not r["normal_finish"] for r in _runs),
          [r["finish"] for r in _runs])
    check("#37 抛出前不泄漏 marker 文本：%s" % _label,
          all(not r["marker"] for r in _runs),
          [r["blob"][:60] for r in _runs if r["marker"]])


# ---------------------------------------------------------------------------
# 3) 前置正文 / 代码围栏 / DSML 尾标签的组合（报告者指出的分块边界）
# ---------------------------------------------------------------------------
def _fenced(text, lang=True):
    return FENCE + ("json" if lang else "") + "\n" + text + "\n" + FENCE


SHAPES = [
    ("整块围栏（json 标注）", lambda t: _fenced(t, True)),
    ("整块围栏（无标注）", lambda t: _fenced(t, False)),
    ("前置正文+围栏（json）+DSML 尾",
     lambda t: "好的，我来写这个文件。\n\n" + _fenced(t, True) + "\n" + DSML_TAIL),
    ("前置正文+围栏（无标注）+DSML 尾",
     lambda t: "好的，我来写这个文件。\n\n" + _fenced(t, False) + "\n" + DSML_TAIL2),
    ("marker+数组+DSML 尾", lambda t: t + "\n" + DSML_TAIL),
]
SHAPE_DIRTY = '{"a": 1} trailing'
SHAPE_CHUNKINGS = (1, 16, 64, 512, None)

_shape_runs = {}
for _slabel, _build in SHAPES:
    _text = _build(bad_echo(SHAPE_DIRTY))
    for _size in SHAPE_CHUNKINGS:
        _r = run_stream(_text, _size)
        _shape_runs[(_slabel, _size)] = _r
        check("#37 raise：%s / %s" % (_slabel, "整段一帧" if _size is None
                                     else "%d 字符/帧" % _size),
              _r["raised"], {"finish": _r["finish"], "marker": _r["marker"],
                             "blob": _r["blob"][:80]})

for _slabel, _build in SHAPES:
    _runs = [_shape_runs[(_slabel, s)] for s in SHAPE_CHUNKINGS]
    check("#37 组合形态抛出前无正常收尾且不泄漏 marker：%s" % _slabel,
          all((not r["normal_finish"]) and (not r["marker"]) for r in _runs),
          [(r["finish"], r["marker"]) for r in _runs])

# 尖括号 marker 变体（上游另一种形态，issue #36 起归一到方括号）
for _size in (1, None):
    _r = run_stream(bad_echo(SHAPE_DIRTY, marker=P.LEAK_MARKER_ALT), _size)
    check("#37 raise：尖括号 marker 变体 / %s"
          % ("整段一帧" if _size is None else "%d 字符/帧" % _size),
          _r["raised"], {"finish": _r["finish"], "marker": _r["marker"]})

# ---------------------------------------------------------------------------
# 4) 不得 raise 的场景（fail-open 边界：这几种都不是「协议回声 + 坏参数」）
# ---------------------------------------------------------------------------
_valid = run_stream(bad_echo(json.dumps({"path": "a.py"})))
check("#37 合法参数 -> 不 raise，照常还原成 tool_calls 并以 tool_calls 收尾",
      (not _valid["raised"]) and _valid["tool_calls"] == 1
      and _valid["finish"] == ["tool_calls"],
      {"raised": _valid["raised"], "tool_calls": _valid["tool_calls"],
       "finish": _valid["finish"]})

_prose = ("关于 " + P.LEAK_MARKER + " 这个标记，网关会把它转成 tool_calls，"
          "它只是内部协议，用户不该看到。")
_r_prose = run_stream(_prose)
check("#37 普通散文讨论 marker -> 不 raise，正文原样透传",
      (not _r_prose["raised"]) and P.LEAK_MARKER in _r_prose["blob"]
      and _r_prose["tool_calls"] == 0,
      {"raised": _r_prose["raised"], "blob": _r_prose["blob"][:80]})

_demo_items = [{"name": "Write", "arguments": '{"path": "a.py"'}]
_fence_demo = ("示例：\n" + FENCE + "json\n"
               + json.dumps(_demo_items, ensure_ascii=False) + "\n" + FENCE
               + "\n这是合法的 JSON 示例。")
_r_demo = run_stream(_fence_demo)
check("#37 无 marker 的 JSON 示例（带围栏、内层参数非法）-> 不 raise、正文照常透传",
      (not _r_demo["raised"]) and _r_demo["tool_calls"] == 0
      and "示例" in _r_demo["blob"],
      {"raised": _r_demo["raised"], "tool_calls": _r_demo["tool_calls"],
       "blob": _r_demo["blob"][:60]})

_r_undeclared = run_stream(bad_echo('{"path": "a.py"', name="NotDeclared"))
check("#37 未声明的工具名 -> 不 raise（与 #27 的 fail-open 语义一致）",
      (not _r_undeclared["raised"]) and _r_undeclared["tool_calls"] == 0,
      {"raised": _r_undeclared["raised"], "tool_calls": _r_undeclared["tool_calls"]})

_r_other = run_stream(bad_echo(json.dumps({"p": "x"}), name="Read"), names=("Read",))
check("#37 声明的另一个工具 + 合法参数 -> 不 raise，正常还原",
      (not _r_other["raised"]) and _r_other["tool_calls"] == 1,
      {"raised": _r_other["raised"], "tool_calls": _r_other["tool_calls"]})


# ---------------------------------------------------------------------------
# 5) 合法与非法混合批次：必须整体失败，抛出前不得释放任何 tool_calls（防部分执行）
# ---------------------------------------------------------------------------
_mixed = echo([{"name": "Write", "arguments": json.dumps({"path": "a.py"})},
               {"name": "Write", "arguments": '{"p": '}])
_mixed_runs = [(size, run_stream(_mixed, size)) for size in (1, 16, None)]
check("#37 混合批次（一条合法 + 一条坏参数）-> raise",
      all(r["raised"] for _, r in _mixed_runs),
      [(s, r["raised"], r["finish"]) for s, r in _mixed_runs])
check("#37 混合批次抛出前没有任何 tool_calls 帧（不部分执行）",
      all(r["tool_calls"] == 0 for _, r in _mixed_runs),
      [r["tool_calls"] for _, r in _mixed_runs])
check("#37 混合批次抛出前没有正常收尾、也没有 marker 泄漏",
      all((not r["normal_finish"]) and (not r["marker"]) for _, r in _mixed_runs),
      [(r["finish"], r["marker"]) for _, r in _mixed_runs])

# ---------------------------------------------------------------------------
# 6) 重试与账号冷却必须显式排除本异常（不是账号问题，也不许重复计费重发）
# ---------------------------------------------------------------------------
check("#37 should_retry_envelope(attempt=0) is False",
      P.should_retry_envelope(P.InvalidToolArguments(), 0, 0) is False)
check("#37 should_retry_envelope 在已发字节 / 用完重试预算时同样 False",
      P.should_retry_envelope(P.InvalidToolArguments(), 123, 0) is False
      and P.should_retry_envelope(P.InvalidToolArguments(), 0, 99) is False)

_acc = A.Account({"uid": "issue37-uid", "realm": "cn", "accessToken": "tok"})
_before = (_acc.cooldown_until, _acc.last_error)
P._handle_envelope_account_cooldown(_acc, P.InvalidToolArguments())
_after = (_acc.cooldown_until, _acc.last_error)
check("#37 _handle_envelope_account_cooldown 不改 cooldown_until / last_error",
      _before == _after and _after[0] == 0 and _after[1] == "", (_before, _after))

# 对照：真·账号级错误仍然会冷却（防止上面的排除写成「整个函数空转」）
_acc2 = A.Account({"uid": "issue37-uid2", "realm": "cn", "accessToken": "tok"})
_b2 = (_acc2.cooldown_until, _acc2.last_error)
P._handle_envelope_account_cooldown(_acc2, P.UpstreamStatus(429, "throttled"))
check("#37 对照：真正的账号级错误仍会冷却（排除是定向的，不是整个函数空转）",
      _acc2.cooldown_until > _b2[0] and _acc2.last_error != _b2[1],
      (_b2, (_acc2.cooldown_until, _acc2.last_error)))

# ---------------------------------------------------------------------------
# 7) 非流式：aggregate_stream 同样 fail-closed
# ---------------------------------------------------------------------------
_agg_bad = run_aggregate(bad_echo(SHAPE_DIRTY))
check("#37 非流式 aggregate_stream 遇坏参数 -> raise",
      _agg_bad["raised"], {"raised": _agg_bad["raised"],
                           "blob": _agg_bad["blob"][:80]})
_agg_bad_fence = run_aggregate("好的。\n\n" + _fenced(bad_echo(SHAPE_DIRTY), True)
                               + "\n" + DSML_TAIL)
check("#37 非流式 aggregate_stream：正文+围栏+DSML 尾 -> raise",
      _agg_bad_fence["raised"],
      {"raised": _agg_bad_fence["raised"], "blob": _agg_bad_fence["blob"][:80]})
_agg_ok = run_aggregate(bad_echo(json.dumps({"path": "a.py"})))
check("#37 非流式合法参数 -> 不 raise，正常折叠成 tool_calls",
      (not _agg_ok["raised"]) and '"tool_calls"' in _agg_ok["blob"],
      _agg_ok["blob"][:80])

# ---------------------------------------------------------------------------
# 8) Responses 流：stream_responses_events 同样 fail-closed
# ---------------------------------------------------------------------------
_resp_bad = run_responses(bad_echo(SHAPE_DIRTY))
check("#37 Responses 遇坏参数 -> raise", _resp_bad["raised"],
      {"raised": _resp_bad["raised"], "events": _resp_bad["events"][-3:]})
check("#37 Responses 抛出前没有 response.completed / response.failed",
      (not _resp_bad["normal_finish"]) and (not _resp_bad["failed_event"]),
      _resp_bad["events"][-4:])
check("#37 Responses 抛出前不泄漏 marker 文本", not _resp_bad["marker"],
      _resp_bad["blob"][:80])
_resp_fence = run_responses("好的。\n\n" + _fenced(bad_echo(SHAPE_DIRTY), True)
                            + "\n" + DSML_TAIL)
check("#37 Responses：正文+围栏+DSML 尾 -> raise", _resp_fence["raised"],
      {"raised": _resp_fence["raised"], "events": _resp_fence["events"][-3:]})
_resp_ok = run_responses(bad_echo(json.dumps({"path": "a.py"})))
check("#37 Responses 合法参数 -> 不 raise，正常完成",
      (not _resp_ok["raised"]) and "response.completed" in _resp_ok["events"],
      _resp_ok["events"][-3:])

finish()
