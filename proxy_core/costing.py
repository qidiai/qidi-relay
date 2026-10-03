"""成本看板：可配价格表 + 按角色聚合的 token/成本报表。

价格表在 config.json 的 pricing 节配置（单位：元 / 百万 token）：

  "pricing": {
    "GLM-5.3": {"input": 4, "output": 16},
    "deepseek-v4-flash": {"input": 0, "output": 0}
  }

未配置价格的模型按 0 计（订阅内 / 免费通道）。角色归属用
router.role_of_model 反查——统计仍按"账号×模型"落盘（token_stats.json），
报表在查询时聚合，改角色映射或价格表后刷新面板即生效，无需迁移数据。
"""

from .router import get_role_map, role_of_model
from .settings import load_config
from .state import token_stats


def get_pricing() -> dict:
    """读 config.json pricing 节，容错解析为 {model: {"input": f, "output": f}}。"""
    pricing = load_config().get("pricing")
    if not isinstance(pricing, dict):
        return {}
    out = {}
    for model, p in pricing.items():
        if not isinstance(model, str) or not isinstance(p, dict):
            continue
        try:
            out[model] = {
                "input": float(p.get("input", 0) or 0),
                "output": float(p.get("output", 0) or 0),
            }
        except (TypeError, ValueError):
            continue
    return out


def cost_of(pricing: dict, model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """按价格表计算一次聚合用量的成本（元）。未定价模型按 0。"""
    p = pricing.get(model) or {"input": 0.0, "output": 0.0}
    return prompt_tokens / 1e6 * p["input"] + completion_tokens / 1e6 * p["output"]


def cost_report() -> dict:
    """按角色聚合全量统计：角色行在前（按 roles 配置顺序），未绑角色的模型行在后。"""
    pricing = get_pricing()

    # model -> 聚合（跨账号/provider 合并）
    agg = {}
    for row in token_stats.get_all():
        m = row.get("model") or "未知"
        a = agg.setdefault(m, {
            "model": m, "requests": 0, "prompt_tokens": 0,
            "completion_tokens": 0, "total_tokens": 0,
            "cost": 0.0, "sources": set(),
        })
        a["requests"] += row.get("requests", 0)
        a["prompt_tokens"] += row.get("prompt_tokens", 0)
        a["completion_tokens"] += row.get("completion_tokens", 0)
        a["total_tokens"] += row.get("total_tokens", 0)
        a["cost"] += cost_of(pricing, m, row.get("prompt_tokens", 0), row.get("completion_tokens", 0))
        if row.get("user_id"):
            a["sources"].add(row["user_id"])

    roles = get_role_map()
    role_rows, unbound = [], []
    for m, a in agg.items():
        role = role_of_model(m)
        item = {
            "role": role,
            "model": m,
            "requests": a["requests"],
            "prompt_tokens": a["prompt_tokens"],
            "completion_tokens": a["completion_tokens"],
            "total_tokens": a["total_tokens"],
            "cost": round(a["cost"], 6),
            "priced": m in pricing,
            "sources": sorted(a["sources"]),
        }
        (role_rows if role else unbound).append(item)

    order = {r: i for i, r in enumerate(roles)}
    role_rows.sort(key=lambda x: order.get(x["role"], 99))
    unbound.sort(key=lambda x: -x["total_tokens"])
    all_rows = role_rows + unbound

    return {
        "total_cost": round(sum(r["cost"] for r in all_rows), 6),
        "total_tokens": sum(r["total_tokens"] for r in all_rows),
        "requests": sum(r["requests"] for r in all_rows),
        "rows": all_rows,
        "roles": roles,
        "pricing": pricing,
    }
