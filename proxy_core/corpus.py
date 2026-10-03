# -*- coding: utf-8 -*-
"""语料采集子系统（阶段 E / 方案 §7）：为训练自己的模型收集语料。

两条数据线（§7.1 correlation 贯穿 + §7.3 脱敏红线）：
  请求线程（routes/proxy.py 三协议入口）：
    捕获原始请求 correlation（protocol + 原始
    messages/instructions + tools 名称列表，协议
    转换之前），流式经 _StreamTap 累积 assistant
    输出流，成功收尾后组样本——请求线程只入队，
    绝不脱敏（§7.3 红线）。
  worker 线程（QueueWorker）：daemon +
    queue.SimpleQueue 无界（§7.4——不存在队列满
    场景，只做全异常吞掉 + 丢弃计数），逐条脱敏
    （proxy_core.redact）→ 追加
    corpus_local/local_YYYYMM.jsonl → flush+fsync
    （corpus_pull.py:56-60 落盘纪律）。

样本 schema（§7.2，顶层兼容云端 {"id","messages"}
读取器，其余为扩展 meta；本地 id local-日期-序号
与云端递增整数 id 按文件名隔离——local_YYYYMM
vs corpus_YYYYMM，corpus_pull.py:55 分名设计）。

config.json "corpus" 块：
  enabled（缺省 False——语料涉及落盘用户数据，
    默认关闭更稳妥，用户显式开启；与
    stream_gate/affinity 的缺省 True 不同）、
  sample_rate（0-1 采样率，缺省 1.0；月度文件
    超 500MB 时生效值封顶 0.3——T9 P2-5 降采样）、
  roles（角色白名单，空/缺省 = 不限）、
  include_thinking（思考过程是否入语料，缺省
    True）、max_body_tokens（单样本 token 上限，
    超出丢弃，缺省 64000；0 = 不限）、
  redact_extra（自定义脱敏规则：词/正则列表，
    parse_extra_rules 启发式分词/正则）。

min_assistant_chars 缺省先统计后定（§7.4 审计
E-6）：上线首周 sample_rate=1.0 全采只打标不过
滤，短样本过滤由训练构建 scripts/corpus_build.py
按 manifest 统计后回头定阈值——采集侧不实现该
过滤。
"""

import json
import logging
import os
import re
import queue
import random
import threading
import time
from datetime import datetime
from pathlib import Path

from . import redact
from .settings import CORPUS_DIR, load_config
from .stats import _cache_read_tokens
from .state import activity
from .water import water

log = logging.getLogger("qidi-relay")

# 默认配置（config.json "corpus" 块覆盖）
DEFAULT_SAMPLE_RATE = 1.0
DEFAULT_INCLUDE_THINKING = True
DEFAULT_MAX_BODY_TOKENS = 64000

# seedguard 告警态提示的最短间隔（防刷屏——§7.5
# "activity 提示一次"）
_ALERT_HINT_INTERVAL_S = 600.0
_alert_hint_lock = threading.Lock()
_last_alert_hint_ts = 0.0

# 月度文件降采样（T9 P2-5）：当月落盘文件超过
# 500MB 时，生效采样率封顶 0.3（防语料无限增长
# 刷爆磁盘），activity/log 限频提示（同 seedguard
# 告警态提示模式，防刷屏）。
_MAX_MONTH_BYTES = 500 * 1024 * 1024
_BIG_MONTH_SAMPLE_CAP = 0.3
_downsample_hint_lock = threading.Lock()
_last_downsample_hint_ts = 0.0


# ─── config 读取（每请求现读——与 _feature_stream_gate
# 同风格，改 config.json 不必重启即生效）────────────

def _corpus_raw() -> dict:
    cfg = load_config().get("corpus")
    return cfg if isinstance(cfg, dict) else {}


def corpus_enabled() -> bool:
    """语料采集总开关（config corpus.enabled，
    缺省 False——语料涉及落盘用户数据，默认关闭
    更稳妥；与 stream_gate/affinity 缺省 True
    不同，用户显式开启）。"""
    return bool(_corpus_raw().get("enabled", False))


