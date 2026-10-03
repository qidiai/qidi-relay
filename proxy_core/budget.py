"""客户端预算限额（阶段 C / 方案 §5）：按 API key 的 day 窗限额。

设计要点（方案 §5 审计裁决）：
- v1 只实现 period=day（day 窗 = 本地日历日，重置点 = 当日午夜）；
- est 复用 router._estimate_tokens（chars/2.5），不另起公式；
- cost 折算用 costing.cost_of 按 pricing 现价折算 est；未定价模型
  cost 贡献 0（对齐 magpie "无价不加"）；
- used 从 stats 的按 key 日记录（token_stats.json "__keys__"）重放——
  stats 已按请求记账，本模块零新增写路径（落盘零开销）；
- inflight 只在进程内（v1 单实例强制语义，§5 C-3）；
- release 幂等（同一次 reserve 的 release 调多次只减一次，
  学 magpie sync.Once）。

口径披露（R5）：
- cost 折算为下界：est 全部按输入价折算（输入价通常
  低于输出价），且请求时尚无输出——实际成本不低于此
  值；按单一输入口径折算会稀释真实成本，故 cost 限额
  作粗粒度兜底，tokens 限额为主判据；
- 跨午夜在途请求窗口归属（记完成窗）：请求跨午夜完成
  时，实际 usage 由 stats 记入完成日；在途份额随
  release 消失、不重复计，used 永远按本地当前日历日
  重放——day 窗边界处的在途请求只影响其完成所在窗。

本模块不依赖 flask——429 响应体由路由层按端点协议组装。
"""

import json
import logging
import threading
from datetime import datetime, timedelta

from .state import token_stats
from .costing import cost_of, get_pricing
from .router import _estimate_tokens  # est 复用此函数（规格 §5.2：禁另起公式）
from .settings import get_api_keys

log = logging.getLogger("qidi-relay")

# 产生 usage 的端点（预算只在这些端点上预留；
# /v1/models、/v1/messages/count_tokens、/codely/models 等
# 不产生用量，豁免——路由层按 request.path 判，列表即常量）。
USAGE_PATHS = ("/v1/chat/completions", "/v1/messages",
               "/v1/responses", "/v1/embeddings")

_lock = threading.Lock()
# 在途注册表：key_id（客户端原始 key）->
# {"n": 在途请求数, "tokens": 在途估 token 数,
#  "cost": 在途估成本, "label": key 标签}
# （label 冗余存放：管理面板按 label 聚合 inflight）
_inflight = {}


def _next_midnight() -> datetime:
    """本地时区下一个午夜（day 窗重置点）。"""
    now = datetime.now()
    return (now + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0)


def _noop():
    """无预算配置时的空释放（签名一致，调用方无需判空）。"""


def _blocks_chars(value) -> int:
    """内容（字符串或块列表）的字符总量。

    遍历口径与 router._estimate_tokens 一致：str 块
    直接计长，dict 块计其 "text" 字段长——不另起
    公式（规格 §5.2）。
    """
    if isinstance(value, str):
        return len(value)
    total = 0
    if isinstance(value, list):
        for block in value:
            if isinstance(block, str):
                total += len(block)
            elif isinstance(block, dict):
                total += len(str(block.get("text", "")))
    return total


def _messages_chars(messages) -> int:
    """messages 数组的字符总量（含 tool_calls，
    与 router._estimate_tokens 的遍历逐项对齐）。"""
    if not isinstance(messages, list):
        return 0
    total = 0
    for m in messages:
        if not isinstance(m, dict):
            continue
        total += _blocks_chars(m.get("content"))
        total += len(str(m.get("tool_calls", "")))
    return total


def estimate_tokens_for(path: str, data: dict) -> int:
    """按端点归一 est 源（R2）：统一 chars/2.5
    （复用 router._estimate_tokens 口径，不另起公式）。

    - /v1/chat/completions：messages（原口径，
      _estimate_tokens 直接覆盖）；
    - /v1/responses、/v1/embeddings：input（请求体
      是 input 而非 messages——原口径对这两端点
      est 恒 0）；
    - /v1/messages：messages + 顶层 system（字符串
      或块列表）+ tools（JSON 长度）——原口径漏计
      system/tools，est 偏低；
    - 其余端点：0（不产生 usage，预算不预留）。
    """
    if not isinstance(data, dict):
        return 0
    if path == "/v1/chat/completions":
        return _estimate_tokens(data)
    if path in ("/v1/responses", "/v1/embeddings"):
        return int(_blocks_chars(data.get("input")) / 2.5)
    if path == "/v1/messages":
        chars = _messages_chars(data.get("messages"))
        chars += _blocks_chars(data.get("system"))
        tools = data.get("tools")
        if tools:
            try:
                chars += len(json.dumps(tools, ensure_ascii=False))
            except (TypeError, ValueError):
                pass
        return int(chars / 2.5)
    return 0


