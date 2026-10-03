"""外部模型提供商（OpenAI 兼容第三方端点，静态 API Key）。

config.json 示例：
{
  "providers": {
    "agnes": {
      "base_url": "https://api.agnes-ai.cn/v1",
      "api_keys": [
        "sk-xxx",
        "sk-yyy",
        {"key": "sk-zzz", "label": "会员"}
      ],
      "models": ["agnes-2.0-flash", "agnes-2.5-flash", "agnes-2.5-pro"]
    },
    "atomcode": {
      "type": "atomcode",
      "version": "5.0.8",
      "models": ["deepseek-v4-flash"]
    }
  }
}

请求的 model 命中某 provider 的 models 列表时，直接转发到该 provider 的
/chat/completions，不占用主账号池，也不做上游签名。
配置带 mtime 缓存，改 config.json 免重启即生效。

**多 key 池**：api_key（单 key，向后兼容）与 api_keys（数组）可任用其一或
混用（自动去重合并）。数组元素是字符串或 {"key": ..., "label": ...}；
label 缺省为 "<provider>·<序号>"。选号策略与主账号池同款：
最少进行中请求优先，同负载轮转。同一 key 重复出现 = 更高选中权重。
非流式请求遇 401/402/403/429 自动换下一个 key 重试（详见 routes/proxy.py）。

type 为 "atomcode" 的 provider 不需要 api_key：凭证从官方客户端的
~/.atomcode/auth.toml 自动加载（用户登录官方客户端后自动生效），
请求头由 proxy_core.atomcode 的 sign_v1 动态签名。
"""

import logging
import threading

from . import atomcode
from . import cline
from . import minicode
from .router import registry
from .settings import CONFIG_FILE, load_config

log = logging.getLogger("qidi-relay")

_lock = threading.Lock()
_cache = None  # (mtime, {name: provider dict})
_key_rr = {}   # provider name -> 轮转游标（同负载时轮转选 key）


def _parse_keys(name: str, p: dict) -> list:
    """把 api_key / api_keys 归一化为 [{"key": ..., "label": ...}] 列表。

    规则：
    - 数组元素可为字符串或 {"key": ..., "label": ...}；label 缺省 "<provider>·<序号>"
    - 带 label 的重复 key 保留（故意加权重）；无 label 的重复 key 去重
      （防 api_key 单值与 api_keys 数组意外重复）
    """
    raw_entries = []
    arr = p.get("api_keys")
    if isinstance(arr, list):
        for k in arr:
            if isinstance(k, str) and k.strip():
                raw_entries.append((k.strip(), ""))
            elif isinstance(k, dict) and isinstance(k.get("key"), str) and k.get("key").strip():
                raw_entries.append((k["key"].strip(), str(k.get("label") or "").strip()))
    single = p.get("api_key")
    if isinstance(single, str) and single.strip():
        raw_entries.append((single.strip(), ""))

    # 保持输入顺序；无 label 的重复 key 去重，带 label 的重复保留
    out = []
    present = set()
    for key, label in raw_entries:
        if label:
            out.append({"key": key, "label": label})
            present.add(key)
        elif key not in present:
            out.append({"key": key, "label": ""})  # 占位，稍后补默认 label
            present.add(key)
    # 补默认 label（防与显式 label 撞名——label 是负载跟踪的 key，必须唯一）
    used = {o["label"] for o in out if o["label"]}
    n = 1
    for o in out:
        if not o["label"]:
            default = f"{name}·{n}"
            while default in used:
                n += 1
                default = f"{name}·{n}"
            o["label"] = default
            used.add(default)
            n += 1
    return out


def _parse_aliases(name: str, p: dict) -> dict:
    """model_aliases: {"请求别名": "上游真实模型ID"}。

    别名命中时按别名路由到本 provider，_forward_external 转发前把
    data["model"] 重写为上游真实 ID。用途：上游模型名与本机已有模型
    大小写不敏感撞名时（如 glm-5.3 撞免费池 GLM-5.3），用带前缀的
    别名安全接入，避免触发守卫变体规范化分支劫持/抖动路由。
    """
    raw = p.get("model_aliases")
    if not isinstance(raw, dict):
        return {}
    out = {}
    for k, v in raw.items():
        if isinstance(k, str) and isinstance(v, str) and k.strip() and v.strip():
            out[k.strip()] = v.strip()
    return out