def _corpus_cfg() -> dict:
    """解析 corpus 块为生效配置（防御式取值，
    坏型回落默认）。"""
    raw = _corpus_raw()
    rate = raw.get("sample_rate", DEFAULT_SAMPLE_RATE)
    try:
        rate = float(rate)
    except (TypeError, ValueError):
        rate = DEFAULT_SAMPLE_RATE
    rate = min(1.0, max(0.0, rate))
    # 生效采样率（T9 P2-5）：月度落盘文件超 500MB
    # 时封顶 0.3（降采样防刷盘）——maybe_collect 的
    # 采样决策用 sample_rate_effective 而非配置值。
    sample_rate_effective = rate
    if (rate > _BIG_MONTH_SAMPLE_CAP
            and _month_file_size() > _MAX_MONTH_BYTES):
        sample_rate_effective = _BIG_MONTH_SAMPLE_CAP
        _downsample_hint_once()
    roles = raw.get("roles")
    if isinstance(roles, list):
        roles = [r for r in roles if isinstance(r, str) and r]
        roles = roles or None
    else:
        roles = None
    incl = raw.get("include_thinking", DEFAULT_INCLUDE_THINKING)
    if not isinstance(incl, bool):
        incl = DEFAULT_INCLUDE_THINKING
    max_body = raw.get("max_body_tokens", DEFAULT_MAX_BODY_TOKENS)
    try:
        max_body = int(max_body)
    except (TypeError, ValueError):
        max_body = DEFAULT_MAX_BODY_TOKENS
    if max_body < 0:
        max_body = 0
    return {
        "enabled": bool(raw.get("enabled", False)),
        "sample_rate": rate,
        "sample_rate_effective": sample_rate_effective,
        "roles": roles,
        "include_thinking": incl,
        "max_body_tokens": max_body,
        "extra": redact.parse_extra_rules(raw.get("redact_extra")),
    }


# ─── correlation 捕获（§7.1，三协议入口调用）──────

# 媒体块白名单占位（§7.1 R6 项目2：递归 + 文本白名单）
# ——content 块列表只放行 {"type":"text"}（或 str）；
# 媒体块与任何含 base64 载体（source.type=="base64"
# 或 data: URI）的块 → {"type":"text","text":
# "[media:N,type]"} 占位。N = 全样本媒体块序号
#（跨 content 列表连续计数）。
_MEDIA_BLOCK_TYPES = ("image", "image_url",
                      "input_image", "document",
                      "file", "input_audio")
_DATA_URI_RE = re.compile(r"data:[^\s\"']+;base64,")
_MAX_MEDIA_DEPTH = 6   # 递归深度上限（防递归炸弹）


def _tool_names(data: dict) -> list:
    """tools 名称列表（OpenAI/Responses 形
    tools[*].function.name；Anthropic 形
    tools[*].name）。"""
    rt = data.get("tools")
    if not isinstance(rt, list):
        return []
    names = []
    for t in rt:
        if not isinstance(t, dict):
            continue
        fn = t.get("function")
        if (isinstance(fn, dict) and isinstance(fn.get("name"), str)
                and fn["name"]):
            names.append(fn["name"])
        elif isinstance(t.get("name"), str) and t["name"]:
            names.append(t["name"])
    return names


def _original_messages(protocol: str, data: dict) -> list:
    """按协议取原始消息（协议原生形态，不做翻译）。"""
    if protocol == "responses":
        # instructions→system 转换（_responses_to_chat）
        # 之前捕获：instructions 原样入 system 消息，
        # input 原样保留（str 或 message 项列表）
        msgs = []
        instr = data.get("instructions")
        if isinstance(instr, str) and instr.strip():
            msgs.append({"role": "system", "content": instr})
        inp = data.get("input")
        if isinstance(inp, str):
            if inp.strip():
                msgs.append({"role": "user", "content": inp})
        elif isinstance(inp, list):
            msgs.extend(it for it in inp if isinstance(it, dict))
        return msgs
    if protocol == "anthropic":
        # anthropic_to_chat 转换之前捕获：原始
        # messages + system（str 或 content block 列表）
        msgs = []
        sysm = data.get("system")
        if isinstance(sysm, str) and sysm.strip():
            msgs.append({"role": "system", "content": sysm})
        elif isinstance(sysm, list):
            parts = [str(p.get("text") or "")
                     for p in sysm
                     if isinstance(p, dict) and p.get("type") == "text"]
            text = "\n".join(x for x in parts if x)
            if text.strip():
                msgs.append({"role": "system", "content": text})
        m = data.get("messages")
        if isinstance(m, list):
            msgs.extend(x for x in m if isinstance(x, dict))
        return msgs
    # openai（/v1/chat/completions）：data 本身即原文
    m = data.get("messages")
    if isinstance(m, list):
        return [x for x in m if isinstance(x, dict)]
    return []


