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
混用（自动去重合并）。数组元素是字符串或
{"key": ..., "label": ..., "disabled": true}；
label 缺省为 "<provider>·<序号>"。选号策略与主账号池同款：
最少进行中请求优先，同负载轮转。同一 key 重复出现 = 更高选中权重。
非流式请求遇 401/402/403/429 自动换下一个 key 重试（详见 routes/proxy.py）。
**F14 per-key 启停**：数组 dict 元素可带 "disabled": true——停用的 key
保留在池内（不清除本体）但不参与选号（pick_key 跳过），且不计入面板
key_count；用于额度耗尽的 key 先停用、额度重置后再启用。

type 为 "atomcode" 的 provider 不需要 api_key：凭证从官方客户端的
~/.atomcode/auth.toml 自动加载（用户登录官方客户端后自动生效），
请求头由 proxy_core.atomcode 的 sign_v1 动态签名。

**附加请求头**（T13，OpenCode Go 网关直连）：静态 key 型 provider 可选配
  - "headers": {"User-Agent": "my-agent/1.0"}     静态附加头（每请求原样带上）
  - "session_header": "x-opencode-session"        会话头名——每请求自动生成
    一个 sess_<hex> 会话 ID 填入该头（Go 网关的路由/提示缓存优化与滥用识别
    要求；会话级稳定 ID 由客户端侧按 conversation 维度另议，v1 按请求粒度）
