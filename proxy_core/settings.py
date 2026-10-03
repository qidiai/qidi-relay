"""常量与配置加载。"""

import json
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

CONFIG_FILE = BASE_DIR / "config.json"
ACCOUNTS_FILE = BASE_DIR / "accounts.json"
STATS_FILE = BASE_DIR / "token_stats.json"
WATER_FILE = BASE_DIR / "water_check.json"
LOG_FILE = BASE_DIR / "proxy.log"

LITELLM_API_BASE = "https://codely-litellm.tuanjie.cn"
CLI_API_KEY_FETCH_URL = "https://codely.tuanjie.cn/api/api-token/cli-api-key"

DEFAULT_PORT = 8765
VERSION = "5.0.0-dev"

# 支持视觉（图片）输入的上游模型；其余模型收到图片内容时会被代理改路由到 vision_model
# 可在 config.json 用 "vision_capable_models": [...] 追加（如外部 provider 的视觉模型）
DEFAULT_VISION_CAPABLE_MODELS = {"codely-vl"}
DEFAULT_VISION_MODEL = "codely-vl"


def load_config() -> dict:
    """加载管理员配置，文件缺失或损坏时返回安全默认值。"""
    if not CONFIG_FILE.exists():
        return {"admin_password": None, "allowed_ips": ["*"]}
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"admin_password": None, "allowed_ips": ["*"]}


def save_config(cfg: dict) -> None:
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def get_port() -> int:
    """端口优先级：RELAY_PORT 环境变量 > config.json "port" > 默认 8765。

    RELAY_PORT 供开发验证用（例：`set RELAY_PORT=8899 && py -3 main.py`）：
    同一目录与生产 8765 并行跑第二实例，不动生产配置不抢生产端口。
    """
    env = os.environ.get("RELAY_PORT")
    if env:
        try:
            return int(env)
        except (TypeError, ValueError):
            pass
    try:
        return int(load_config().get("port", DEFAULT_PORT))
    except (TypeError, ValueError):
        return DEFAULT_PORT


def get_vision_model() -> str:
    """视觉改路由目标模型，可在 config.json 中用 "vision_model" 覆盖。"""
    m = load_config().get("vision_model", DEFAULT_VISION_MODEL)
    return m if isinstance(m, str) and m else DEFAULT_VISION_MODEL


def get_vision_capable_models() -> set:
    """视觉能力模型集合 = 内置默认 ∪ config.json 的 "vision_capable_models"。"""
    extra = load_config().get("vision_capable_models", [])
    result = set(DEFAULT_VISION_CAPABLE_MODELS)
    if isinstance(extra, list):
        result.update(m for m in extra if isinstance(m, str))
    return result


# thinking（推理）模型：上游会把未指定的 tool_choice 处理成 'specified' 而 400
# （"tool_choice 'specified' is incompatible with thinking enabled"），
# 代理对这些模型在带 tools 的请求上显式补 tool_choice="auto"。
DEFAULT_THINKING_MODELS = ["KIMI-K3", "GLM-5.3", "GLM-5.3-FLASH"]


def get_thinking_models() -> list:
    m = load_config().get("thinking_models", DEFAULT_THINKING_MODELS)
    if isinstance(m, list):
        return [x for x in m if isinstance(x, str) and x.strip()]
    return list(DEFAULT_THINKING_MODELS)


def _parse_api_keys(raw) -> dict:
    """归一化 api_keys 配置 -> {key: label}。

    支持两种格式（可混用）：
    - 简单字符串列表：["sk-aaa", "sk-bbb"]（label = key 前缀）
    - 对象列表：[{"key": "sk-aaa", "label": "张三", "daily_token_limit": 200000}]
      label 缺省用 key 前缀；daily_token_limit 为可选的每 key 日 token 限额。
    """
    out = {}
    if not isinstance(raw, list):
        return out
    for item in raw:
        if isinstance(item, str):
            k = item.strip()
            if k:
                out[k] = {"label": k[:12] + "…" if len(k) > 12 else k, "limit": None}
        elif isinstance(item, dict):
            k = str(item.get("key") or "").strip()
            if not k:
                continue
            label = str(item.get("label") or "").strip() or (k[:12] + "…" if len(k) > 12 else k)
            limit = item.get("daily_token_limit")
            try:
                limit = int(limit) if limit is not None else None
            except (TypeError, ValueError):
                limit = None
            out[k] = {"label": label, "limit": limit}
    return out


def get_api_keys() -> dict:
    """需要鉴权的 API Key 表 {key: {label, limit}}；空 = 不鉴权（纯局域网模式向后兼容）。"""
    return _parse_api_keys(load_config().get("api_keys", []))


def get_api_key_entries() -> list:
    """API key 明细（管理面板展示用；含 key 前缀脱敏，不含完整 key）。"""
    return [
        {"label": v["label"], "limit": v["limit"], "key_preview": k[:8] + "…" + k[-4:] if len(k) > 14 else k}
        for k, v in get_api_keys().items()
    ]
