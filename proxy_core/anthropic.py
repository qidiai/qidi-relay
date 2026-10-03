# -*- coding: utf-8 -*-
"""Anthropic Messages API ↔ OpenAI chat completions 协议适配层。

让局域网内只实现 Anthropic /v1/messages 的客户端（含 Cline 的 Anthropic
Provider 模式）也能直接接入本代理。这里只做协议翻译；模型路由、鉴权、
冷却、用量统计仍复用 routes.proxy._proxy_chat 的既有链路。
"""

import json
import time
import uuid


# ─── 请求方向：Anthropic → OpenAI ─────────────────────────────────────────────


def _system_text(system) -> str:
    """Anthropic system 支持字符串或 content block 列表，统一提取文本。"""
    if isinstance(system, str):
        return system.strip()
    if isinstance(system, list):
        parts = []
        for p in system:
            if isinstance(p, dict) and p.get("type") == "text":
                parts.append(str(p.get("text") or ""))
        return "\n".join(x for x in parts if x).strip()
    return ""


def _image_part(part: dict) -> dict | None:
    """Anthropic image block → OpenAI image_url part。"""
    src = part.get("source") or {}
    if not isinstance(src, dict):
        return None
    url = ""
    if src.get("type") == "base64" and src.get("data"):
        media_type = src.get("media_type") or "image/png"
        url = f"data:{media_type};base64,{src['data']}"
    elif src.get("type") == "url" and src.get("url"):
        url = str(src["url"])
    if not url:
        return None
    return {"type": "image_url", "image_url": {"url": url}}


