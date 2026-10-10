# -*- coding: utf-8 -*-
"""OpenCode serve 桥通道（T12）：把 OpenCode Go 订阅的 free 模型挂进中转。

背景（指挥官实证）：OpenCode Go 订阅的 free 模型**只能**经 `opencode serve`
桥（默认 127.0.0.1:4096）调用——key 直连 https://opencode.ai/zen/v1 返回
403（free）/ 402（订阅只吃 $0 余额）。本模块封装该桥的 session API：

  POST /session                        -> 新建会话 {"id": "ses_..."}
  POST /session/{sesID}/message        -> 阻塞返回 assistant message JSON：
                                          {info: {tokens:{input,output,total,
                                                           reasoning,cache},
                                                   finish, modelID, ...},
                                           parts: [{type:"reasoning",text},
                                                   {type:"text",text}, ...]}

协议约定（/doc OpenAPI 摘录，2026-10-10 拉取）：
- session.create body: {title?, model?:{id, providerID, variant?}, ...}
- session.prompt body: {model:{providerID, modelID}?, system?, parts:[...],
                        noReply?, agent?, tools?, format?, variant?}
  （parts 必填；TextPartInput = {type:"text", text, ...}）
- message 响应 200: {info: AssistantMessage, parts: Part[]}
  AssistantMessage.tokens = {input, output, reasoning, cache{read,write}, total?}
  Part = anyOf(TextPart, ReasoningPart, SubtaskPart, FilePart, ToolPart, ...)

**会话压缩（v1 核心设计）**：多轮 OpenAI messages（system/user/assistant/
tool）压缩成单条文本 prompt——system 消息置顶、历史按 role 标注、
最后一条 user 消息为落点。每请求新建 session 单次发送（v1 简化：
不复用 serve session）。

TODO（v2）：结合 affinity session_key 复用 serve session——同一客户端
会话的连续请求共享桥内 session，省掉每请求新建 session 的开销，
且让桥内多轮上下文天然保持（与压缩互补而非互斥）。

外部通道"可判负"语义：桥不可达（ConnectionError/超时）-> 抛
OpenCodeBridgeError。判负只耗 attempt 换号，**不触发任何冷却**
（与 cline 通道的账号冷却语义不同：桥是本机常驻服务，掉线是
"换个号/稍后重试"而非"该账号要冷却"）。
"""

import json
import logging

log = logging.getLogger("qidi-relay")

# 桥默认地址（config 缺省时使用）。路径常量走 settings 模式可注入：
# settings.py 定义 OPENCODE_SERVE_BASE 默认值，本模块直接引用
# （测试可通过 monkeypatch 重定向）。
from .settings import OPENCODE_SERVE_BASE as DEFAULT_SERVE_BASE

# config.json 的 providers.<name>.base_url 可显式覆盖桥地址；
# 缺省时取 DEFAULT_SERVE_BASE。bridge base 按 provider 维度缓存
# （providers._load_providers 的 mtime 重载会调用 reload_from_config）。
_bridge_base = {}   # provider name -> base_url

# POST /session/{id}/message 的阻塞超时（免费小模型实测 ~35s，
# 给足余量取 120s；桥卡死由该超时兜底，之后判负换号）。
MESSAGE_TIMEOUT_S = 120.0
CONNECT_TIMEOUT_S = 15.0


class OpenCodeBridgeError(RuntimeError):
    """桥不可达（连接失败/超时/非 200 且非可换号语义）。

    外部通道"可判负"异常：上游层捕获后记 503，只耗 attempt，
    不写任何冷却表。
    """


def reload_from_config(cfg: dict) -> None:
    """按 config providers 段重建 opencode-serve 桥地址表（_load_providers 调用）。"""
    global _bridge_base
    provs = (cfg.get("providers") or {})
    new = {}
    for name, p in provs.items():
        if isinstance(p, dict) and p.get("type") == "opencode-serve":
            base = (p.get("base_url") or "").rstrip("/") or DEFAULT_SERVE_BASE
            new[name] = base
    _bridge_base.clear()
    _bridge_base.update(new)