def _is_media_block(block) -> bool:
    """媒体块判定（§7.1 白名单的反面）：type 属
    媒体族，或含 base64 载体（source.type==
    "base64"，或任意字段藏 data: URI——整块
    序列化后扫 data:…;base64, 形态）。"""
    if not isinstance(block, dict):
        return False
    t = block.get("type")
    if isinstance(t, str) and t in _MEDIA_BLOCK_TYPES:
        return True
    src = block.get("source")
    if isinstance(src, dict) and src.get("type") == "base64":
        return True
    # data: URI 只扫本块直接字符串值（不
    # 递归嵌套——否则含 data: URI 文本的
    # 消息/内容列表整体被误判为媒体块）
    for v in block.values():
        if isinstance(v, str) and _DATA_URI_RE.search(v):
            return True
    return False


def _placeholder_media(node, depth=0, counter=None):
    """媒体块白名单占位（§7.1 R6 项目2：递归 +
    文本白名单）。

    content 块列表只放行 {"type":"text"} 与
    str；媒体块（覆盖 tool_result.content 等
    嵌套）→ {"type":"text","text":"[media:N,
    type]"} 占位；其他结构化块（tool_result/
    tool_use 等）保留骨架并递归其内容。
    depth 超 _MAX_MEDIA_DEPTH 原样返回（防
    递归炸弹）。counter = [全样本媒体块序号]
    ——跨 content 列表连续计数。"""
    if counter is None:
        counter = [0]
    if depth > _MAX_MEDIA_DEPTH:
        return node
    if isinstance(node, dict):
        if _is_media_block(node):
            counter[0] += 1
            mtype = node.get("type")
            if not isinstance(mtype, str) or not mtype:
                mtype = "base64"
            return {"type": "text",
                    "text": "[media:%d,%s]" % (
                        counter[0], mtype)}
        return {k: _placeholder_media(v, depth + 1,
                                     counter)
                for k, v in node.items()}
    if isinstance(node, list):
        return [_placeholder_media(it, depth + 1,
                                   counter)
                for it in node]
    return node


def capture_correlation(protocol: str, data: dict, *,
                        headers=None, key_label=None,
                        client=None) -> dict | None:
    """三协议入口捕获原始请求（§7.1 correlation 贯穿）。

    在协议转换之前调用（/v1/chat 的 data 本身即
    原文；/v1/responses 的 instructions+input 在
    _responses_to_chat 转换前；/v1/messages 的
    messages+system 在 anthropic_to_chat 转换前）。
    开关关闭 → None（零开销：不建 correlation、
    不遍历 messages）。

    返回 correlation：protocol / messages（协议原生
    形态，媒体块已换 [media:N,type] 占位）/
    tools（名称列表）/ session_id（affinity 会话
    键四级 fallback）/ client（来源 IP）。
    """
    if not corpus_enabled():
        return None
    session_id = None
    try:
        from .affinity import session_key as _skey
        session_id = _skey(data, headers, key_label, client)
    except Exception:
        session_id = None
    return {
        "protocol": protocol,
        "messages": _placeholder_media(
            _original_messages(protocol, data)),
        "tools": _tool_names(data),
        "session_id": session_id,
        "client": client,
    }


# ─── _StreamTap：流式 assistant 输出聚合器（§7.1）──

# 流式累积字符软顶（R6 项目7：防上游异常输出
# 打爆请求线程内存——超顶即停累积并置
# truncated，样本 meta 记 body_truncated: true）
_TAP_CHAR_CAP = 200_000


