#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""workbuddy2api.py — WorkBuddy(腾讯 CodeBuddy 桌面端) OpenAI → Anthropic 协议适配器。

上游 : https://copilot.tencent.com/v1/messages  (Anthropic Messages 协议, 仅支持流式)
本地 : http://127.0.0.1:8768/v1                 (OpenAI 兼容)

凭据来源: CodeBuddyExtension 的明文 JSON 文件, 桌面端运行时会自动刷新该文件;
         本适配器每次上游请求都重新读取, 因此 token 跟随桌面端自动续期, 无需硬编码。

端点:
  GET  /v1/models            30 个对外模型 (OpenAI 格式, 附 wb_credits 倍率)
  GET  /healthz              健康检查
  POST /v1/chat/completions  OpenAI Chat Completions (流式 SSE / 非流式聚合)

依赖: aiohttp (仅此一个第三方库)
"""

import asyncio
import codecs
import json
import time
import uuid

from aiohttp import ClientSession, ClientTimeout, web

# ---------------------------------------------------------------------------
# 3.1 常量与配置
# ---------------------------------------------------------------------------
AUTH_FILE = r"C:\Users\?\AppData\Local\CodeBuddyExtension\Data\Public\auth\workbuddy-desktop.info"
UPSTREAM = "https://copilot.tencent.com"
LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 8768

MAX_TOKENS_DEFAULT = 4096
MAX_TOKENS_LIMIT = 64000
CONCURRENCY_LIMIT = 4
UPSTREAM_TIMEOUT = ClientTimeout(connect=30, total=300)   # 连接 30s / 总 300s

# ---------------------------------------------------------------------------
# 3.6 对外模型清单 (30 个) 及其 credits 倍率 (即计费倍率, 照抄规格第一节)
#     id 原样保留, 不加前缀 —— 中转侧会自行加 wb- 前缀映射。
# ---------------------------------------------------------------------------
MODEL_CREDITS = {
    "hy3": "x0.00",
    "hy4-preview": "x0.00",
    "hy3-x": "x0.05",
    "hy4-preview-x": "x0.29",
    "glm-5.3-flash": "x0.06",
    "deepseek-v4-flash": "x0.17",
    "minimax-m2.5": "x0.18",
    "minimax-m2.7": "x0.26",
    "minimax-m3": "x0.25",
    "fast-model": "x0.21",
    "balanced-model": "x0.65",
    "deep-model": "x1.20",
    "glm-5.3": "x0.79",
    "glm-5.2": "x0.79",
    "glm-5.1": "x0.79",
    "glm-5.0-turbo": "x0.95",
    "glm-4.6v": "x0.11",
    "glm-4.6": "x0.23",
    "glm-5v-turbo": "x0.71",
    "kimi-k3-1": "x1.62",
    "kimi-k2.7": "x0.57",
    "kimi-k2.6": "x0.52",
    "kimi-k2.5": "x0.45",
    "kimi-k2-thinking": "x0.54",
    "deepseek-v4-pro": "x0.51",
    "deepseek-v3-2-volc": "x0.29",
    "deepseek-v3-1-volc": "x0.52",
    "deepseek-v3-1-lkeap": "x0.52",
    "deepseek-v3-1": "x0.52",
    "deepseek-v3-0324-lkeap": "x0.52",
}
MODELS = list(MODEL_CREDITS.keys())

# 并发闸门: 上限 4, 满则 429 (见 3.5)
_semaphore = asyncio.Semaphore(CONCURRENCY_LIMIT)


# ---------------------------------------------------------------------------
# 3.2 Token 读取 (每次上游请求实时读文件, 不缓存)
# ---------------------------------------------------------------------------
def load_auth():
    """读取桌面端 auth 文件, 返回 (accessToken, uid)。失败抛异常。"""
    with open(AUTH_FILE, encoding="utf-8") as f:
        d = json.load(f)
    return d["auth"]["accessToken"], d["account"]["uid"]


# ---------------------------------------------------------------------------
# 3.4 content 数组提取
# ---------------------------------------------------------------------------
def extract_text(content):
    """把 OpenAI 的 content(可能是字符串或数组) 提取为纯文本。

    数组时取各块 .text 拼接; image_url 块忽略并计入占位(上游暂不支持图)。
    返回 (文本, 被忽略的图片块数)。
    """
    if isinstance(content, str):
        return content, 0
    text_parts = []
    images = 0
    if isinstance(content, list):
        for blk in content:
            if isinstance(blk, str):
                text_parts.append(blk)
                continue
            if not isinstance(blk, dict):
                continue
            if blk.get("type") in ("image_url", "image"):
                images += 1
                continue
            t = blk.get("text")
            if isinstance(t, str):
                text_parts.append(t)
    return "".join(text_parts), images


# ---------------------------------------------------------------------------
# 3.3.3 OpenAI → Anthropic 请求体转换
# ---------------------------------------------------------------------------
def convert_messages(messages):
    """system 消息拼接为顶层文本; user/assistant 转 messages 数组。"""
    system_parts = []
    converted = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        text, _images = extract_text(m.get("content"))
        if role == "system":
            if text:
                system_parts.append(text)
        elif role in ("user", "assistant"):
            converted.append({"role": role, "content": text})
    return "\n".join(system_parts), converted


def normalize_max_tokens(value):
    """缺省/非法 → 4096; 上限 64000。"""
    try:
        mt = int(value)
    except (TypeError, ValueError):
        return MAX_TOKENS_DEFAULT
    if mt <= 0:
        return MAX_TOKENS_DEFAULT
    return min(mt, MAX_TOKENS_LIMIT)


# ---------------------------------------------------------------------------
# 响应辅助
# ---------------------------------------------------------------------------
def json_error(message, status):
    """OpenAI 风格的错误响应体。"""
    return web.json_response({"error": {"message": message}}, status=status)


def upstream_error(status, raw):
    """把上游非 200 响应透传为 {"error":{"message":...}} 包装。"""
    msg = raw
    try:
        j = json.loads(raw)
        if isinstance(j, dict):
            err = j.get("error")
            if isinstance(err, dict) and err.get("message"):
                msg = err["message"]
            elif isinstance(err, str) and err:
                msg = err
            elif j.get("message"):
                msg = j["message"]
    except Exception:
        pass
    return json_error(msg, status)


def build_usage(usage):
    """Anthropic usage → OpenAI usage。"""
    prompt = int(usage.get("input_tokens") or 0)
    completion = int(usage.get("output_tokens") or 0)
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
    }


def extract_error_message(ev):
    """从上游 SSE 的 error 事件里抠出可读信息 (多层兜底, 不返回空串)。

    上游形态通常是 {"type":"error","error":{"type":...,"message":...}},
    也兼容 err 为字符串、message 直接挂顶层、以及整段兜底序列化。
    """
    if isinstance(ev, dict):
        err = ev.get("error")
        if isinstance(err, dict):
            for key in ("message", "type"):
                v = err.get(key)
                if isinstance(v, str) and v:
                    return v
        elif isinstance(err, str) and err:
            return err
        msg = ev.get("message")
        if isinstance(msg, str) and msg:
            return msg
    try:
        return json.dumps(ev, ensure_ascii=False)[:300]
    except Exception:
        return "upstream error"


def upstream_stream_error(message):
    """流式错误块与聚合 502 体共用的结构。"""
    return {"error": {"message": message, "type": "upstream_error"}}


class SseChunkDecoder:
    """跨 TCP chunk 安全的 UTF-8 增量解码 + 按 \n 切行。

    逐 chunk 直接 decode("utf-8", "replace") 会把跨 chunk 的中文多字节序列
    切成 U+FFFD 替换符; 增量解码器把不完整的尾巴留在内部, 等下一个 chunk 补齐。
    """

    def __init__(self):
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._buffer = ""

    def feed(self, raw):
        """喂入一个 chunk, 返回本次可完整切出的行 (不含 \n)。"""
        self._buffer += self._decoder.decode(raw)
        lines = []
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            lines.append(line)
        return lines

    def flush(self):
        """上游流结束: 补 final=True 排空解码器, 返回残留的最后一行。"""
        self._buffer += self._decoder.decode(b"", final=True)
        if not self._buffer:
            return []
        tail, self._buffer = self._buffer, ""
        return [tail]


async def iter_sse_events(resp):
    """逐行读取上游 SSE, 产出每个 data: 行的 JSON 对象。

    用 SseChunkDecoder 增量解码, 中文跨 chunk 不再被切成 U+FFFD。
    """
    decoder = SseChunkDecoder()
    async for raw in resp.content.iter_any():
        for line in decoder.feed(raw):
            obj = _parse_sse_line(line)
            if obj is not None:
                yield obj
    for line in decoder.flush():
        obj = _parse_sse_line(line)
        if obj is not None:
            yield obj


def _parse_sse_line(line):
    """解析单行 SSE; 非 data: 行或 JSON 解析失败返回 None。"""
    line = line.strip()
    if not line.startswith("data:"):
        return None
    payload = line[5:].strip()
    if not payload:
        return None
    try:
        obj = json.loads(payload)
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


def collect_event(ev, include_thinking, text_parts, thinking_parts, usage):
    """把单个上游事件归并进聚合缓冲; 返回事件类型。"""
    etype = ev.get("type")
    if etype == "message_start":
        msg = ev.get("message") or {}
        u = msg.get("usage") or {}
        if u:
            for k in ("input_tokens", "output_tokens"):
                if u.get(k) is not None:
                    usage[k] = u[k]
    elif etype == "content_block_delta":
        delta = ev.get("delta") or {}
        dtype = delta.get("type")
        if dtype == "text_delta":
            t = delta.get("text")
            if isinstance(t, str) and t:
                text_parts.append(t)
        elif dtype == "thinking_delta":
            if include_thinking:
                t = delta.get("thinking")
                if isinstance(t, str) and t:
                    thinking_parts.append(t)
    elif etype == "message_delta":
        u = ev.get("usage") or {}
        for k in ("input_tokens", "output_tokens"):
            if u.get(k) is not None:
                usage[k] = u[k]
    return etype


# ---------------------------------------------------------------------------
# 端点: GET /healthz
# ---------------------------------------------------------------------------
async def handle_healthz(request):
    try:
        _token, uid = load_auth()
    except Exception:
        return web.json_response(
            {"status": "ok", "token_configured": False, "uid_prefix": ""}
        )
    return web.json_response(
        {"status": "ok", "token_configured": True, "uid_prefix": uid[:8]}
    )


# ---------------------------------------------------------------------------
# 端点: GET /v1/models
# ---------------------------------------------------------------------------
async def handle_models(request):
    data = [
        {
            "id": model,
            "object": "model",
            "created": 0,
            "owned_by": "workbuddy",
            "wb_credits": MODEL_CREDITS[model],
        }
        for model in MODELS
    ]
    return web.json_response({"object": "list", "data": data})


# ---------------------------------------------------------------------------
# 端点: POST /v1/chat/completions
# ---------------------------------------------------------------------------
async def handle_chat(request):
    try:
        body = await request.json()
    except Exception:
        return json_error("invalid JSON body", 400)
    if not isinstance(body, dict):
        return json_error("request body must be a JSON object", 400)

    model = body.get("model")
    # 中转以 wb- 前缀路由到本适配器；剥前缀映射回上游原名（wb-hy3 -> hy3）
    display_model = model  # 响应回显请求原名（wb-hy3 进 wb-hy3 出，避免中转注水检测误报）
    if isinstance(model, str) and model.startswith("wb-") and model[3:] in MODEL_CREDITS:
        model = model[3:]
        body["model"] = model
    if model not in MODEL_CREDITS:
        return json_error(
            "unknown model: %r; available models: wb-* (via relay) or %s" % (model, ", ".join(MODELS)),
            400,
        )

    messages = body.get("messages")
    if not isinstance(messages, list):
        messages = []
    stream = bool(body.get("stream"))
    include_thinking = bool(body.get("include_thinking"))
    max_tokens = normalize_max_tokens(body.get("max_tokens"))

    system_text, anth_messages = convert_messages(messages)

    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "stream": True,          # 强制流式: 上游不支持 stream:false
        "messages": anth_messages,
    }
    if system_text:
        payload["system"] = system_text

    # 3.5 并发闸门: 满则 429
    if _semaphore.locked():
        return json_error(
            "too many concurrent requests (max %d)" % CONCURRENCY_LIMIT, 429
        )
    await _semaphore.acquire()
    try:
        return await _forward(request, payload, model, stream, include_thinking, display_model)
    finally:
        _semaphore.release()


async def _forward(request, payload, model, stream, include_thinking, display_model=None):
    """发往上游并翻译响应。"""
    # 3.2 每次上游请求实时读 token
    try:
        token, uid = load_auth()
    except Exception:
        return json_error(
            "无法读取 WorkBuddy 凭据文件, 请登录 WorkBuddy 桌面端。(%s)" % AUTH_FILE,
            502,
        )

    headers = {
        "Authorization": "Bearer " + token,
        "X-User-Id": uid,
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }

    session = ClientSession(timeout=UPSTREAM_TIMEOUT)
    resp = None
    handed_off = False   # True = resp/session 已交给下游两路各自收尾
    try:
        try:
            resp = await session.post(
                UPSTREAM + "/v1/messages", json=payload, headers=headers
            )
        except Exception as e:
            return json_error("upstream request failed: %s" % e, 502)

        ctype = resp.headers.get("Content-Type", "")

        # 3.3.7 上游错误
        if resp.status != 200:
            raw = await resp.text()   # 读体本身也可能抛, 由 finally 兜底关 session
            status = resp.status
            return upstream_error(status, raw)

        # 200 但返回的不是 SSE: 可能是 {"type":"error",...} 体
        if "text/event-stream" not in ctype:
            raw = await resp.text()   # 同上, 抛异常时不泄漏 session
            try:
                j = json.loads(raw)
            except Exception:
                j = None
            if isinstance(j, dict) and j.get("type") == "error":
                return upstream_error(502, raw)
            return upstream_error(502, raw)

        echo = display_model if display_model else model
        handed_off = True
        try:
            if stream:
                return await _stream_response(
                    request, resp, session, echo, include_thinking
                )
            return await _aggregate_response(resp, session, echo, include_thinking)
        except BaseException:
            # 下游还没进入自己的 finally 就炸了(如 prepare 失败): 收回所有权交给 finally
            handed_off = False
            raise
    finally:
        if not handed_off:
            if resp is not None:
                resp.close()
            await session.close()


async def _stream_response(request, resp, session, model, include_thinking):
    """上游 Anthropic SSE → OpenAI SSE。"""
    chat_id = "chatcmpl-" + uuid.uuid4().hex[:16]
    created = int(time.time())

    out = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream; charset=utf-8",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
    await out.prepare(request)

    def chunk(delta, finish_reason=None, usage=None):
        obj = {
            "id": chat_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [
                {"index": 0, "delta": delta, "finish_reason": finish_reason}
            ],
        }
        if usage is not None:
            obj["usage"] = usage
        return ("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode("utf-8")

    usage = {}
    role_sent = False
    upstream_failed = False
    try:
        async for ev in iter_sse_events(resp):
            etype = ev.get("type")
            if etype == "message_start":
                u = (ev.get("message") or {}).get("usage") or {}
                for k in ("input_tokens", "output_tokens"):
                    if u.get(k) is not None:
                        usage[k] = u[k]
                if not role_sent:
                    await out.write(chunk({"role": "assistant"}))
                    role_sent = True
            elif etype == "content_block_delta":
                delta = ev.get("delta") or {}
                dtype = delta.get("type")
                if dtype == "text_delta":
                    t = delta.get("text")
                    if isinstance(t, str) and t:
                        if not role_sent:
                            await out.write(chunk({"role": "assistant"}))
                            role_sent = True
                        await out.write(chunk({"content": t}))
                elif dtype == "thinking_delta":
                    if include_thinking:
                        t = delta.get("thinking")
                        if isinstance(t, str) and t:
                            if not role_sent:
                                await out.write(chunk({"role": "assistant"}))
                                role_sent = True
                            await out.write(chunk({"reasoning_content": t}))
            elif etype == "message_delta":
                u = ev.get("usage") or {}
                for k in ("input_tokens", "output_tokens"):
                    if u.get(k) is not None:
                        usage[k] = u[k]
            elif etype == "message_stop":
                break
            elif etype == "error":
                # 上游中途报错: 如实告知客户端, 不伪装成 finish_reason="stop" 的正常收尾
                err_chunk = upstream_stream_error(extract_error_message(ev))
                await out.write(
                    ("data: " + json.dumps(err_chunk, ensure_ascii=False) + "\n\n")
                    .encode("utf-8")
                )
                upstream_failed = True
                break

        if not upstream_failed:
            if not role_sent:
                await out.write(chunk({"role": "assistant"}))
                role_sent = True
            await out.write(chunk({}, finish_reason="stop", usage=build_usage(usage)))
            await out.write(b"data: [DONE]\n\n")
        await out.write_eof()
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        resp.close()
        await session.close()
    return out


async def _aggregate_response(resp, session, model, include_thinking):
    """内部仍走流式, 聚合 text_delta 成完整 OpenAI completion。"""
    chat_id = "chatcmpl-" + uuid.uuid4().hex[:16]
    text_parts = []
    thinking_parts = []
    usage = {}
    error_msg = None
    try:
        async for ev in iter_sse_events(resp):
            etype = collect_event(ev, include_thinking, text_parts, thinking_parts, usage)
            if etype == "message_stop":
                break
            if etype == "error":
                # 上游中途报错: 不能把半截文本当正常完成返回
                error_msg = extract_error_message(ev)
                break
    finally:
        resp.close()
        await session.close()

    if error_msg is not None:
        return web.json_response(upstream_stream_error(error_msg), status=502)

    message = {"role": "assistant", "content": "".join(text_parts)}
    if include_thinking and thinking_parts:
        message["reasoning_content"] = "".join(thinking_parts)
    result = {
        "id": chat_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {"index": 0, "message": message, "finish_reason": "stop"}
        ],
        "usage": build_usage(usage),
    }
    return web.json_response(result)


# ---------------------------------------------------------------------------
# 应用装配
# ---------------------------------------------------------------------------
def make_app():
    # aiohttp's default client_max_size is 1MB; large agent contexts (1000+
    # message sessions) exceed it and surface as "invalid JSON body" 400s.
    # 256MB gives generous headroom (body is fully buffered in memory).
    app = web.Application(client_max_size=256 * 1024 * 1024)
    app.router.add_get("/healthz", handle_healthz)
    app.router.add_get("/v1/models", handle_models)
    app.router.add_post("/v1/chat/completions", handle_chat)
    return app


if __name__ == "__main__":
    print("workbuddy2api listening on http://%s:%d/v1" % (LISTEN_HOST, LISTEN_PORT))
    web.run_app(make_app(), host=LISTEN_HOST, port=LISTEN_PORT, print=None)