def _tool_result_text(content) -> str:
    """tool_result 的 content 可为字符串或 text block 列表。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict) and p.get("type") == "text":
                parts.append(str(p.get("text") or ""))
            elif isinstance(p, str):
                parts.append(p)
        return "\n".join(x for x in parts if x)
    if content is None:
        return ""
    return json.dumps(content, ensure_ascii=False)


def _assistant_from_anthropic(msg: dict) -> dict:
    """assistant 消息：text + tool_use → OpenAI content + tool_calls。"""
    content = msg.get("content")
    if isinstance(content, str):
        return {"role": "assistant", "content": content}
    text_parts, tool_calls = [], []
    if isinstance(content, list):
        for p in content:
            if not isinstance(p, dict):
                continue
            if p.get("type") == "text":
                text_parts.append(str(p.get("text") or ""))
            elif p.get("type") == "tool_use":
                args = p.get("input")
                tool_calls.append({
                    "id": str(p.get("id") or f"call_{uuid.uuid4().hex[:12]}"),
                    "type": "function",
                    "function": {
                        "name": str(p.get("name") or ""),
                        "arguments": json.dumps(args if args is not None else {},
                                                ensure_ascii=False),
                    },
                })
    out = {"role": "assistant",
           "content": "\n".join(x for x in text_parts if x) if text_parts else None}
    if tool_calls:
        out["tool_calls"] = tool_calls
    elif out["content"] is None:
        out["content"] = ""
    return out


def _user_from_anthropic(msg: dict) -> list:
    """user 消息可混有 text/image/tool_result；OpenAI 需拆成 user/tool 多条。"""
    content = msg.get("content")
    if not isinstance(content, list):
        return [{"role": "user", "content": "" if content is None else str(content)}]
    out, user_parts = [], []

    def flush_user():
        nonlocal user_parts
        if user_parts:
            out.append({"role": "user", "content": user_parts})
            user_parts = []

    for p in content:
        if isinstance(p, str):
            user_parts.append({"type": "text", "text": p})
            continue
        if not isinstance(p, dict):
            continue
        ptype = p.get("type")
        if ptype == "text":
            user_parts.append({"type": "text", "text": str(p.get("text") or "")})
        elif ptype == "image":
            image = _image_part(p)
            if image:
                user_parts.append(image)
        elif ptype == "tool_result":
            flush_user()
            text = _tool_result_text(p.get("content"))
            if p.get("is_error"):
                text = "[tool error] " + text
            out.append({
                "role": "tool",
                "tool_call_id": str(p.get("tool_use_id") or p.get("id") or ""),
                "content": text,
            })
    flush_user()
    return out or [{"role": "user", "content": ""}]



def anthropic_to_chat(data: dict) -> dict:
    """Anthropic Messages 请求体 → OpenAI chat/completions 请求体。"""
    msgs = []
    system = _system_text(data.get("system"))
    if system:
        msgs.append({"role": "system", "content": system})
    for m in data.get("messages") or []:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if role == "assistant":
            msgs.append(_assistant_from_anthropic(m))
        elif role == "user":
            msgs.extend(_user_from_anthropic(m))

    chat = {
        "model": str(data.get("model") or ""),
        "messages": msgs,
        "stream": bool(data.get("stream")),
    }
    if data.get("max_tokens") is not None:
        chat["max_tokens"] = data.get("max_tokens")
    for key in ("temperature", "top_p", "presence_penalty", "frequency_penalty"):
        if data.get(key) is not None:
            chat[key] = data[key]
    if isinstance(data.get("stop_sequences"), list) and data["stop_sequences"]:
        chat["stop"] = data["stop_sequences"]
    meta = data.get("metadata") or {}
    if isinstance(meta, dict) and meta.get("user_id"):
        chat["user"] = str(meta["user_id"])

    tools = []
    for t in data.get("tools") or []:
        if not isinstance(t, dict) or not t.get("name"):
            continue
        tools.append({
            "type": "function",
            "function": {
                "name": str(t["name"]),
                "description": str(t.get("description") or ""),
                "parameters": t.get("input_schema") or {"type": "object", "properties": {}},
            },
        })
    if tools:
        chat["tools"] = tools

    tc = data.get("tool_choice")
    if isinstance(tc, dict):
        ttype = tc.get("type")
        if ttype in ("auto", "none"):
            chat["tool_choice"] = ttype
        elif ttype == "any":
            chat["tool_choice"] = "required"
        elif ttype == "tool" and tc.get("name"):
            chat["tool_choice"] = {"type": "function",
                                    "function": {"name": str(tc["name"])}}

    # Anthropic thinking → 本代理/上游已有的 reasoning_effort 口径
    thinking = data.get("thinking")
    if isinstance(thinking, dict):
        if thinking.get("type") == "enabled":
            chat.setdefault("reasoning_effort", "high")
        elif thinking.get("type") == "disabled":
            chat.setdefault("reasoning_effort", "off")
    return chat


# ─── 响应方向：OpenAI → Anthropic ─────────────────────────────────────────────


def _message_text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict) and p.get("type") == "text":
                parts.append(str(p.get("text") or ""))
            elif isinstance(p, str):
                parts.append(p)
        return "\n".join(x for x in parts if x)
    return ""


def _stop_reason(openai_finish: str | None, has_tool_calls: bool = False) -> str:
    if has_tool_calls or openai_finish == "tool_calls":
        return "tool_use"
    return {
        "stop": "end_turn",
        "length": "max_tokens",
        "content_filter": "refusal",
        None: "end_turn",
    }.get(openai_finish, "end_turn")


def chat_to_anthropic(chat: dict, requested_model: str) -> dict:
    """OpenAI chat completion → Anthropic message 对象。"""
    choice = ((chat.get("choices") or [{}])[0] or {})
    msg = choice.get("message") or {}
    text = _message_text(msg)
    blocks = []
    if text:
        blocks.append({"type": "text", "text": text})
    tool_calls = msg.get("tool_calls") or []
    for tc in tool_calls:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or {}
        raw_args = fn.get("arguments") or "{}"
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            if not isinstance(args, dict):
                args = {"value": args}
        except Exception:
            args = {}
        blocks.append({
            "type": "tool_use",
            "id": str(tc.get("id") or f"toolu_{uuid.uuid4().hex[:12]}"),
            "name": str(fn.get("name") or ""),
            "input": args,
        })
    if not blocks:
        blocks.append({"type": "text", "text": ""})
    usage = chat.get("usage") or {}
    return {
        "id": chat.get("id") if str(chat.get("id") or "").startswith("msg_")
             else f"msg_{uuid.uuid4().hex}",
        "type": "message",
        "role": "assistant",
        "model": requested_model or chat.get("model") or "",
        "content": blocks,
        "stop_reason": _stop_reason(choice.get("finish_reason"), bool(tool_calls)),
        "stop_sequence": None,
        "usage": {
            "input_tokens": int(usage.get("prompt_tokens") or 0),
            "output_tokens": int(usage.get("completion_tokens") or 0),
        },
    }


def error_payload(message: str, err_type: str = "api_error") -> dict:
    """Anthropic 风格错误体。"""
    return {"type": "error", "error": {"type": err_type, "message": message}}


def error_from_openai(payload: dict, fallback: str = "upstream error") -> dict:
    """把 OpenAI error 对象映射成 Anthropic error 对象。"""
    err = (payload or {}).get("error") or {}
    if not isinstance(err, dict):
        return error_payload(str(err or fallback))
    raw_type = str(err.get("type") or "api_error")
    type_map = {
        "invalid_request_error": "invalid_request_error",
        "invalid_api_key": "authentication_error",
        "rate_limit_error": "rate_limit_error",
    }
    return error_payload(str(err.get("message") or fallback),
                         type_map.get(raw_type, "api_error"))


# ─── 输入 token 本地估算（/v1/messages/count_tokens） ─────────────────────────
#
# Claude Code 等客户端每次请求前会调 count_tokens 做上下文预算。官方口径是
# 图片 ≈ (宽×高)/750、CJK 单字 ≈ 1 token、英文 ≈ 4 字符/token；这里拿不到
# 图片尺寸，取常见截图的量级并整体宁高不低（客户端据此提前压缩上下文，
# 估高只会让它更早压缩，估低才会真的超限被上游拒绝）。

IMAGE_TOKEN_ESTIMATE = 1600       # 每张图片（含文档截图）的固定估算
MESSAGE_OVERHEAD_TOKENS = 4       # 每条消息的 role/分隔符开销（官方为 3 左右）
_WIDE_RANGES = (
    (0x1100, 0x11FF), (0x2E80, 0x303F), (0x3040, 0x30FF), (0x3130, 0x318F),
    (0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xA960, 0xA97F), (0xAC00, 0xD7AF),
    (0xF900, 0xFAFF), (0xFE30, 0xFE4F), (0xFF00, 0xFF60),
)   # 中日韩标点/假名/韩文/汉字：逐字计 1 token
# 预编译成码位删除表：逐字符跑 any(ord(ch) in 11 区间) 要 1.3s/MB，
# 而 count_tokens 的请求体是客户端可控大小，一条 8MB 文本就能占住核心十几秒。
# translate 一次 C 级扫描即可，删掉的字符数就是宽字符数。
_WIDE_DELETE_TABLE = {cp: None for lo, hi in _WIDE_RANGES for cp in range(lo, hi + 1)}


def _text_tokens(text: str) -> int:
    """一段文本的 token 估算：宽字符逐字计 1，其余按 4 字符计 1。"""
    if not text:
        return 0
    wide = len(text) - len(text.translate(_WIDE_DELETE_TABLE))
    return wide + (len(text) - wide + 3) // 4


def estimate_input_tokens(data: dict) -> int:
    """本地估算 Anthropic Messages 请求的输入 token 数（不打上游、不记账）。

    必须与 anthropic_to_chat 同源取字段（system / text / tool_use.input /
    tool_result / tools 定义），否则估算与真实载荷脱节：image 块绝不展开
    base64（一次请求的图片 data 能让估算虚高上万），thinking 历史块与
    anthropic_to_chat 一致地丢弃（本代理不把思考块转发给上游）。
    """
    pieces = []
    sys_text = _system_text(data.get("system"))
    if sys_text:
        pieces.append(sys_text)

    messages = data.get("messages") or []
    images = 0
    for m in messages:
        if not isinstance(m, dict):
            continue
        pieces.append(str(m.get("role") or "user"))
        content = m.get("content")
        if isinstance(content, str):
            pieces.append(content)
        elif isinstance(content, list):
            for p in content:
                if not isinstance(p, dict):
                    pieces.append(str(p))
                    continue
                ptype = p.get("type")
                if ptype == "text":
                    pieces.append(str(p.get("text") or ""))
                elif ptype == "image":
                    images += 1
                elif ptype == "tool_use":
                    pieces.append(str(p.get("name") or ""))
                    pieces.append(json.dumps(p.get("input") or {},
                                             ensure_ascii=False))
                elif ptype == "tool_result":
                    pieces.append(_tool_result_text(p.get("content")))
                elif ptype in ("thinking", "redacted_thinking"):
                    continue                 # 不随请求转发，不占上游预算
                elif isinstance(p.get("text"), str):
                    pieces.append(p["text"])  # 未知块类型：有文本就算文本

    for t in data.get("tools") or []:
        if not isinstance(t, dict):
            continue
        pieces.append(str(t.get("name") or ""))
        pieces.append(str(t.get("description") or ""))
        pieces.append(json.dumps(t.get("input_schema") or {},
                                 ensure_ascii=False))

    tokens = _text_tokens("".join(pieces))
    tokens += MESSAGE_OVERHEAD_TOKENS * len(messages) + IMAGE_TOKEN_ESTIMATE * images
    return max(1, tokens)


# ─── SSE 桥：OpenAI data-only 流 → Anthropic event/data 流 ────────────────────


class AnthropicSSEBridge:
    """把 chat/completions SSE 增量翻译成 Anthropic Messages 事件流。"""

    def __init__(self, requested_model: str):
        self.requested_model = requested_model
        self.message_id = f"msg_{uuid.uuid4().hex}"
        self.created = int(time.time())
        self.usage = {"input_tokens": 0, "output_tokens": 0}
        self.finish_reason = None
        self.next_block_index = 0
        self.text_block = None
        self.tool_blocks = {}
        self.block_order = []
        self.closed = False

    @staticmethod
    def ev(event: str, obj: dict) -> str:
        return f"event: {event}\ndata: {json.dumps(obj, ensure_ascii=False)}\n\n"

    def start(self) -> str:
        return self.ev("message_start", {
            "type": "message_start",
            "message": {
                "id": self.message_id,
                "type": "message",
                "role": "assistant",
                "model": self.requested_model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": dict(self.usage),
            },
        })

    @staticmethod
    def _unwrap(obj):
        if isinstance(obj, dict):
            data = obj.get("data")
            if isinstance(data, dict) and (data.get("choices") or data.get("id")
                                           or "usage" in data):
                return data
        return obj

    def _capture_usage(self, usage):
        if not isinstance(usage, dict):
            return
        self.usage = {
            "input_tokens": int(usage.get("prompt_tokens")
                                or usage.get("input_tokens") or 0),
            "output_tokens": int(usage.get("completion_tokens")
                                 or usage.get("output_tokens") or 0),
        }

    def _start_text(self) -> list:
        if self.text_block is not None:
            return []
        idx = self.next_block_index
        self.next_block_index += 1
        self.text_block = idx
        self.block_order.append(idx)
        return [self.ev("content_block_start", {
            "type": "content_block_start",
            "index": idx,
            "content_block": {"type": "text", "text": ""},
        })]

    def _tool_index(self, tc: dict) -> int:
        try:
            return int(tc.get("index", 0))
        except (TypeError, ValueError):
            return 0

    def _start_tool(self, tc: dict, key: int) -> tuple[list, dict]:
        idx = self.next_block_index
        self.next_block_index += 1
        fn = tc.get("function") or {}
        block = {
            "index": idx,
            "id": str(tc.get("id") or f"call_{key + 1}"),
            "name": str(fn.get("name") or ""),
        }
        self.tool_blocks[key] = block
        self.block_order.append(idx)
        events = [self.ev("content_block_start", {
            "type": "content_block_start",
            "index": idx,
            "content_block": {
                "type": "tool_use",
                "id": block["id"],
                "name": block["name"],
                "input": {},
            },
        })]
        return events, block

    def feed_payload(self, payload: str) -> list:
        """喂一条 OpenAI SSE data 内容，返回 0..N 个 Anthropic 事件。"""
        try:
            chunk = self._unwrap(json.loads(payload))
        except Exception:
            return []
        if not isinstance(chunk, dict):
            return []
        if isinstance(chunk.get("error"), dict):
            return [self.ev("error", {
                "type": "error",
                "error": {
                    "type": "api_error",
                    "message": str(chunk["error"].get("message") or "upstream error"),
                },
            })]
        self._capture_usage(chunk.get("usage"))
        events = []
        choice = ((chunk.get("choices") or [{}])[0] or {})
        delta = choice.get("delta") or {}
        text = delta.get("content")
        if text:
            events.extend(self._start_text())
            events.append(self.ev("content_block_delta", {
                "type": "content_block_delta",
                "index": self.text_block,
                "delta": {"type": "text_delta", "text": str(text)},
            }))
        for tc in delta.get("tool_calls") or []:
            if not isinstance(tc, dict):
                continue
            key = self._tool_index(tc)
            if key in self.tool_blocks:
                block = self.tool_blocks[key]
                if tc.get("id"):
                    block["id"] = str(tc["id"])
                fn0 = tc.get("function") or {}
                if isinstance(fn0, dict) and fn0.get("name"):
                    block["name"] = str(fn0["name"])
            else:
                start_events, block = self._start_tool(tc, key)
                events.extend(start_events)
            fn = tc.get("function") or {}
            args = fn.get("arguments") if isinstance(fn, dict) else None
            if args:
                if not isinstance(args, str):
                    args = json.dumps(args, ensure_ascii=False)
                events.append(self.ev("content_block_delta", {
                    "type": "content_block_delta",
                    "index": block["index"],
                    "delta": {"type": "input_json_delta", "partial_json": args},
                }))
        if choice.get("finish_reason"):
            self.finish_reason = choice["finish_reason"]
        return events

    def finish(self) -> list:
        """关闭所有 content block，并发送 message_delta/message_stop。"""
        if self.closed:
            return []
        self.closed = True
        events = []
        for idx in self.block_order:
            events.append(self.ev("content_block_stop", {
                "type": "content_block_stop", "index": idx,
            }))
        events.append(self.ev("message_delta", {
            "type": "message_delta",
            "delta": {
                "stop_reason": _stop_reason(self.finish_reason,
                                            bool(self.tool_blocks)),
                "stop_sequence": None,
            },
            "usage": {"output_tokens": self.usage["output_tokens"]},
        }))
        events.append(self.ev("message_stop", {"type": "message_stop"}))
        return events

