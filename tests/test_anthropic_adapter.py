# -*- coding: utf-8 -*-
"""Anthropic Messages 协议适配层的纯单元测试。"""

import json
import pathlib
import sys

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from proxy_core import anthropic  # noqa: E402


def test_anthropic_to_chat_converts_system_media_tools_and_tool_results():
    req = {
        "model": "cline-free/deepseek-v4.1-flash",
        "system": [{"type": "text", "text": "你是助手"}],
        "messages": [
            {"role": "user", "content": [
                {"type": "text", "text": "看图"},
                {"type": "image", "source": {
                    "type": "base64", "media_type": "image/png", "data": "AAAA"}},
            ]},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "toolu_1", "name": "read_file",
                 "input": {"path": "a.txt"}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_1",
                 "content": [{"type": "text", "text": "文件内容"}]},
            ]},
        ],
        "tools": [{"name": "read_file", "description": "读文件",
                   "input_schema": {"type": "object", "properties": {}}}],
        "tool_choice": {"type": "tool", "name": "read_file"},
        "max_tokens": 123,
        "stream": False,
    }
    chat = anthropic.anthropic_to_chat(req)
    assert chat["model"] == req["model"]
    assert chat["max_tokens"] == 123
    assert [m["role"] for m in chat["messages"]] == [
        "system", "user", "assistant", "tool"]
    user_content = chat["messages"][1]["content"]
    assert user_content[0] == {"type": "text", "text": "看图"}
    assert user_content[1]["image_url"]["url"].startswith("data:image/png;base64,")
    tool_call = chat["messages"][2]["tool_calls"][0]
    assert tool_call["function"]["name"] == "read_file"
    assert json.loads(tool_call["function"]["arguments"]) == {"path": "a.txt"}
    assert chat["messages"][3]["tool_call_id"] == "toolu_1"
    assert chat["tool_choice"] == {"type": "function",
                                    "function": {"name": "read_file"}}
    # 工具定义本身必须原样落到上游载荷里：只断言 tool_choice 的话，
    # "整个 tools 不转发"也能全绿，而模型侧看到的是一个没有任何函数的会话
    assert chat["tools"] == [{"type": "function", "function": {
        "name": "read_file", "description": "读文件",
        "parameters": {"type": "object", "properties": {}}}}]


def test_chat_to_anthropic_converts_text_tool_calls_and_usage():
    chat = {
        "id": "chatcmpl-1",
        "model": "upstream-name",
        "choices": [{"finish_reason": "tool_calls", "message": {
            "role": "assistant",
            "content": "先看文件",
            "tool_calls": [{
                "id": "call_1", "type": "function",
                "function": {"name": "read_file",
                             "arguments": '{"path":"a.txt"}'},
            }],
        }}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
    }
    msg = anthropic.chat_to_anthropic(chat, "requested-model")
    assert msg["id"].startswith("msg_")
    assert msg["model"] == "requested-model"
    assert msg["stop_reason"] == "tool_use"
    assert msg["usage"] == {"input_tokens": 11, "output_tokens": 7}
    assert msg["content"][0] == {"type": "text", "text": "先看文件"}
    assert msg["content"][1]["type"] == "tool_use"
    assert msg["content"][1]["input"] == {"path": "a.txt"}


def test_anthropic_sse_bridge_streams_text_tool_args_and_finish():
    bridge = anthropic.AnthropicSSEBridge("cline-free/test")
    out = [bridge.start()]
    out += bridge.feed_payload(json.dumps({
        "choices": [{"delta": {"content": "你好"}}],
    }, ensure_ascii=False))
    out += bridge.feed_payload(json.dumps({
        "choices": [{"delta": {"tool_calls": [{
            "index": 0, "id": "call_1", "type": "function",
            "function": {"name": "read_file", "arguments": '{"path"'},
        }]}}],
    }, ensure_ascii=False))
    out += bridge.feed_payload(json.dumps({
        "choices": [{"delta": {"tool_calls": [{
            "index": 0, "function": {"arguments": ':"a.txt"}'},
        }]}}],
        "usage": {"prompt_tokens": 2, "completion_tokens": 5},
    }, ensure_ascii=False))
    out += bridge.feed_payload(json.dumps({
        "choices": [{"finish_reason": "tool_calls"}],
    }))
    out += bridge.finish()
    body = "".join(out)
    assert "event: message_start" in body
    assert '"type": "text_delta"' in body and "你好" in body
    assert '"type": "tool_use"' in body and "read_file" in body
    assert '"type": "input_json_delta"' in body
    assert '"stop_reason": "tool_use"' in body
    assert '"output_tokens": 5' in body
    assert body.endswith('event: message_stop\ndata: {"type": "message_stop"}\n\n')


def test_estimate_input_tokens_counts_locally_and_never_expands_image_base64():
    """/v1/messages/count_tokens 的口径：CJK 逐字、图片算固定值、thinking 不计。"""

    def build(thinking_len=5000):
        return {
            "system": "你是助手",
            "messages": [
                {"role": "user", "content": [
                    {"type": "text", "text": "a" * 400},
                    {"type": "image", "source": {"type": "base64",
                                                 "media_type": "image/png",
                                                 "data": "A" * 200_000}},
                ]},
                {"role": "assistant", "content": [
                    {"type": "thinking", "thinking": "x" * thinking_len},
                    {"type": "tool_use", "id": "toolu_1", "name": "read_file",
                     "input": {"path": "a.txt"}},
                ]},
                {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_1",
                     "content": [{"type": "text", "text": "文件内容"}]},
                ]},
            ],
            "tools": [{"name": "read_file", "description": "读文件",
                       "input_schema": {"type": "object", "properties": {}}}],
        }

    n = anthropic.estimate_input_tokens(build())
    # 20 万字符的 base64 只按一张图算：逐字符计价会虚高到 5 万以上
    assert anthropic.IMAGE_TOKEN_ESTIMATE < n < anthropic.IMAGE_TOKEN_ESTIMATE + 300
    # thinking 历史块不随请求转发，也不该计价
    assert anthropic.estimate_input_tokens(build(thinking_len=200_000)) == n

    no_image = build()
    no_image["messages"][0]["content"].pop(1)
    assert n - anthropic.estimate_input_tokens(no_image) == anthropic.IMAGE_TOKEN_ESTIMATE

    # 空请求也给 1（客户端拿它做比例运算，返回 0 会引出除零分支）
    assert anthropic.estimate_input_tokens({"messages": []}) == 1
    assert anthropic.estimate_input_tokens({}) == 1