def base_url_for(name: str) -> str:
    """取某 provider 的桥 base_url（缓存缺失回落默认地址）。"""
    return _bridge_base.get(name) or DEFAULT_SERVE_BASE


# --- 会话压缩（v1 核心）-------------------------------------------

def _msg_text(content) -> str:
    """OpenAI message content（str / list[{type,text}] / None）-> 纯文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict) and p.get("type") in ("text", "input_text", "output_text"):
                parts.append(str(p.get("text", "")))
            elif isinstance(p, str):
                parts.append(p)
        return "\n".join(x for x in parts if x)
    return ""


def _tool_calls_text(m: dict) -> str:
    """assistant.tool_calls -> 文本行（压缩进单 prompt）。"""
    tcs = m.get("tool_calls")
    if not isinstance(tcs, list):
        return ""
    lines = []
    for tc in tcs:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or {}
        lines.append("[调用工具 %s] %s" % (fn.get("name", "?"), fn.get("arguments", "")))
    return "\n".join(lines)


def _history_lines(messages: list) -> list:
    """多轮历史（除最后一条 user 落点外）-> 按 role 标注的文本行。"""
    lines = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        role = str(m.get("role", "user"))
        text = _msg_text(m.get("content"))
        if role == "system":
            continue  # system 单独置顶
        if role == "assistant":
            tc = _tool_calls_text(m)
            body = "\n".join(x for x in [text, tc] if x)
            if not body:
                continue
            lines.append("[历史·助手] " + body)
        elif role == "tool":
            body = text or str(m.get("content", ""))
            lines.append(("[历史·工具结果] " + body).strip())
        else:  # user 及其它（function 等）按 user 归并
            if text:
                lines.append("[历史·用户] " + text)
    return lines


def _system_text(messages: list) -> str:
    """所有 system 消息合并置顶。"""
    parts = []
    for m in messages:
        if isinstance(m, dict) and m.get("role") == "system":
            t = _msg_text(m.get("content"))
            if t:
                parts.append(t)
    return "\n\n".join(parts)


def compress_messages(messages: list) -> str:
    """多轮 messages -> 单条文本 prompt（会话压缩 v1）。

    格式：
      [system 置顶段]
      历史段（[历史·用户/助手/工具结果] 逐条标注，末条 user 移出历史）
      [最后一条用户] <落点文本>

    系统消息置顶、历史按 role 标注、最后一条 user 为落点。
    """
    msgs = [m for m in (messages or []) if isinstance(m, dict)]
    sys_text = _system_text(msgs)
    # 最后一条 user 移出历史（作为落点）
    last_user = ""
    idx_last_user = -1
    for i, m in enumerate(msgs):
        if m.get("role") == "user" and _msg_text(m.get("content")).strip():
            last_user = _msg_text(m.get("content"))
            idx_last_user = i
    hist = _history_lines(msgs[:idx_last_user] if idx_last_user >= 0 else msgs)
    blocks = []
    if sys_text:
        blocks.append(sys_text)
    if hist:
        blocks.append("\n".join(hist))
    if last_user:
        blocks.append("[最后一条用户] " + last_user)
    return "\n\n".join(b for b in blocks if b)


# --- parts 解析 ---------------------------------------------------

def _parse_parts(parts: list) -> dict:
    """把桥返回的 parts 数组解析成 {content, reasoning}。

    type=="text" 拼接为 content；type=="reasoning" 拼为 reasoning；
    其它类型（step-start/step-finish/tool/file/subtask/...）忽略。
    """
    content = []
    reasoning = []
    for p in parts or []:
        if not isinstance(p, dict):
            continue
        t = p.get("type")
        txt = p.get("text")
        if txt is None:
            txt = ""
        if t == "text":
            content.append(str(txt))
        elif t == "reasoning":
            reasoning.append(str(txt))
        # 其它类型忽略
    return {"content": "".join(content), "reasoning": "".join(reasoning)}


def _map_finish(info_finish) -> str:
    """桥 info.finish -> OpenAI finish_reason。'stop'->'stop'，其余如实映射。"""
    f = str(info_finish or "")
    if f == "stop":
        return "stop"
    return f or "stop"


# --- 请求执行 -----------------------------------------------------

def send_chat(messages: list, model_id: str, timeout: float | None = None,
              provider_name: str = "") -> dict:
    """经 opencode serve 桥发送一次 chat（阻塞）。

    参数：
      messages        OpenAI 风格消息列表（system/user/assistant/tool）
      model_id        模型 ID（裸名，如 "ling-3.1-flash-free"）
      timeout         阻塞超时（缺省 MESSAGE_TIMEOUT_S）
      provider_name   config providers 段里该 opencode-serve 条目的名字
                      （用于取 base_url；缺省走桥默认地址）

    返回 dict:
      content / reasoning / usage:{input,output,total} /
      finish / model_served

    抛 OpenCodeBridgeError：桥连接失败/超时/非 200（可判负，不冷却）。
    """
    import httpx
    base = base_url_for(provider_name)
    t = timeout if timeout is not None else MESSAGE_TIMEOUT_S
    prompt = compress_messages(messages)
    hdrs = {"Content-Type": "application/json"}
    sys_text = _system_text(messages)

    # trust_env=False：桥是本机端点（127.0.0.1），绝不走系统/企业 HTTP 代理
    # （本机实测 HKCU Internet Settings ProxyEnable=1→7890；默认 trust_env=True
    # 会把 loopback 也塞进代理，桥掉线时表现为代理 502 而非干净的
    # ConnectionRefused，且平白多一跳）。
    try:
        with httpx.Client(trust_env=False, timeout=httpx.Timeout(
                connect=CONNECT_TIMEOUT_S, read=t, write=60,
                pool=CONNECT_TIMEOUT_S)) as client:
            # 1) 新建 session
            sess_r = client.post(base + "/session",
                                 json={"title": "qidi-relay-opencode",
                                       "model": {"id": model_id,
                                                 "providerID": "opencode"}},
                                 headers=hdrs)
            if sess_r.status_code != 200:
                raise OpenCodeBridgeError(
                    "opencode bridge /session 返回 %d: %s"
                    % (sess_r.status_code, sess_r.text[:200]))
            session_id = (sess_r.json() or {}).get("id")
            if not session_id:
                raise OpenCodeBridgeError(
                    "opencode bridge /session 未返回 session id")
            # 2) 单条 message 到该 session（阻塞等全文）
            msg_body = {
                "model": {"providerID": "opencode", "modelID": model_id},
                "parts": [{"type": "text", "text": prompt}],
            }
            if sys_text:
                msg_body["system"] = sys_text
            msg_r = client.post(
                "%s/session/%s/message" % (base, session_id),
                json=msg_body, headers=hdrs,
                timeout=httpx.Timeout(
                    connect=CONNECT_TIMEOUT_S, read=t, write=60,
                    pool=CONNECT_TIMEOUT_S))
            if msg_r.status_code != 200:
                raise OpenCodeBridgeError(
                    "opencode bridge /message 返回 %d: %s"
                    % (msg_r.status_code, msg_r.text[:200]))
    except httpx.HTTPError as e:
        # 连接失败/超时（含 ConnectionRefused）→ 可判负异常（不冷却）
        raise OpenCodeBridgeError("opencode bridge 不可达: %s" % e) from e
    doc = msg_r.json() or {}
    info = doc.get("info") or {}
    parsed = _parse_parts(doc.get("parts"))
    tokens = info.get("tokens") or {}
    usage = {
        "input": int(tokens.get("input") or 0),
        "output": int(tokens.get("output") or 0),
        "total": int(tokens.get("total") or 0),
    }
    if not usage["total"]:
        # 缺 total 时由 input+output 补（桥的 tokens.total 可能不回填）
        usage["total"] = usage["input"] + usage["output"]
    finish = _map_finish(info.get("finish"))
    model_served = str(info.get("modelID") or model_id)
    if info.get("error"):
        # 桥返回 200 但 info.error 非空（上游错误）-> 判负
        raise OpenCodeBridgeError(
            "opencode bridge info.error: %s"
            % json.dumps(info.get("error"), ensure_ascii=False)[:200])
    return {
        "content": parsed["content"],
        "reasoning": parsed["reasoning"],
        "usage": usage,
        "finish": finish,
        "model_served": model_served,
    }