典型用途：网关拒绝通用 SDK UA（python-httpx）或要求带会话头的场景。
"""

import logging
import threading

from . import atomcode
from . import cline
from . import minicode
from . import opencode
from .router import registry
from .settings import CONFIG_FILE, load_config

log = logging.getLogger("qidi-relay")

_lock = threading.Lock()
_cache = None  # (mtime, {name: provider dict})
_key_rr = {}   # provider name -> 轮转游标（同负载时轮转选 key）


def _parse_keys(name: str, p: dict) -> list:
    """把 api_key / api_keys 归一化为 [{"key", "label"[, "disabled"]}] 列表。

    规则：
    - 数组元素可为字符串或 {"key": ..., "label": ...}；label 缺省 "<provider>·<序号>"
    - 带 label 的重复 key 保留（故意加权重）；无 label 的重复 key 去重
      （防 api_key 单值与 api_keys 数组意外重复）
    - disabled（F14 per-key 启停）：dict 条目透传其 disabled 真值——仅当
      停用时输出 "disabled": True（启用态省略该键，保持 config 干净）；
      str 条目恒为启用态。停用的 key 保留在池内但不参与 pick_key 选号。
    """
    raw_entries = []  # (key, label, disabled)
    arr = p.get("api_keys")
    if isinstance(arr, list):
        for k in arr:
            if isinstance(k, str) and k.strip():
                raw_entries.append((k.strip(), "", False))
            elif isinstance(k, dict) and isinstance(k.get("key"), str) and k.get("key").strip():
                raw_entries.append((k["key"].strip(), str(k.get("label") or "").strip(),
                                    bool(k.get("disabled"))))
    single = p.get("api_key")
    if isinstance(single, str) and single.strip():
        raw_entries.append((single.strip(), "", False))

    # 保持输入顺序；无 label 的重复 key 去重，带 label 的重复保留
    out = []
    present = set()
    for key, label, disabled in raw_entries:
        entry = {"key": key, "label": label}
        if disabled:
            entry["disabled"] = True
        if label:
            out.append(entry)
            present.add(key)
        elif key not in present:
            out.append(entry)  # 占位，稍后补默认 label
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


def _parse_headers(name: str, p: dict) -> dict:
    """headers: {"静态附加头名": "值"}（T13）。

    只收 str→str，其余（非字符串值/空名）忽略——坏条目绝不静默把一个
    非字符串值塞进 httpx headers（那会在请求时炸 TypeError）。
    """
    raw = p.get("headers")
    if not isinstance(raw, dict):
        return {}
    return {str(k).strip(): str(v)
            for k, v in raw.items()
            if isinstance(k, str) and k.strip() and isinstance(v, str) and v.strip()}


def _parse_session_header(name: str, p: dict) -> str:
    """session_header: 每请求自动生成会话 ID 填入的请求头名（T13）。

    空串/非字符串 = 不启用（缺省行为逐字节不变）。
    """
    v = p.get("session_header")
    return v.strip() if isinstance(v, str) and v.strip() else ""


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


def is_enabled(p: dict) -> bool:
    """provider 启用开关（config 节 enabled，缺省启用，T10）。

    非布尔防御：config 手误（如 "enabled": "false" 字符串、
    0/1 数字）一律按启用处理——宁可用到报错的池，也不静默
    关错池（isinstance 检查，非真值化）。
    """
    v = p.get("enabled", True) if isinstance(p, dict) else True
    return v if isinstance(v, bool) else True


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
        opencode.reload_from_config(cfg)
        provs = {}
        for name, p in (cfg.get("providers") or {}).items():
            if not isinstance(p, dict):
                continue
            enabled = is_enabled(p)  # T10: enabled 开关（缺省启用）
            base_url = (p.get("base_url") or "").rstrip("/")
            models = [m for m in (p.get("models") or []) if isinstance(m, str)]
            if p.get("type") == "atomcode":
                # AtomCode 通道：无静态 key，签名头动态生成；base_url 默认官方网关
                if models:
                    provs[name] = {
                        "name": name,
                        "enabled": enabled,
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
                        "enabled": enabled,
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
                        "enabled": enabled,
                        "type": "minicode",
                        "base_url": base,
                        "api_key": None,
                        "keys": [],
                        "models": models,
                        "model_aliases": _parse_aliases(name, p),
                    }
                else:
                    log.warning("provider %s (minicode) 未配置 models，已忽略", name)
            elif p.get("type") == "opencode-serve":
                # OpenCode serve 桥通道（T12）：无静态 key，OpenCode Go 订阅的
                # free 模型只能经 `opencode serve` 桥（本机 127.0.0.1:4096）
                # 调用——key 直连官方 zen 网关 403/402。base_url 缺省取桥默认
                # 地址（settings.OPENCODE_SERVE_BASE，可注入），config 可显式覆盖。
                # 桥掉线是"可判负"的外部通道：判负只耗 attempt 换号，不触发冷却。
                if models:
                    provs[name] = {
                        "name": name,
                        "enabled": enabled,
                        "type": "opencode-serve",
                        "base_url": base_url or opencode.DEFAULT_SERVE_BASE,
                        "api_key": None,
                        "keys": [],
                        "models": models,
                    }
                else:
                    log.warning("provider %s (opencode-serve) 未配置 models，已忽略", name)
            else:
                keys = _parse_keys(name, p)
                if base_url and keys and models:
                    provs[name] = {
                        "name": name,
                        "enabled": enabled,
                        "base_url": base_url,
                        "api_key": keys[0]["key"],  # 兼容字段（water 探针等单 key 消费者）
                        "keys": keys,
                        "models": models,
                        "model_aliases": _parse_aliases(name, p),
                        # T13：静态附加头 + 会话头（Go 网关直连准入要求）。
                        # 缺省 {} 与 ""——行为与升级前逐字节一致。
                        "headers": _parse_headers(name, p),
                        "session_header": _parse_session_header(name, p),
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
    # F14: 停用（disabled）的 key 保留在池内但不参与选号——额度耗尽的 key
    # 先停用，待额度重置再启用。与 exclude 叠加过滤后为空 → None
    # （调用方 _forward_external 的 None 处理链已存在：无可用 key 直接判负）。
    keys = [k for k in keys if not k.get("disabled")]
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
        if not prov.get("enabled", True):
            continue  # T10: 已停用——池关闭，模型不可达（请求落模型守卫 404）
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
        if not prov.get("enabled", True):
            continue  # T10: 已停用——模型从 /v1/models 消失
        for m in prov["models"]:
            out.append({"id": m, "object": "model", "owned_by": prov["name"]})
        for a in (prov.get("model_aliases") or {}):
            if a in prov["models"]:
                continue  # 与直配名重复，只展示一次
            out.append({"id": a, "object": "model", "owned_by": prov["name"]})
    return out


def all_providers() -> dict:
    """所有已配置的外部 provider（含 key，仅供服务端管理接口使用，禁止直接返给前端）。

    T10: 含已停用（enabled=false）的 provider——其 prov 带
    "enabled": False，管理面板据此展示"已停用"态并可再启用；
    路由/模型列表层（match/all_models）已将其跳过。
    """
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


def is_opencode(prov: dict) -> bool:
    """该 provider 是否为 OpenCode serve 桥通道（协议转换/会话压缩由 proxy_core.opencode 负责）。"""
    return bool(prov) and prov.get("type") == "opencode-serve"