_EST_SYS = "你是助手，回答尽量简短一些，不要废话连篇，直接给结论。"
_EST_TEXT = "a" * 400
_EST_TOOL_NAME = "read_file"
_EST_TOOL_DESC = "读取指定路径文件的完整内容，用于后续的代码分析与问题定位，返回结果包含行号信息。" * 2
_EST_TOOL_INPUT = {"path": "src/main.py", "offset": 120, "limit": 200,
                   "pattern": "def find_all_call_sites_with_a_rather_long_identifier"}
_EST_TR_TEXT = "文件读取成功，以下是内容的完整转述，其中包含若干中文字符用于验证宽字符计价。" * 2
_EST_SCHEMA = {"type": "object",
               "properties": {"path": {"type": "string"},
                              "offset": {"type": "integer"},
                              "limit": {"type": "integer"}},
               "required": ["path"]}


def _est_request(drop=()):
    """按需摘掉某个计价来源后的请求体；drop 里没出现过的名字忽略。"""

    def keep(name):
        return name not in drop

    req = {"model": "GLM-5.3", "messages": []}
    if keep("system"):
        req["system"] = _EST_SYS
    if keep("text"):
        req["messages"].append({"role": "user", "content": [
            {"type": "text", "text": _EST_TEXT}]})
    if keep("tool_use"):
        req["messages"].append({"role": "assistant", "content": [
            {"type": "tool_use", "id": "toolu_1", "name": _EST_TOOL_NAME,
             "input": _EST_TOOL_INPUT}]})
    if keep("tool_result"):
        req["messages"].append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_1",
             "content": [{"type": "text", "text": _EST_TR_TEXT}]}]})
    if keep("tools"):
        req["tools"] = [{"name": _EST_TOOL_NAME, "description": _EST_TOOL_DESC,
                         "input_schema": _EST_SCHEMA}]
    return req


def test_estimate_input_tokens_counts_every_source_marginally():
    """每个计价来源都要单独摘掉量一次边际，只看总量落在宽带里守不住。

    上面那条用例的 `1600 < n < 1900` 与"去掉图片正好少 1600"是同函数相减：
    把 tools 定义或 tool_result 正文整个不计数，总量照样落在带里（变异实测
    腰斩上千 token 仍全绿）。这里逐来源取边际，期望值用测试内独立声明的黄金
    口径算，容差只给 ±1——各段拼成一整串后，单次移除最多差 1 个 token。
    """
    full = anthropic.estimate_input_tokens(_est_request())
    ov = anthropic.MESSAGE_OVERHEAD_TOKENS

    def check(source, expected_str, extra=0):
        ref = _reference_text_tokens(expected_str)
        assert ref > 20, (source, ref)              # 分量太小 = 断言空转
        got = full - anthropic.estimate_input_tokens(_est_request(drop=(source,)))
        assert abs(got - (ref + extra)) <= 1, (source, got, ref + extra)

    check("system", _EST_SYS.strip())
    check("text", "user" + _EST_TEXT, extra=ov)
    check("tool_use", "assistant" + _EST_TOOL_NAME
          + json.dumps(_EST_TOOL_INPUT, ensure_ascii=False), extra=ov)
    check("tool_result", "user" + _EST_TR_TEXT, extra=ov)
    check("tools", _EST_TOOL_NAME + _EST_TOOL_DESC
          + json.dumps(_EST_SCHEMA, ensure_ascii=False))


