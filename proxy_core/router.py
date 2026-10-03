"""智能路由核心：进行中请求注册表 + 负载感知选号 + 经济路由。

解决的实际痛点：
1. 流式请求一跑几分钟，界面上看不出"卡死"还是"在慢慢吐字"
   → RequestRegistry 实时记录每条进行中请求（模型/账号/已耗时/已收字节），
     admin 的 /api/inflight 暴露给前端"进行中请求"面板。
2. 轮询选号不知道账号当前是否已在扛长请求
   → load_of(uid) 给 get_next 做"最少负载优先"参考。
3. 大上下文请求全部压给 GLM-5.3，又贵又慢
   → economic_reroute：小请求可按规则改走轻模型（默认关闭，config.json 开启）。
"""

import logging
import threading
import time
import uuid

from .settings import load_config

log = logging.getLogger("qidi-relay")


class RequestRegistry:
    """线程安全的进行中请求注册表。

    流式生成器开始时 register()，每个 chunk 到达时 touch()，
    结束/异常时 finish()。inflight() 给管理面板用。
    """

    def __init__(self, capacity: int = 500):
        self._lock = threading.Lock()
        self._reqs = {}  # rid -> dict
        self._capacity = capacity

    def register(self, model: str, user_id: str, stream: bool = False) -> str:
        rid = uuid.uuid4().hex[:12]
        with self._lock:
            if len(self._reqs) >= self._capacity:  # 防御：极端情况下丢最老的
                oldest = next(iter(self._reqs))
                self._reqs.pop(oldest, None)
            self._reqs[rid] = {
                "rid": rid,
                "model": model,
                "user_id": user_id,
                "stream": stream,
                "started": time.time(),
                "last_chunk": time.time(),
                "bytes": 0,
            }
        return rid

    def touch(self, rid: str, nbytes: int = 0):
        with self._lock:
            r = self._reqs.get(rid)
            if r:
                r["last_chunk"] = time.time()
                r["bytes"] += nbytes

    def finish(self, rid: str, status: str = "done"):
        with self._lock:
            r = self._reqs.pop(rid, None)
        if r:
            r["ended"] = time.time()
            r["status"] = status
            return r
        return None

    def inflight(self) -> list:
        """进行中请求快照（带已耗时/静默秒数，按耗时降序）。"""
        now = time.time()
        with self._lock:
            out = []
            for r in self._reqs.values():
                out.append(dict(r, elapsed=round(now - r["started"], 1),
                                idle=round(now - r["last_chunk"], 1)))
            return sorted(out, key=lambda x: -x["elapsed"])

    def load_of(self, user_id: str) -> int:
        """某账号（或 provider 名）当前进行中的请求数。"""
        with self._lock:
            return sum(1 for r in self._reqs.values() if r["user_id"] == user_id)


registry = RequestRegistry()


# ─── 经济路由 ────────────────────────────────────────────────────────────────

def _estimate_tokens(data: dict) -> int:
    """粗估请求上下文 token 数（字符数 / 2.5，中英文混合经验值）。"""
    msgs = data.get("messages")
    if not isinstance(msgs, list):
        return 0
    total = 0
    for m in msgs:
        if not isinstance(m, dict):
            continue
        c = m.get("content")
        if isinstance(c, str):
            total += len(c)
        elif isinstance(c, list):
            for p in c:
                if isinstance(p, dict):
                    total += len(str(p.get("text", "")))
        total += len(str(m.get("tool_calls", "")))
    return int(total / 2.5)


def economic_reroute(data: dict) -> str | None:
    """按 config.json 的 routing 规则把轻请求改走便宜模型。

    配置示例（默认全部关闭）：
      "routing": {
        "economy": {
          "enabled": true,
          "rules": [
            {"from": "GLM-5.3", "to": "codely-flash", "max_prompt_chars": 60000}
          ]
        }
      }
    返回改写说明（未改写返回 None）。
    """
    eco = (load_config().get("routing") or {}).get("economy") or {}
    if not eco.get("enabled"):
        return None
    model = data.get("model", "")
    rules = eco.get("rules") or []
    est = None
    for rule in rules:
        if rule.get("from") != model:
            continue
        if est is None:
            est = _estimate_tokens(data)
        max_chars = rule.get("max_prompt_chars", 60000)
        # 用字符数直接比较，避免估算误差把大请求错误降级
        chars = est * 5 // 2  # 反推字符数
        if chars <= max_chars:
            data["model"] = rule.get("to", model)
            return f"{model}→{data['model']}（经济路由：{chars:,} 字符 ≤ {max_chars:,}）"
    return None


# ─── 角色别名路由 ────────────────────────────────────────────────────────────

def get_role_map() -> dict:
    """config.json 的 roles 映射：{"commander": "GLM-5.3", "coder": "deepseek-v4-flash", ...}。"""
    roles = load_config().get("roles") or {}
    if not isinstance(roles, dict):
        return {}
    return {k: v for k, v in roles.items() if isinstance(k, str) and isinstance(v, str) and v}


def resolve_role(data: dict) -> str | None:
    """把 model 字段里的角色别名解析成真实模型名（改写 data["model"]）。

    客户端只需把 model 设为角色名（commander/coder/reviewer 或自定义别名），
    代理按 config.json 的 roles 映射替换成真实模型，再走既有路由链
    （视觉改路由 → 经济路由 → provider 匹配 → 账号池）。
    大小写不敏感。返回解析说明（未命中返回 None）。
    """
    model = data.get("model", "")
    if not model or not isinstance(model, str):
        return None
    roles = get_role_map()
    target = roles.get(model)
    if target is None:
        for k, v in roles.items():
            if k.lower() == model.lower():
                target = v
                break
    if target and target != model:
        data["model"] = target
        return f"{model} → {target}"
    return None


def role_of_model(model: str) -> str | None:
    """反查模型名对应的角色别名（成本看板用）。一个模型可能对应多个角色，取第一个。"""
    for role, target in get_role_map().items():
        if target == model:
            return role
    return None