def _used_for(label: str, budget: dict) -> tuple:
    """按 key 标签重放当日用量 -> (tokens, cost)。

    从 stats 的按 key 日记录（token_stats.json "__keys__"，
    stats.py:19/32）重放：TokenStats.get_key_stats() 已按
    本地日历日 rollover（stats.py:61-67，旧日期条目清零，
    即 day 窗重置点 = 当日午夜）。cache_reads=false 时
    缓存读 token 不计入（record_key 单独累计 cache_tokens，
    stats.py:72-96）。

    cost 折算口径（T9 P1-2 拆输入输出）：新条目按
    stats 拆记的 prompt/completion 折算（costing.
    cost_of 四参全用——输出 token 按输出价，不再
    按单一输入口径折算稀释真实成本，双审实测旧口径
    低估 1.6–4.5×）；per-model 用量只有总量——按
    模型 token 占当日总量比例分摊拆分。旧条目（无
    拆分字段，get_key_stats 回落 p=total, c=0）
    保持既有输入价口径。per-model 用量不含 cache
    拆分，故 cost 不做 cache 剔除。cost 限额仍作
    粗粒度兜底（tokens 限额为主判据）。

    类型防御（R5）：重放异常不得逃逸临界区——
    reserve 的乐观登记（n/tokens/cost）在锁内、
    重放之前已入账，异常若逃逸会让已登记的 n 泄漏
    （无 release handle 可撤）。异常按 used=0 +
    log.error 处理：fail-open（把已用满的请求拒掉
    是更坏的故障），记账自愈（stats 侧数据不动，
    下一次重放恢复正常）。
    """
    try:
        return _replay_used(label, budget)
    except Exception as e:
        log.error("预算用量重放失败（key=%s），按 0 用量 fail-open: %s",
                  label, e)
        return 0, 0.0


def _replay_used(label: str, budget: dict) -> tuple:
    """重放主体（_used_for 的 try 内调用）。"""
    u_tokens = 0
    u_cost = 0.0
    pricing = get_pricing()
    for e in token_stats.get_key_stats():
        if e.get("label") != label:
            continue
        # 拆输入输出口径（T9 P1-2）：cost_of 四参全用
        # ——按 stats 拆记的 prompt/completion 折算。
        # models 字典只有 per-model 总量——按模型 token
        # 占当日总量比例分摊拆分。旧条目回落
        # （get_key_stats 补 p=total, c=0）时分摊结果
        # 恰为 p=t, c=0，与升级前逐字节一致。
        e_total = int(e.get("total_tokens", 0) or 0)
        e_prompt = int(e.get("prompt_tokens", 0) or 0)
        e_completion = int(e.get("completion_tokens", 0) or 0)
        for model, t in (e.get("models") or {}).items():
            t = int(t or 0)
            if e_total > 0:
                share = t / e_total
                u_cost += cost_of(
                    pricing, model,
                    int(e_prompt * share),
                    int(e_completion * share))
            else:
                u_cost += cost_of(pricing, model, t, 0)
        u_tokens = int(e.get("total_tokens", 0) or 0)
        if not budget.get("cache_reads", False):
            u_tokens = max(0, u_tokens - int(e.get("cache_tokens", 0) or 0))
        break
    return u_tokens, u_cost


def used(key_id: str) -> dict:
    """该 key 当日已用量（从 stats 按 key 日记录重放）-> {"tokens", "cost"}。"""
    meta = get_api_keys().get(key_id)
    if not isinstance(meta, dict):
        return {"tokens": 0, "cost": 0.0}
    budget = meta.get("budget")
    if not isinstance(budget, dict):
        return {"tokens": 0, "cost": 0.0}
    u_tokens, u_cost = _used_for(meta["label"], budget)
    return {"tokens": u_tokens, "cost": round(u_cost, 6)}