class _StreamTap:
    """流式 assistant 输出聚合器（学 cline.py:553-584
    _merge_tool_call_delta 模式但独立实现）。

    上游按 index 分片返回 tool_calls：首片通常带
    id/name，后续片只带 function.arguments 片段；
    index→槽位合并，arguments 片段按序拼接。
    累积带字符软顶（_TAP_CHAR_CAP——超顶停累积
    置 truncated）。纯内存累积，零 IO；仅在请求
    线程内使用。"""

    def __init__(self):
        self.content_parts = []
        self.thinking_parts = []
        self.tool_calls = {}   # index -> {"id","type","function":{...}}
        self.finish = None
        self._chars = 0
        self.truncated = False

    def _take(self, text: str) -> str:
        """取一段待累积文本（字符软顶）：返回应
        累积部分（可空串）；一旦触顶即置
        truncated，后续累积全停。"""
        if self.truncated:
            return ""
        room = _TAP_CHAR_CAP - self._chars
        if room <= 0:
            self.truncated = True
            return ""
        if len(text) <= room:
            self._chars += len(text)
            return text
        self._chars = _TAP_CHAR_CAP
        self.truncated = True
        return text[:room]

    def _accumulate(self, parts: list, text: str) -> None:
        """累积一段输出到 parts（字符软顶见
        _take）。"""
        chunk = self._take(text)
        if chunk:
            parts.append(chunk)

    def feed(self, obj: dict) -> None:
        """吃一个解析成功的 SSE data 帧（OpenAI 形）。"""
        choices = obj.get("choices")
        if not isinstance(choices, list):
            return
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta")
            if not isinstance(delta, dict):
                continue
            if delta.get("content"):
                self._accumulate(
                    self.content_parts, delta["content"])
            rc = delta.get("reasoning_content")
            if rc:
                self._accumulate(self.thinking_parts, rc)
            rs = delta.get("reasoning")
            if rs:
                self._accumulate(self.thinking_parts, rs)
            self._merge_tool_calls(delta.get("tool_calls"))
            fr = choice.get("finish_reason")
            if fr:
                self.finish = fr

    def _merge_tool_calls(self, delta_tools) -> None:
        """tool_calls 分片合并（cline.py:553-584 模式
        独立实现：index→槽位，首片带 id/name，
        后续片只带 function.arguments 片段）。"""
        if not isinstance(delta_tools, list):
            return
        for item in delta_tools:
            if not isinstance(item, dict):
                continue
            try:
                idx = int(item.get("index", len(self.tool_calls)))
            except (TypeError, ValueError):
                idx = len(self.tool_calls)
            slot = self.tool_calls.setdefault(idx, {
                "id": "",
                "type": "function",
                "function": {"name": "", "arguments": ""},
            })
            if item.get("id"):
                slot["id"] = str(item["id"])
            if item.get("type"):
                slot["type"] = str(item["type"])
            fn = item.get("function") or {}
            if not isinstance(fn, dict):
                continue
            if fn.get("name"):
                slot["function"]["name"] = str(fn["name"])
            if fn.get("arguments"):
                arg = self._take(str(fn["arguments"]))
                if arg:
                    slot["function"]["arguments"] += arg

    def assistant(self) -> dict:
        """聚合结果（§7.2 schema assistant 块）。"""
        return {
            "content": "".join(self.content_parts),
            "thinking": "".join(self.thinking_parts),
            "tool_calls": [self.tool_calls[i]
                           for i in sorted(self.tool_calls)],
            "finish": self.finish,
        }


# ─── 决策表（§7.5）+ 样本组装（§7.2）──────────────

def _role_of(model: str):
    """模型 → 角色别名（router.role_of_model 反查；
    一个模型可能对应多个角色，取第一个）。"""
    try:
        from .router import role_of_model
        return role_of_model(model)
    except Exception:
        return None


def _provider_of(model: str) -> str:
    """模型 → provider 名（外部通道名；主池模型
    返回 "pool"）。"""
    try:
        from . import providers as _pv
        prov = _pv.match(model)
        return (prov or {}).get("name") or "pool"
    except Exception:
        return "pool"


def _estimate_sample_tokens(messages, assistant, usage) -> int:
    """样本 token 量估算：usage.total_tokens 优先
    （上游已报数最准），缺省按 chars/2.5——messages
    部分复用 router._estimate_tokens 同公式（不另起
    公式），assistant 部分按同除数折算。"""
    total = (usage or {}).get("total_tokens")
    if isinstance(total, int) and total > 0:
        return total
    est = 0
    try:
        from .router import _estimate_tokens
        est += _estimate_tokens({"messages": messages or []})
    except Exception:
        pass
    a = assistant or {}
    achars = 0
    for v in (a.get("content"), a.get("thinking")):
        if isinstance(v, str):
            achars += len(v)
    for tc in a.get("tool_calls") or ():
        achars += len(str(tc))
    return est + int(achars / 2.5)