def _load_providers() -> dict:
    """读取 config.json 的 providers 配置（mtime 变化时自动重载）。"""
    global _cache
    try:
        mtime = CONFIG_FILE.stat().st_mtime
    except OSError:
        return {}
    with _lock:
        if _cache and _cache[0] == mtime:
            return _cache[1]
        cfg = load_config()
        atomcode.reload_channels_from_config(cfg)
        cline.reload_channels_from_config(cfg)
        minicode.reload_channels_from_config(cfg)
        provs = {}
        for name, p in (cfg.get("providers") or {}).items():
            if not isinstance(p, dict):
                continue
            base_url = (p.get("base_url") or "").rstrip("/")
            models = [m for m in (p.get("models") or []) if isinstance(m, str)]
            if p.get("type") == "atomcode":
                # AtomCode 通道：无静态 key，签名头动态生成；base_url 默认官方网关
                if models:
                    provs[name] = {
                        "name": name,
                        "type": "atomcode",
                        "base_url": base_url or atomcode.DEFAULT_LLM_BASE,
                        "api_key": None,
                        "keys": [],
                        "models": models,
                    }
                else:
                    log.warning("provider %s (atomcode) 未配置 models，已忽略", name)
            elif p.get("type") == "cline":
                # Cline 官方通道：无静态 key，凭证从官方客户端 providers.json
                # 动态读取（mtime 感知 + 过期自刷新写回）；base_url 固定官方网关
                if models:
                    provs[name] = {
                        "name": name,
                        "type": "cline",
                        "base_url": cline.API_BASE,
                        "api_key": None,
                        "keys": [],
                        "models": models,
                    }
                else:
                    log.warning("provider %s (cline) 未配置 models，已忽略", name)
            elif p.get("type") == "minicode":
                # MiniCode（mcode / MiniMax Code CLI）官方通道：无静态 key，凭证从
                # ~/.minimax/auth/prod/<region>/mcode-public/auth.json 动态读取
                # （mtime 感知 + 过期自动刷新写回）；Anthropic 协议由
                # proxy_core.minicode + routes/proxy._forward_minicode 负责转换
                if models:
                    chs = minicode.channels_for(name)
                    base = base_url or (chs[0].api_base if chs else minicode.API_BASE_CN)
                    provs[name] = {
                        "name": name,
                        "type": "minicode",
                        "base_url": base,
                        "api_key": None,
                        "keys": [],
                        "models": models,
                        "model_aliases": _parse_aliases(name, p),
                    }
                else:
                    log.warning("provider %s (minicode) 未配置 models，已忽略", name)
            else:
                keys = _parse_keys(name, p)
                if base_url and keys and models:
                    provs[name] = {
                        "name": name,
                        "base_url": base_url,
                        "api_key": keys[0]["key"],  # 兼容字段（water 探针等单 key 消费者）
                        "keys": keys,
                        "models": models,
                        "model_aliases": _parse_aliases(name, p),
                    }
                else:
                    log.warning("provider %s 配置不完整（需 base_url/api_keys/models），已忽略", name)
        _cache = (mtime, provs)
        return provs


def pick_key(prov: dict, exclude: set = None) -> dict | None:
    """从 provider 的 key 池选一个 key：最少进行中请求优先，同负载轮转。

    返回 {"key": ..., "label": ...}；无可用 key 返回 None。
    exclude：重试时排除已失败的 key（按 key 本体匹配）。
    """
    keys = prov.get("keys") or []
    if exclude:
        keys = [k for k in keys if k["key"] not in exclude]
    if not keys:
        return None
    if len(keys) == 1:
        return keys[0]
    # 负载键加 provider 前缀隔离命名空间（防跨 provider 同名 label 污染负载计数）
    loads = {k["label"]: registry.load_of(f"key:{prov['name']}|{k['label']}") for k in keys}
    start = _key_rr.get(prov["name"], 0) % len(keys)
    best = None
    for i in range(len(keys)):
        k = keys[(start + i) % len(keys)]
        if best is None or loads[k["label"]] < loads[best["label"]]:
            best = k
    _key_rr[prov["name"]] = (start + 1) % len(keys)
    return best


def match(model: str):
    """model 命中外部 provider 时返回其配置，否则 None。

    命中范围 = models 直配名 ∪ model_aliases 别名（0928）。
    """
    if not model:
        return None
    for prov in _load_providers().values():
        if model in prov["models"] or model in (prov.get("model_aliases") or {}):
            return prov
    return None


def all_models() -> list:
    """所有外部模型条目（供 /v1/models 合并展示）。含别名（0928）。

    1001: 别名键与 models 直配名相同（minicode 的"公开名=别名键"模式）时去重，
    防止 /v1/models 同一模型重复列出。
    """
    out = []
    for prov in _load_providers().values():
        for m in prov["models"]:
            out.append({"id": m, "object": "model", "owned_by": prov["name"]})
        for a in (prov.get("model_aliases") or {}):
            if a in prov["models"]:
                continue  # 与直配名重复，只展示一次
            out.append({"id": a, "object": "model", "owned_by": prov["name"]})
    return out


def all_providers() -> dict:
    """所有已配置的外部 provider（含 key，仅供服务端管理接口使用，禁止直接返给前端）。"""
    return _load_providers()


def is_atomcode(prov: dict) -> bool:
    """该 provider 是否为 AtomCode 签名通道。"""
    return bool(prov) and prov.get("type") == "atomcode"


def is_cline(prov: dict) -> bool:
    """该 provider 是否为 Cline 官方通道（凭证/指纹头由 proxy_core.cline 管理）。"""
    return bool(prov) and prov.get("type") == "cline"


def is_minicode(prov: dict) -> bool:
    """该 provider 是否为 MiniCode（mcode）官方通道（凭证/协议转换由 proxy_core.minicode 负责）。"""
    return bool(prov) and prov.get("type") == "minicode"