# 测试内独立声明的黄金口径：宽字符就这几段。产品里 _WIDE_RANGES 若被改错
# （少一段、边界偏一位），下面的对账用例与边界用例都会立刻红——参考实现必须
# 和被测实现不同源，否则"把区间定义改坏"这件事没有任何东西在守。
GOLDEN_WIDE_RANGES = (
    (0x1100, 0x11FF), (0x2E80, 0x303F), (0x3040, 0x30FF), (0x3130, 0x318F),
    (0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xA960, 0xA97F), (0xAC00, 0xD7AF),
    (0xF900, 0xFAFF), (0xFE30, 0xFE4F), (0xFF00, 0xFF60),
)


def _reference_text_tokens(text: str) -> int:
    """独立参考实现：逐字符与黄金区间比较（同时也是被替换掉的旧热路径算法）。"""
    wide = sum(1 for ch in text
               if any(lo <= ord(ch) <= hi for lo, hi in GOLDEN_WIDE_RANGES))
    return wide + (len(text) - wide + 3) // 4


def test_wide_ranges_match_golden_definition():
    """区间定义与测试里的黄金口径逐项一致（改区间必须同时改这里）。"""
    assert tuple(anthropic._WIDE_RANGES) == GOLDEN_WIDE_RANGES
    # 全角标点/汉字/假名各至少一个代表码位，按 1 字符 = 1 token 计
    assert anthropic._text_tokens("一") == 1
    assert anthropic._text_tokens("　、。「】＀￯") == 7
    assert anthropic._text_tokens("aaaa") == 1
    assert anthropic._text_tokens("\U0001F600\U0001F601"
                                  "\U0001F602\U0001F603") == 1


def test_wide_delete_table_covers_golden_ranges_exactly():
    """预编译删除表与黄金口径双向一致——只比 _WIDE_RANGES 守不住表本身。

    表里少一个码位（被 pop、或推导式写成 range(lo, hi) 漏掉上界）时，区间定义
    依然正确，而那批字符会从 1 字 1 token 悄悄掉回 4 字 1 token；这种差异在
    单字符样本上根本量不出来（两种算法都返回 1），只能整表对账。
    """
    expected = {cp for lo, hi in GOLDEN_WIDE_RANGES for cp in range(lo, hi + 1)}
    table = anthropic._WIDE_DELETE_TABLE
    assert set(table) == expected                       # 既没少也没多
    assert all(v is None for v in table.values())        # 非 None 的值不是"删除"


def test_text_tokens_matches_reference_on_every_range_boundary():
    """宽字符计数走的是预编译查表，边界码位必须逐位与比较口径一致。

    少算一位区间边界（如 0x2FFF 被当成宽字符）会让整段英文/标点按 1:1 计价，
    客户端据此做的上下文预算就会歪。
    """
    import random

    samples = ["", "a", "漢", "\U0001F300", "\U0002FFFF", "x漢" * 40,
               "　、。「】＀￯￰", "\n\t  "]
    # 每个边界码位取 4 个重复：1 个字符时宽窄两种口径都得 1，漏掉的码位
    # 要凑够 4 个才会从 1 token 变成 4 token，差异才可见
    for lo, hi in GOLDEN_WIDE_RANGES:
        for cp in (lo - 1, lo, (lo + hi) // 2, hi, hi + 1):
            samples += [chr(cp) * 4, chr(cp) * 4 + "xxxxxxxx"]
    random.seed(20260925)
    samples += ["".join(chr(random.randint(0, 0x2FFFF))
                        for _ in range(random.randint(1, 64)))
                for _ in range(500)]
    for text in samples:
        assert anthropic._text_tokens(text) == _reference_text_tokens(text), repr(text[:24])


def test_text_tokens_avoids_per_character_bytecode_scan():
    """热路径必须是整段一遍扫，不是逐字符跑字节码（count_tokens 体积客户端可控）。

    判据用同一份文本上新旧算法的耗时比：绝对毫秒会随机器负载抖到几倍（旧算法
    在 4MB 上实测 5~12s 都出现过），比值才是算法复杂度的信号。
    """
    import time

    text = ("中英 mixed text 你好世界 " * 60_000)[:1024 * 1024]
    t0 = time.perf_counter()
    expect = _reference_text_tokens(text)          # 旧的逐字符 any() 算法
    slow = time.perf_counter() - t0
    t0 = time.perf_counter()
    got = anthropic._text_tokens(text)             # 现在的预编译查表
    fast = time.perf_counter() - t0
    assert got == expect
    assert fast * 8 < slow, (
        f"逐字符参考算法 {slow:.2f}s，实现 {fast:.2f}s，不到 8 倍——热路径又退化回"
        "单字符循环，一条大请求就能占住一个核心")