def _seedguard_alert() -> bool:
    """seedguard 告警态（§7.5 联动——签名方案疑被
    官方轮换，此期间上游返回不可信 → 暂停采集）。

    告警面勘察：seedguard.py:56-64 _state 结构
    （"alert": False 初值；run_check 在
    seedguard.py:226-229 置位；_save_state
    seedguard.py:77-84 持久化 seed_state.json 的
    alert/alert_detail/last_check_at 键）；只读快照
    出口 state()（seedguard.py:254-257——与
    /api/seed-alert 同源，routes/admin.py:947-956）。
    """
    try:
        from .seedguard import state as _sg_state
        return bool(_sg_state().get("alert"))
    except Exception:
        return False


def _alert_hint_once() -> None:
    """seedguard 告警态提示（activity 面板黄条）——
    同一告警期至多每 10 分钟一次（防刷屏）。"""
    global _last_alert_hint_ts
    now = time.time()
    with _alert_hint_lock:
        if now - _last_alert_hint_ts < _ALERT_HINT_INTERVAL_S:
            return
        _last_alert_hint_ts = now
    activity.add("warn", "语料采集暂停：seedguard 告警态"
                 "（签名方案疑被官方轮换，期间上游返回"
                 "不可信），恢复后自动续采",
                 status="corpus-pause")


def _month_file_size() -> int:
    """当月落盘文件实际大小（字节；目录/文件缺失或
    任何 OSError 容错 0——T9 P2-5 降采样判据）。"""
    try:
        return os.path.getsize(
            CORPUS_DIR / f"local_{datetime.now():%Y%m}.jsonl")
    except OSError:
        return 0


def _downsample_hint_once() -> None:
    """月度文件超限降采样提示（activity 面板黄条 +
    log）——同一限频窗口（_ALERT_HINT_INTERVAL_S）
    内至多一次（防刷屏，同 seedguard 告警态提示
    模式）。"""
    global _last_downsample_hint_ts
    now = time.time()
    with _downsample_hint_lock:
        if now - _last_downsample_hint_ts < _ALERT_HINT_INTERVAL_S:
            return
        _last_downsample_hint_ts = now
    activity.add("warn", "语料月度文件已超 500MB，"
                 "采样率临时封顶 0.3（降采样防刷盘）",
                 status="corpus-downsample")
    log.warning("[语料] 月度文件超 %.0fMB，采样率封顶 %.1f",
                _MAX_MONTH_BYTES / 1024 / 1024,
                _BIG_MONTH_SAMPLE_CAP)


def maybe_collect(correlation, assistant, *, uid: str,
                  model: str, model_served=None,
                  usage=None, ttft_content_ms=None,
                  total_ms: int = 0, stream: bool = False,
                  partial: bool = False,
                  body_truncated: bool = False):
    """请求线程采集入口（§7.2 样本组装 + §7.5 决策表）。

    只做决策 + 入队——脱敏绝不在请求线程（§7.3
    红线：全部在 QueueWorker 线程执行，
    meta.redact_hits 脱敏后写入）。

    决策表（§7.5）：
      partial（失败/截断）→ 不产样本；
      seedguard 告警态 → 暂停采集 + activity 提示
        一次（防刷屏）；
      sample_rate 采样 / roles 白名单 /
        max_body_tokens 超限 → 不产样本；
      water_suspect → 保留 + 打标；
      正常 → 保留。
    body_truncated：流式累积触字符软顶
    （_StreamTap.truncated，R6 项目7）→ 样本
    meta 记 body_truncated: true（内容可能
    不完整）。
    返回入队样本或 None。
    """
    if correlation is None or partial:
        return None
    cfg = _corpus_cfg()
    if not cfg["enabled"]:
        return None
    if _seedguard_alert():
        _alert_hint_once()
        return None
    if (cfg["sample_rate_effective"] < 1.0
            and random.random() > cfg["sample_rate_effective"]):
        return None
    role = _role_of(model)
    if cfg["roles"] is not None and role not in cfg["roles"]:
        return None
    est = _estimate_sample_tokens(
        correlation.get("messages"), assistant, usage)
    if cfg["max_body_tokens"] > 0 and est > cfg["max_body_tokens"]:
        worker.note_oversize()
        return None
    water_suspect = _water_suspect(uid, model)
    sample = {
        "id": "",   # worker 落盘时分配（§7.2 本地 id 规则）
        "session_id": correlation.get("session_id"),
        "ts": datetime.now().isoformat(timespec="seconds"),
        "client": correlation.get("client"),
        "role": role,
        "model": model,
        "model_served": model_served,
        "provider": _provider_of(model),
        "protocol": correlation.get("protocol"),
        "messages": correlation.get("messages") or [],
        "assistant": assistant,
        "tools": correlation.get("tools") or [],
        "usage": {
            "prompt": (usage or {}).get("prompt_tokens", 0),
            "completion": (usage or {}).get("completion_tokens", 0),
            "cache_read": _cache_read_tokens(usage),
            "total": (usage or {}).get("total_tokens", 0),
        },
        "ttft_content_ms": ttft_content_ms,
        "total_ms": total_ms,
        "stream": stream,
        "partial": partial,
        "body_truncated": body_truncated,
        "water_suspect": water_suspect,
        "redact_hits": 0,   # worker 脱敏后写入（§7.3）
    }
    worker.submit(sample)
    return sample