def reserve(key_id: str, est_tokens: int, model: str | None = None):
    """为一次请求预留预算额度（方案 §5.2）。

    返回 (release, refusal)：
    - refusal 非 None = 判负（used + 在途 >= 限额），调用方应
      拒绝请求；此时本次乐观登记已回滚，在途计数不变。
    - release = 释放函数（幂等：同一次 reserve 的 release 调
      多次只减一次），请求完成/失败/客户端断开后必须调用。

    判负条件（满足任一即判负）：
    - used_tokens + 在途 tokens >= limit.tokens
    - used_cost + 在途 cost >= limit.cost

    est_tokens 由调用方复用 router._estimate_tokens 估计
    （首次 est 缺历史时仍按此值）；model 用于 cost 折算
    （costing.cost_of 以 pricing 现价折算 est；无价 → 0）。
    """
    meta = get_api_keys().get(key_id)
    if not isinstance(meta, dict):
        return _noop, None
    budget = meta.get("budget")
    if not isinstance(budget, dict):
        return _noop, None
    limit_tokens = budget.get("tokens")
    limit_cost = budget.get("cost")
    if limit_tokens is None and limit_cost is None:
        return _noop, None
    est_tokens = max(0, int(est_tokens or 0))
    est_cost = 0.0
    if limit_cost is not None and est_tokens:
        try:
            # 折算口径：est 全部视作输入 token 按现价折算
            # （请求尚无输出；输出部分以实际 usage 记账后
            # 由 used() 重放自然计入）。
            est_cost = cost_of(get_pricing(), model or "", est_tokens, 0)
        except Exception:
            est_cost = 0.0
    with _lock:
        e = _inflight.get(key_id)
        if e is None:
            e = {"n": 0, "tokens": 0, "cost": 0.0, "label": meta["label"]}
            _inflight[key_id] = e
        # 乐观登记：先计入在途，再判负（并发预留的正确顺序——
        # 判负检查必须看到本请求的在途份额）
        e["n"] += 1
        e["tokens"] += est_tokens
        e["cost"] += est_cost
        u_tokens, u_cost = _used_for(meta["label"], budget)
        reason = None
        if limit_tokens is not None and u_tokens + e["tokens"] >= limit_tokens:
            reason = "tokens"
        elif limit_cost is not None and u_cost + e["cost"] >= limit_cost:
            reason = "cost"
        if reason is not None:
            # 判负：回滚本次乐观登记，在途计数恢复原状
            e["n"] -= 1
            e["tokens"] -= est_tokens
            e["cost"] -= est_cost
            if e["n"] <= 0:
                _inflight.pop(key_id, None)
            return _noop, _refusal(meta, budget, reason, u_tokens, u_cost)
        handle = {"done": False}

        def release():
            """幂等释放（magpie sync.Once 同款：多次调用只减一次）。"""
            with _lock:
                if handle["done"]:
                    return
                handle["done"] = True
                cur = _inflight.get(key_id)
                if cur is None:
                    return
                cur["n"] -= 1
                cur["tokens"] -= est_tokens
                cur["cost"] -= est_cost
                if cur["n"] <= 0:
                    _inflight.pop(key_id, None)

        return release, None


def _refusal(meta: dict, budget: dict, reason: str,
             used_tokens: int, used_cost: float) -> dict:
    """判负信息（由路由层按端点协议翻译成 429 响应体）。"""
    reset_at = _next_midnight()
    retry_after = max(1, int((reset_at - datetime.now()).total_seconds()))
    return {
        "label": meta["label"],
        "reason": reason,
        "used_tokens": used_tokens,
        "limit_tokens": budget.get("tokens"),
        "used_cost": round(used_cost, 6),
        "limit_cost": budget.get("cost"),
        "reset_at": reset_at.strftime("%Y-%m-%d %H:%M:%S"),
        "retry_after": retry_after,
    }


def snapshot(label: str, budget_cfg: dict) -> dict:
    """管理面板预算快照（方案 §5.4）：限额的各维度全量曝光。

    tokens_left/cost_left = 限额 - 已用 - 在途（与新请求
    的判负条件一致——在途份额占用额度，含 cost 维度：
    inflight_cost 暴露在途估成本，cost_left 同步扣减）。
    """
    used_tokens, used_cost = _used_for(label, budget_cfg)
    inflight = inflight_tokens = 0
    inflight_cost = 0.0
    with _lock:
        for e in _inflight.values():
            if e["label"] == label:
                inflight += e["n"]
                inflight_tokens += e["tokens"]
                inflight_cost += e["cost"]
    limit_tokens = budget_cfg.get("tokens")
    limit_cost = budget_cfg.get("cost")
    return {
        "limit_tokens": limit_tokens,
        "used_tokens": used_tokens,
        "tokens_left": (limit_tokens - used_tokens - inflight_tokens
                        if limit_tokens is not None else None),
        "limit_cost": limit_cost,
        "used_cost": round(used_cost, 6),
        "cost_left": (round(limit_cost - used_cost - inflight_cost, 6)
                      if limit_cost is not None else None),
        "reset_at": _next_midnight().strftime("%Y-%m-%d %H:%M:%S"),
        "inflight": inflight,
        "inflight_tokens": inflight_tokens,
        "inflight_cost": round(inflight_cost, 6),
    }
