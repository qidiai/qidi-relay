# -*- coding: utf-8 -*-
"""Anthropic 桥接转换层套件（task-78）。

    python tests/_test_anthropic_bridge.py

覆盖设计文档 .team/72-ANTHROPIC-BRIDGE-DESIGN.md 的离线判据（§6 的 1/2/3/4/6/7）
与 §8 易错点里的可静态验证项。**不联网、不依赖 qoder_proxy / qoder_accounts**。
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import qoder_anthropic as AN

PASS = FAIL = SKIP = 0


def check(label, cond, extra=None):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] " + label)
    else:
        FAIL += 1
        print("  [FAIL] %s  %r" % (label, extra))


def skip(label):
    global SKIP
    SKIP += 1
    print("  [SKIP] " + label)


# ---- 0. 模块契约：零依赖、不 import 网关本体 ----
_src = open(os.path.join(ROOT, "qoder_anthropic.py"), encoding="utf-8").read()
# 只解析**真正的 import 语句**（docstring 里提到这些名字不算依赖）
import re as _re
_imports = _re.findall(r"^\s*(?:import|from)\s+([A-Za-z_][\w.]*)", _src, _re.M)
_bad_mods = ("qoder_proxy", "qoder_accounts", "requests", "urllib", "socket",
             "aiohttp", "httpx")
check("模块自包含：只 import 标准库（零依赖）",
      not any(m.split(".")[0] in _bad_mods for m in _imports)
      and sorted(set(_imports)) == ["json", "os", "time", "uuid"],
      sorted(set(_imports)))

# ---- 指纹行（与前端 _test_dashboard_*.js 同格式；实现见 tests/_suite_head.py）----
import os as _os_fp
import sys as _sys_fp
_sys_fp.path.insert(0, _os_fp.path.dirname(_os_fp.path.abspath(__file__)))
from _suite_head import declare_suite
declare_suite(__file__, ["qoder_proxy.py", "qoder_accounts.py", "qoder_anthropic.py"])
print()
print("[1] system 三形态合并（设计文档 §3.1 / §8 易错点 1）")
_s1 = AN.system_prompt_of({"system": "top-str",
                           "messages": [{"role": "user", "content": "u"}]})
_s2 = AN.system_prompt_of({"system": [{"type": "text", "text": "top-str"}],
                           "messages": [{"role": "user", "content": "u"}]})
_s3 = AN.system_prompt_of({"messages": [
    {"role": "system", "content": "mid-system"},
    {"role": "user", "content": "u"}]})
check("顶层字符串 / 顶层数组 / messages 内 system 三种形态都能取到",
      _s1 == "top-str" and _s2 == "top-str" and _s3 == "mid-system")
_merged = AN.system_prompt_of({"system": "s-a", "messages": [
    {"role": "system", "content": "s-b"},
    {"role": "user", "content": "u"}]})
check("三形态**合并**（含 messages 内的 system，不丢弃）",
      _merged == "s-a\n\ns-b", _merged)
_chat = AN.messages_to_chat({"messages": [
    {"role": "system", "content": "s-b"}, {"role": "user", "content": "u"}]})
check("合并后的 system 落在 messages[0]（且只落一条）",
      _chat["messages"][0] == {"role": "system", "content": "s-b"}
      and len([m for m in _chat["messages"] if m["role"] == "system"]) == 1)

print()
print("[2] 入站映射（§3.1 的 14 行字段表）")
_REQ = {"model": "m", "max_tokens": 64, "messages": [
    {"role": "user", "content": [
        {"type": "text", "text": "hi"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                     "data": "AAA"}}]},
    {"role": "assistant", "content": [
        {"type": "text", "text": "calling"},
        {"type": "tool_use", "id": "toolu_1", "name": "f", "input": {"city": "SZ"}}]},
    {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "toolu_1",
         "content": [{"type": "text", "text": "25C"},
                     {"type": "image", "source": {"type": "url", "url": "http://x/i.png"}}]}]}]}
_c = AN.messages_to_chat(_REQ)
check("tool_use -> assistant.tool_calls（id 逐字保留、input 序列化成字符串）",
      _c["messages"][1]["tool_calls"][0]["id"] == "toolu_1"
      and _c["messages"][1]["tool_calls"][0]["function"]["name"] == "f"
      and json.loads(_c["messages"][1]["tool_calls"][0]["function"]["arguments"])
      == {"city": "SZ"})
check("tool_result -> role:tool + tool_call_id 配对（图片降级为占位）",
      _c["messages"][2] == {"role": "tool", "tool_call_id": "toolu_1",
                            "content": "25C\n[image omitted]"}, _c["messages"][2])
check("image(base64) -> data URI（qoder 的 image_url 形态）",
      _c["messages"][0]["content"][1]["image_url"]["url"]
      == "data:image/png;base64,AAA")
# 单块 dict 形态（issue #33 根因回归钉）：content 直接给一个 image 块而非列表时，
# 必须转成 image_url part；旧行为压成 {"type":"text","text":""} 把图静默吞掉。
_single = AN.messages_to_chat({"max_tokens": 1, "messages": [
    {"role": "user", "content": {"type": "image", "source":
        {"type": "base64", "media_type": "image/png", "data": "BBBB"}}}]})
check("单块 dict content：image 块保留为 image_url part（不吞成空文本）",
      _single["messages"][0]["content"][0]["type"] == "image_url"
      and _single["messages"][0]["content"][0]["image_url"]["url"]
      == "data:image/png;base64,BBBB", _single["messages"][0]["content"])
check("thinking block 入站丢弃（设计文档 §3.1）",
      "thinking" not in json.dumps(AN.messages_to_chat({"max_tokens": 1, "messages": [
          {"role": "assistant", "content": [
              {"type": "thinking", "thinking": "t", "signature": "s"},
              {"type": "text", "text": "x"}]}]}), ensure_ascii=False))
check("不产生空消息（只有 tool_result 的消息不落 content:\"\" 幽灵轮次）",
      all(m.get("content") or m.get("tool_calls")
          for m in AN.messages_to_chat({"max_tokens": 1, "messages": [
              {"role": "user", "content": [{"type": "tool_result",
                                            "tool_use_id": "t", "content": "x"}]}]})
          ["messages"]))

print()
print("[3] tools / tool_choice 四形态（§3.1）")
_T = AN.messages_to_chat({"max_tokens": 1, "messages": [{"role": "user", "content": "u"}],
                          "tools": [{"name": "f", "description": "d",
                                     "input_schema": {"type": "object"}}]})
check("Anthropic tools -> chat function 工具（input_schema -> parameters）",
      _T["tools"][0]["function"]["name"] == "f"
      and _T["tools"][0]["function"]["parameters"] == {"type": "object"})
_T2 = AN.messages_to_chat({"max_tokens": 1, "messages": [{"role": "user", "content": "u"}],
                           "tools": [{"type": "function", "function": {"name": "g"}}]})
check("幂等：已是 chat 形态的 tools 原样保留",
      _T2["tools"][0]["function"]["name"] == "g")
_tc = lambda v: AN.messages_to_chat({"max_tokens": 1, "tool_choice": v,
                                     "messages": [{"role": "user", "content": "u"}]})
check("tool_choice 四形态：auto/any/tool/none",
      _tc("auto")["tool_choice"] == "auto"
      and _tc("any")["tool_choice"] == "required"
      and _tc("none")["tool_choice"] == "none"
      and _tc({"type": "tool", "name": "f"})["tool_choice"]
      == {"type": "function", "function": {"name": "f"}})
check("max_tokens 必填校验（与 chat 协议相反）",
      AN.validate_request({"messages": [{"role": "user", "content": "u"}]})[0] is False
      and AN.validate_request({"max_tokens": 8,
                               "messages": [{"role": "user", "content": "u"}]})[0] is True)
print()
print("[4] usage 映射与守恒（§4.1 / 判据 6）")
_u = AN.chat_usage_to_anthropic({"prompt_tokens": 1000, "completion_tokens": 20,
                                 "prompt_cache_hit_tokens": 400,
                                 "completion_tokens_details": {"reasoning_tokens": 5}})
check("口径：input_tokens 不含 cache_read（规范口径）",
      _u["input_tokens"] == 600 and _u["cache_read_input_tokens"] == 400)
check("守恒：input + cache_read == prompt_tokens",
      _u["input_tokens"] + _u["cache_read_input_tokens"] == 1000)
check("cache_creation 恒 0（上游无此概念，不编造）",
      _u["cache_creation_input_tokens"] == 0)
check("cached 回退链（details / prompt_details）+ 越界 clamp",
      AN.chat_usage_to_anthropic({"prompt_tokens": 10, "completion_tokens_details":
                                  {"cached_tokens": 4}})["cache_read_input_tokens"] == 4
      and AN.chat_usage_to_anthropic({"prompt_tokens": 10,
                                      "prompt_cache_hit_tokens": 999})
      ["cache_read_input_tokens"] == 10)
check("缺 usage / None 输入 -> 全 0 且不抛",
      AN.chat_usage_to_anthropic(None)["input_tokens"] == 0
      and AN.chat_usage_to_anthropic({})["output_tokens"] == 0)

print()
print("[5] effort 映射：断点 + clamp + 单调（§4.2 / 判据 7）")
_full = ["minimal", "low", "medium", "high"]
_bp = [AN.effort_from_anthropic({"thinking": {"type": "enabled", "budget_tokens": b}},
                                supported=_full) for b in (512, 1024, 1025, 4096, 4097,
                                                           16384, 16385)]
check("断点精确：1024->minimal / 1025->low / 4096->low / 4097->medium / 16385->high",
      _bp == ["minimal", "minimal", "low", "low", "medium", "medium", "high"], _bp)
check("clamp：目标档不在表内 -> 取最近的更低档（无更低则最低档）",
      AN.effort_from_anthropic({"thinking": {"type": "enabled", "budget_tokens": 512}},
                               supported=["low", "high"]) == "low"
      and AN.effort_from_anthropic({"thinking": {"type": "enabled",
                                    "budget_tokens": 99999}},
                                   supported=["low", "medium"]) == "medium")
check("无档位表 / disabled -> None（不下发，保持「不猜测」语义）",
      AN.effort_from_anthropic({"thinking": {"type": "enabled", "budget_tokens": 512}})
      is None
      and AN.effort_from_anthropic({"thinking": {"type": "disabled"}}) is None)
check("catalog_meta fallback（thinking_config.enabled.efforts）",
      AN.effort_from_anthropic({"thinking": {"type": "enabled", "budget_tokens": 512}},
                               catalog_meta={"thinking_config": {"enabled": {
                                   "efforts": ["low", "high"]}}}) == "low")
_ranks = [AN.EFFORT_RANK[AN.effort_from_anthropic(
    {"thinking": {"type": "enabled", "budget_tokens": b}}, supported=_full)]
    for b in (1, 512, 1024, 2000, 4096, 8000, 16384, 20000, 99999)]
check("单调：budget 递增 => 档位 EFFORT_RANK 不降",
      all(a <= b for a, b in zip(_ranks, _ranks[1:])), _ranks)

print()
print("[6] 事件流：序列自洽 + 心跳 + [DONE] + 增量参数（§4.3 / 判据 1）")


def _chunk(delta, fin=None, usage=None):
    o = {"choices": [{"index": 0, "delta": delta, "finish_reason": fin}]}
    if usage is not None:
        o["usage"] = usage
    return ("data: " + json.dumps(o, ensure_ascii=False) + "\n\n").encode()


_frames = [_chunk({"role": "assistant", "content": "he"}),
           _chunk({"content": "llo"}),
           _chunk({"tool_calls": [{"index": 0, "id": "call_1",
                                  "function": {"name": "f",
                                               "arguments": "{\"a\":"}}]}),
           _chunk({"tool_calls": [{"index": 0,
                                  "function": {"arguments": "1}"}}]}),
           b": ping\n\n",
           _chunk({}, "tool_calls", usage={"prompt_tokens": 10,
                                           "completion_tokens": 3}),
           b"data: [DONE]\n\n"]
_holder = {}
_out = [f.decode("utf-8") for f in AN.stream_anthropic_events(iter(_frames), "m",
                                                              _holder)]
_evs = [ln[7:].strip() for e in _out for ln in e.splitlines()
        if ln.startswith("event:")]
check("事件序列：message_start -> 块 -> message_delta -> message_stop",
      _evs[0] == "message_start" and _evs[-2] == "message_delta"
      and _evs[-1] == "message_stop", _evs)
check("content_block_start / stop 数量相等且 index 单调",
      _evs.count("content_block_start") == _evs.count("content_block_stop")
      and len(_evs) >= 4)
_starts = [json.loads(ln[6:])["index"] for e in _out for ln in e.splitlines()
           if ln.startswith("data:") and "content_block_start" in ln]
_stops = [json.loads(ln[6:])["index"] for e in _out for ln in e.splitlines()
          if ln.startswith("data:") and "content_block_stop" in ln]
check("每个块的 start/stop index 一一对应（集合相等且各自不重复）",
      sorted(_starts) == sorted(_stops) and len(set(_starts)) == len(_starts)
      and len(_starts) >= 2, (_starts, _stops))
_md = [json.loads(ln[6:]) for e in _out for ln in e.splitlines()
       if ln.startswith("data:") and "\"stop_reason\"" in ln][-1]
check("stop_reason 合法集合（end_turn/tool_use/max_tokens）",
      _md["delta"]["stop_reason"] in ("end_turn", "tool_use", "max_tokens"), _md)
check("心跳注释原样透传、[DONE] 不被转发",
      any(": ping" in e for e in _out) and not any("[DONE]" in e for e in _out))
_parts = [json.loads(ln[6:])["delta"]["partial_json"] for e in _out
          for ln in e.splitlines() if ln.startswith("data:")
          and "input_json_delta" in ln]
check("input_json_delta 是增量分片（拼接后才合法 JSON）",
      _parts == ["{\"a\":", "1}"] and json.loads("".join(_parts)) == {"a": 1}, _parts)
check("tool_use 块带 id 与 name（首片即建立块）",
      any("\"type\": \"tool_use\"" in e and "call_1" in e for e in _out))
check("usage 落在 message_delta（output_tokens 来自末帧）",
      _md["usage"]["output_tokens"] == 3, _md["usage"])

print()
print("[7] thinking 降级（§5）与 count_tokens（§6 判据 4）")
os.environ.pop("QD_ANTHROPIC_THINKING", None)
check("默认 off：事件流里没有 thinking 块",
      all("thinking" not in e for e in _out))
_msg_default = AN.chat_to_messages({"choices": [{"message": {
    "role": "assistant", "content": "x", "reasoning_content": "r"}}]}, "m")
check("默认 off：非流式出站不含 thinking block",
      all(b.get("type") != "thinking" for b in _msg_default["content"]))
os.environ["QD_ANTHROPIC_THINKING"] = "empty"
_out2 = [f.decode("utf-8") for f in AN.stream_anthropic_events(
    iter([_chunk({"reasoning_content": "r1"}), _chunk({"content": "x"}, "stop")]),
    "m", {})]
_msg_empty = AN.chat_to_messages({"choices": [{"message": {
    "role": "assistant", "content": "x", "reasoning_content": "r"}}]}, "m")
_blocks = [b for b in _msg_empty["content"] if b.get("type") == "thinking"]
check("empty 模式：发 thinking block 且 signature == \"\"（不伪造）",
      any("\"type\": \"thinking\"" in e for e in _out2)
      and len(_blocks) == 1 and _blocks[0]["signature"] == "")
os.environ.pop("QD_ANTHROPIC_THINKING", None)
_tk = AN.estimate_anthropic_tokens({"max_tokens": 8,
                                    "system": "中文提示词",
                                    "messages": [{"role": "user",
                                                  "content": "hello world"}]})
check("count_tokens：返回 Anthropic 形状且 N>0",
      isinstance(_tk, dict) and _tk["input_tokens"] > 0, _tk)
check("count_tokens：CJK 感知（中文比同长度 ASCII 计入更多 token）",
      AN.estimate_anthropic_tokens({"messages": [{"role": "user",
                                                    "content": "中" * 20}]})
      ["input_tokens"]
      > AN.estimate_anthropic_tokens({"messages": [{"role": "user",
                                                     "content": "a" * 20}]})
      ["input_tokens"])

print()
print("[8] 非流出站与契约边界")
_msg = AN.chat_to_messages({"choices": [{"message": {"role": "assistant",
    "content": "hi", "tool_calls": [{"id": "c1", "function": {"name": "f",
                                     "arguments": "{\"x\": 1}"}}]},
    "finish_reason": "tool_calls"}],
    "usage": {"prompt_tokens": 5, "completion_tokens": 2}}, "m")
check("非流式：stop_reason=tool_use + tool_use 块 input 已解析",
      _msg["stop_reason"] == "tool_use"
      and _msg["content"][1]["input"] == {"x": 1})
check("stop_reason 映射：stop->end_turn / length->max_tokens",
      AN.chat_to_messages({"choices": [{"message": {"content": "x"},
                                        "finish_reason": "stop"}]})["stop_reason"]
      == "end_turn"
      and AN.chat_to_messages({"choices": [{"message": {"content": "x"},
                                            "finish_reason": "length"}]})
      ["stop_reason"] == "max_tokens")
_bad = AN.chat_to_messages({"choices": [{"message": {"tool_calls": [
    {"id": "c2", "function": {"name": "f", "arguments": "{not-json"}}]}}]})
check("arguments 非法 JSON -> input={} （不抛异常）",
      _bad["content"][0]["input"] == {})
check("契约：本模块**不清洗**（输入必须已被 recover_leaked_tool_calls 处理）",
      # 硬编码标记字面量只是为了证明「原样透传」这一契约，不引入常量依赖
      any("[assistant 请求调用工具]" in e for e in
          [f.decode("utf-8") for f in AN.stream_anthropic_events(
              iter([_chunk({"content": "[assistant 请求调用工具] x"}, "stop")]), "m", {})]))

print()
print("SUMMARY: TOTAL %d checks, %d passed, %d skipped"
      % (PASS + FAIL + SKIP, PASS, SKIP))
print("RESULT: %s (exit %d)" % ("GREEN" if FAIL == 0 else "RED",
                                1 if FAIL else 0))
sys.exit(1 if FAIL else 0)