def _water_suspect(uid: str, model: str) -> bool:
    """该 (账号, 模型) 是否有被动嫌疑记录（§7.5
    water_suspect 判定——water 模块只读查询面
    has_passive_suspect）。"""
    try:
        return water.has_passive_suspect(uid, model)
    except Exception:
        return False


# arguments JSON 解析失败哨兵（区别于合法 JSON
# null——后者解析为 None 也是有效结构）
_NOT_JSON = object()


def _redact_tool_calls(tool_calls, extra) -> tuple:
    """tool_calls 结构化脱敏（§7.3 R6 项目6）。

    function.arguments 是 JSON 字符串——键名探测
    （is_sensitive_key）对字符串失效：先
    json.loads → 随整块走 redact_messages（递归，
    键名探测生效）→ 重新 dumps 回字符串；解析
    失败/非 JSON 结构 → 回落现有正则路径
    （redact_messages 原样处理字符串）。"""
    if not isinstance(tool_calls, list):
        return tool_calls, 0
    out = []
    hits = 0
    for tc in tool_calls:
        if not isinstance(tc, dict):
            nv, h = redact.redact_messages(tc, extra)
            out.append(nv)
            hits += h
            continue
        fn = tc.get("function")
        parsed = _NOT_JSON
        if (isinstance(fn, dict)
                and isinstance(fn.get("arguments"), str)
                and fn["arguments"]):
            try:
                parsed = json.loads(fn["arguments"])
            except (ValueError, TypeError):
                parsed = _NOT_JSON
        if isinstance(parsed, (dict, list)):
            # arguments 是 JSON 结构：解析后随整体
            # redact_messages（键名探测生效），再
            # dumps 回字符串
            tc2 = dict(tc)
            fn2 = dict(fn)
            fn2["arguments"] = parsed
            tc2["function"] = fn2
            nv, h = redact.redact_messages(tc2, extra)
            fnv = nv.get("function")
            if isinstance(fnv, dict):
                fnv = dict(fnv)
                fnv["arguments"] = json.dumps(
                    fnv.get("arguments"),
                    ensure_ascii=False)
                nv = dict(nv)
                nv["function"] = fnv
            out.append(nv)
            hits += h
        else:
            # 回落：现有正则路径（arguments 按文本
            # 规则处理）
            nv, h = redact.redact_messages(tc, extra)
            out.append(nv)
            hits += h
    return out, hits


# ─── QueueWorker：落盘 worker（§7.4）──────────────

class QueueWorker:
    """语料落盘 worker（§7.4）。

    daemon 线程 + queue.SimpleQueue 无界（§7.4 修正
    原"队列满"表述——不存在队列满场景，只做 worker
    全异常吞掉 + 丢弃计数）。逐条追加
    corpus_local/local_YYYYMM.jsonl，flush+fsync
    （corpus_pull.py:56-60 落盘纪律）。

    落盘目录读模块全局 CORPUS_DIR（settings 路径
    常量——测试 TMP 注入点，与 CONVERSATIONS_FILE
    同纪律）。
    """

    def __init__(self):
        self._q = queue.SimpleQueue()
        self._lock = threading.Lock()
        self._started = False
        self._stop = threading.Event()
        self._thread = None
        # 统计（线程安全：_lock 内）
        self._day = ""
        self._day_count = 0
        self._day_tokens = 0
        self._month = ""
        self._month_count = 0
        self._month_tokens = 0
        self._total_count = 0
        self._total_tokens = 0
        self._by_model: dict = {}
        self._by_role: dict = {}
        self._water_suspect = 0
        self._redact_hits = 0
        self._discarded = 0
        self._oversize = 0
        # 本地 id 序号（§7.2：local-YYYYMMDD-序号）
        self._seq_day = ""
        self._seq = 0

    def start(self) -> None:
        """启动 daemon worker 线程（幂等，防重复
        启动——seedguard.start_background 同款）。"""
        with self._lock:
            if self._started:
                return
            self._started = True
        t = threading.Thread(target=self._run,
                             name="corpus-worker",
                             daemon=True)
        self._thread = t
        t.start()
        log.info("[语料] worker 线程已启动（落盘目录 %s）",
                 CORPUS_DIR)

    def submit(self, sample: dict) -> None:
        """入队（请求线程调用——只入队，绝不在请求
        线程脱敏/落盘，§7.3 红线）。"""
        self._q.put(sample)

    def backlog(self) -> int:
        """队列积压（/api/corpus/stats 面板字段）。"""
        return self._q.qsize()

    def note_oversize(self) -> None:
        """max_body_tokens 超限丢弃计数（请求线程
        侧决策——样本未入队，单独计数）。"""
        with self._lock:
            self._oversize += 1

    def stop(self, join_timeout: float = 3.0) -> None:
        """请求 worker 线程退出（停机用）。

        置停止位 → 线程退出循环前排空剩余
        积压（停机不丢语料）；join 等待
        （超时返回——daemon 线程兜底，进程
        结束自动清）。
        """
        self._stop.set()
        if self._thread is not None:
            self._thread.join(join_timeout)

    def drain(self) -> int:
        """同步处理完队列积压（测试/停机用——正常
        服务由 worker 线程消费）。返回处理条数。"""
        n = 0
        while True:
            try:
                item = self._q.get_nowait()
            except queue.Empty:
                return n
            try:
                self._process(item)
            except Exception as e:
                log.error("[语料] 样本处理失败（丢弃）: %s", e)
                with self._lock:
                    self._discarded += 1
            n += 1

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                item = self._q.get(timeout=0.5)
            except queue.Empty:
                continue
            except Exception as e:
                # 队列本身异常（理论上 SimpleQueue
                # 不会）——吞掉继续，丢弃计数
                log.error("[语料] 队列读取异常（忽略）: %s", e)
                with self._lock:
                    self._discarded += 1
                continue
            try:
                self._process(item)
            except Exception as e:
                # worker 全异常吞掉 + 丢弃计数（§7.4）：
                # 落盘失败绝不影响代理主流程
                log.error("[语料] 样本处理失败（丢弃）: %s", e)
                with self._lock:
                    self._discarded += 1
        # 退出前排空剩余积压（停机不丢语料）
        while True:
            try:
                item = self._q.get_nowait()
            except queue.Empty:
                return
            try:
                self._process(item)
            except Exception as e:
                log.error("[语料] 样本处理失败（丢弃）: %s", e)
                with self._lock:
                    self._discarded += 1

    def _process(self, item) -> None:
        """处理一条样本：开关复检（§7.5 R6 项目8：
        入队后运营者关开关 → 丢弃+计数，排空
        backlog 不再落新盘）→ 分配本地 id →
        脱敏（§7.3 全部在本线程）→ 追加落盘
        （flush+fsync）→ 统计。"""
        cfg = _corpus_cfg()
        if not cfg["enabled"]:
            with self._lock:
                self._discarded += 1
            return
        sample = dict(item)
        now = datetime.now()
        day = now.strftime("%Y%m%d")
        month = now.strftime("%Y%m")
        # 本地 id：local-YYYYMMDD-序号（§7.2——与云端
        # 递增整数 id 按文件名隔离：local_YYYYMM.jsonl
        # vs corpus_YYYYMM.jsonl）
        with self._lock:
            if self._seq_day != day:
                self._seq_day = day
                self._seq = 0
            self._seq += 1
            sample["id"] = f"local-{day}-{self._seq:06d}"
        # ── 脱敏（§7.3 红线：全部在本 worker 线程）──
        extra = cfg["extra"]
        if not cfg["include_thinking"]:
            # 思考过程不入语料（先于脱敏——不脱敏
            # 不落盘的内容）
            asst0 = sample.get("assistant")
            if isinstance(asst0, dict):
                asst0 = dict(asst0)
                asst0["thinking"] = ""
                sample["assistant"] = asst0
        msgs, h_msgs = redact.redact_messages(
            sample.get("messages") or [], extra)
        sample["messages"] = msgs
        # meta 字段脱敏（R6 项目1）：client（来源
        # IP 明文）与 session_id（affinity 四级
        # fallback 可能嵌 IP/key 名——
        # affinity.py:103-143）各过一遍文本规则。
        # 命中不计入 redact_hits——该统计口径为
        # 内容脱敏命中（冻结语义）。
        client, _h_cl = redact.redact_text(
            sample.get("client"), extra)
        sample["client"] = client
        session_id, _h_sid = redact.redact_text(
            sample.get("session_id"), extra)
        sample["session_id"] = session_id
        asst = sample.get("assistant")
        if not isinstance(asst, dict):
            asst = {}
        content, h_c = redact.redact_text(
            asst.get("content") or "", extra)
        thinking, h_t = redact.redact_text(
            asst.get("thinking") or "", extra)
        tcs, h_tc = _redact_tool_calls(
            asst.get("tool_calls") or [], extra)
        sample["assistant"] = {
            "content": content,
            "thinking": thinking,
            "tool_calls": tcs,
            "finish": asst.get("finish"),
        }
        hits = h_msgs + h_c + h_t + h_tc
        sample["redact_hits"] = hits
        # ── 落盘：逐条 flush+fsync（corpus_pull.py:56-60
        # 落盘纪律）──
        path = self._file_path(month)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(sample, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        # ── 统计 ──
        usage = sample.get("usage")
        tokens = (usage or {}).get("total", 0)
        model = sample.get("model") or ""
        role = sample.get("role")
        with self._lock:
            if self._day != day:
                self._day = day
                self._day_count = 0
                self._day_tokens = 0
            if self._month != month:
                self._month = month
                self._month_count = 0
                self._month_tokens = 0
            self._day_count += 1
            self._day_tokens += tokens
            self._month_count += 1
            self._month_tokens += tokens
            self._total_count += 1
            self._total_tokens += tokens
            self._by_model[model] = self._by_model.get(model, 0) + 1
            if role:
                self._by_role[role] = self._by_role.get(role, 0) + 1
            if sample.get("water_suspect"):
                self._water_suspect += 1
            self._redact_hits += hits

    def _file_path(self, month: str) -> Path:
        """当月落盘文件（local_YYYYMM.jsonl）。

        目录取模块全局 CORPUS_DIR（调用时解析——
        测试 TMP 注入生效）。
        """
        d = CORPUS_DIR
        d.mkdir(parents=True, exist_ok=True)
        return d / f"local_{month}.jsonl"

    def stats(self) -> dict:
        """/api/corpus/stats 面板数据（§7.6）：今日/
        本月条数与 token 量、按 model/role 分布、
        water_suspect 数、redact_hits 总数、丢弃
        计数、队列积压、月度文件大小与生效采样率
        （T9 P2-5 降采样面板曝光）。"""
        cfg = _corpus_cfg()
        month_size = _month_file_size()
        with self._lock:
            return {
                "enabled": cfg["enabled"],
                "today": {"date": self._day,
                          "count": self._day_count,
                          "tokens": self._day_tokens},
                "month": {"month": self._month,
                          "count": self._month_count,
                          "tokens": self._month_tokens},
                "total": {"count": self._total_count,
                          "tokens": self._total_tokens},
                "by_model": dict(self._by_model),
                "by_role": dict(self._by_role),
                "water_suspect": self._water_suspect,
                "redact_hits": self._redact_hits,
                "discarded": self._discarded,
                "oversize": self._oversize,
                "backlog": self._q.qsize(),
                "month_size_bytes": month_size,
                "sample_rate_effective": cfg["sample_rate_effective"],
            }


# 模块单例（main.py 启动时 start_worker() 启动
# daemon 线程；未启动时样本滞留队列——drain()
# 可同步消费）
worker = QueueWorker()


def start_worker() -> QueueWorker:
    """启动语料落盘 worker（main.py 调用入口，
    幂等）。"""
    worker.start()
    return worker
