"""常量与配置加载。"""

import json
import logging
import os
from pathlib import Path

log = logging.getLogger("qidi-relay")

BASE_DIR = Path(__file__).resolve().parent.parent

CONFIG_FILE = BASE_DIR / "config.json"
ACCOUNTS_FILE = BASE_DIR / "accounts.json"
STATS_FILE = BASE_DIR / "token_stats.json"
WATER_FILE = BASE_DIR / "water_check.json"
LOG_FILE = BASE_DIR / "proxy.log"
# 会话粘性记忆（阶段 B / 方案 v2 §4）：内存表 +
# 后台线程 ≤2s 周期原子落盘（tmp+rename）。路径
# 常量可注入（测试重定向到 TMP——方案 §7.7 纪律：
# 新模块一律走 settings.py 路径常量模式，禁止
# seedguard 式 BASE_DIR 兜底直写仓库数据文件）。
CONVERSATIONS_FILE = BASE_DIR / "conversations.json"

# 语料采集（阶段 E / 方案 §7）：本地语料落盘目录
# （local_YYYYMM.jsonl 与云端 corpus_pull 拉取的
# corpus_YYYYMM.jsonl 按文件名隔离——§7.2 分名设计
# 经审计 PASS）。路径常量可注入（测试重定向到
# TMP——与 CONVERSATIONS_FILE 同纪律）。
CORPUS_DIR = BASE_DIR / "corpus_local"

LITELLM_API_BASE = "https://codely-litellm.tuanjie.cn"
CLI_API_KEY_FETCH_URL = "https://codely.tuanjie.cn/api/api-token/cli-api-key"

# OpenCode serve 桥默认地址（T12，opencode-serve 通道）。
# 新模块一律走 settings.py 路径常量模式（§7.7 纪律）：缺省
# http://127.0.0.1:4096（`opencode serve --port 4096` 常驻桥），
# config.json 的 providers.<name>.base_url 可显式覆盖；测试可
# 直接重定向本常量（monkeypatch proxy_core.opencode.DEFAULT_
# SERVE_BASE 或 settings.OPENCODE_SERVE_BASE）。
OPENCODE_SERVE_BASE = "http://127.0.0.1:4096"

DEFAULT_PORT = 8765
VERSION = "5.0.0-dev"

# 支持视觉（图片）输入的上游模型；其余模型收到图片内容时会被代理改路由到 vision_model
# 可在 config.json 用 "vision_capable_models": [...] 追加（如外部 provider 的视觉模型）
DEFAULT_VISION_CAPABLE_MODELS = {"codely-vl"}
DEFAULT_VISION_MODEL = "codely-vl"


# 重复键检查（F 小修）：json 的 dict 语义会静默丢弃重复键
# （后者覆盖前者）。config.json 曾因此出现 pricing 节
# "deepseek-v4-flash" 与 "DeepSeek-V4-Flash" 双键并存——
# 计费/路由按大小写敏感键取值，静默覆盖等于配置漂移。
# 用 object_pairs_hook 在解析时收集"大小写折叠后重复"的
# 键路径并告警（只告警，解析行为不变）。
CONFIG_DUPLICATE_KEY_WARNINGS: list = []
_MAX_DUP_KEY_WARNINGS = 20   # 告警上限：防坏配置灌爆日志/面板


class _DupKeyNode(dict):
    """object_pairs_hook 产物：除 json 语义的 dict 外，额外
    记住本层全部键值对（含被覆盖的重复键），供事后按路径
    收集重复键。是 dict 的子类——对 load_config 的调用方
    完全透明（.get/[]/items 语义不变）。"""

    def __init__(self, pairs):
        super().__init__(pairs)   # 与 json.loads 一致：后者覆盖前者
        self._dup_pairs = pairs


def _collect_dup_keys(node, prefix: str, out: list) -> None:
    """递归收集"大小写折叠后重复"的键路径（去重，超上限即停）。

    数组内的对象以 "键[]" 记路径（配置里数组元素几乎不
    重复，仅防御性覆盖）。
    """
    if len(out) >= _MAX_DUP_KEY_WARNINGS:
        return
    folded: dict = {}
    for k, _v in node._dup_pairs:
        if not isinstance(k, str):
            continue
        f = k.casefold()
        if f in folded:
            if folded[f] == k:
                # 两处拼写完全相同：旧文案 "X 与 X" 冗余
                # （T9 P3-8b）——明示重复出现即可
                path = (f"{prefix}{k} 重复出现"
                        "（两处拼写完全相同）")
            else:
                path = (f"{prefix}{folded[f]} 与 {prefix}{k}"
                        " 大小写折叠后重复")
            if path not in out:
                out.append(path)
            if len(out) >= _MAX_DUP_KEY_WARNINGS:
                return
        else:
            folded[f] = k
    for k, v in node._dup_pairs:
        if isinstance(v, _DupKeyNode):
            _collect_dup_keys(v, f"{prefix}{k}.", out)
        elif isinstance(v, list):
            for item in v:
                if isinstance(item, _DupKeyNode):
                    _collect_dup_keys(item, f"{prefix}{k}[].", out)
        if len(out) >= _MAX_DUP_KEY_WARNINGS:
            return


def load_config() -> dict:
    """加载管理员配置，文件缺失或损坏时返回安全默认值。

    大小写折叠后重复的键路径收集进 CONFIG_DUPLICATE_KEY_WARNINGS
    （每次加载先收集局部表、成功后整体替换——并发读者
    不见半表；上限 20 条、去重）——解析结果本身不变
    （重复键仍按 json 语义后者覆盖前者）。
    """
    if not CONFIG_FILE.exists():
        CONFIG_DUPLICATE_KEY_WARNINGS[:] = []
        return {"admin_password": None, "allowed_ips": ["*"]}
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = json.load(f, object_pairs_hook=_DupKeyNode)
    except Exception:
        CONFIG_DUPLICATE_KEY_WARNINGS[:] = []
        return {"admin_password": None, "allowed_ips": ["*"]}
    # 先收集局部表、成功后整体替换（T9 P3-8a）：消除
    # 并发 clear/append 交错——读者要么见旧表要么见
    # 新表，不见半表。原地切片赋值而非重绑名字：
    # from 导入方（routes/proxy.py:43）持有原表对象
    # 引用，重绑会使其读到陈旧表。
    warnings: list = []
    if isinstance(cfg, _DupKeyNode):
        _collect_dup_keys(cfg, "", warnings)
    CONFIG_DUPLICATE_KEY_WARNINGS[:] = warnings
    return cfg


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


def _parse_budget(item: dict, daily_limit: int | None) -> dict | None:
    """解析每 key 预算限额块（阶段 C / 方案 §5.1）。

    返回 None = 未配置限额。limit.tokens 优先于旧
    daily_token_limit；无 limit 块但有 daily_token_limit
    时按迁移语义视为 {"period": "day", "tokens":
    daily_token_limit}——旧配置零改动即获得 day 窗
    预算限额。
    """
    raw = item.get("limit")
    if raw is None:
        if daily_limit is None:
            return None
        # 迁移语义：只写了旧 daily_token_limit 时视为
        # day 窗 tokens 限额（limit 块缺省）。
        return {"period": "day", "tokens": daily_limit,
                "cost": None, "cache_reads": False}
    if not isinstance(raw, dict):
        log.warning("api_keys 条目的 limit 块应为对象，忽略该限额配置")
        return None
    period = str(raw.get("period") or "day")
    if period != "day":
        # v1 只实现 day 窗：其他 period 值告警并按 day 处理（方案 §5.1）。
        log.warning("api key 预算 limit.period=%s 暂不支持（v1 仅 day），按 day 处理",
                    period)
    tokens = raw.get("tokens")
    try:
        tokens = int(tokens) if tokens is not None else None
    except (TypeError, ValueError):
        tokens = None
    cost = raw.get("cost")
    try:
        cost = float(cost) if cost is not None else None
    except (TypeError, ValueError):
        cost = None
    return {"period": "day", "tokens": tokens, "cost": cost,
            "cache_reads": bool(raw.get("cache_reads", False))}


# key label 撞名去重（T9 P1-1）：两个条目 label 相同
# （含缺省 label = key 前 12 位截断撞名）时，解析期
# 去重命名——首个保持原名，后续同名条目加 #2/#3 后缀
# （确定性：按配置出现顺序、后缀递增避开已用 label），
# 保证同一 label 唯一映射一个 key。每个 (原名, 新名)
# 对只告警一次（get_api_keys 每请求高频调用，防刷屏）。
_LABEL_DEDUPE_WARNED: set = set()


def _dedupe_key_labels(out: dict) -> dict:
    """label 撞名去重（首个保持原名，后续同名条目
    加 #2/#3 后缀）。返回新表（键顺序不变）。"""
    if not out:
        return out
    used: set = set()
    deduped: dict = {}
    for k, v in out.items():
        label = v["label"]
        final = label
        if final in used:
            i = 2
            while f"{label}#{i}" in used:
                i += 1
            final = f"{label}#{i}"
            if (label, final) not in _LABEL_DEDUPE_WARNED:
                _LABEL_DEDUPE_WARNED.add((label, final))
                log.warning(
                    "api_keys 两个条目 label 撞名（%s）："
                    "首个保持原名，后续条目重命名为 %s",
                    label, final)
        used.add(final)
        if final == label:
            deduped[k] = v
        else:
            v2 = dict(v)
            v2["label"] = final
            deduped[k] = v2
    return deduped


def _parse_api_keys(raw) -> dict:
    """归一化 api_keys 配置 -> {key: {label, limit, budget}}。

    支持两种格式（可混用）：
    - 简单字符串列表：["sk-aaa", "sk-bbb"]（label = key 前缀）
    - 对象列表：[{"key": "sk-aaa", "label": "张三",
                  "daily_token_limit": 200000,
                  "limit": {"period": "day", "tokens": 2000000,
                            "cost": 5.0, "cache_reads": false}}]
      label 缺省用 key 前缀；daily_token_limit 为可选的每 key
      日 token 限额（旧字段）；limit 块为客户端预算限额
      （阶段 C / 方案 §5.1，优先于旧字段）。

    budget 语义（方案 §5.1）：
    - limit.tokens 优先——与 daily_token_limit 并存时旧字段
      不参与阈值判定（预算检查兜住同一阈值）；
    - 无 limit 块但有 daily_token_limit → 迁移为
      {"period": "day", "tokens": daily_token_limit}；
    - 返回的 "limit" 字段 = 生效的日 token 阈值
      （limit.tokens 优先），既有 daily_token_limit_exceeded
      检查与预算检查共用同一阈值。
    """
    out = {}
    if not isinstance(raw, list):
        return out
    for item in raw:
        if isinstance(item, str):
            k = item.strip()
            if k:
                out[k] = {"label": k[:12] + "…" if len(k) > 12 else k,
                          "limit": None, "budget": None}
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
            budget = _parse_budget(item, limit)
            # limit.tokens 优先：limit 块生效时旧字段不参与
            # 阈值判定（新机制预算检查兜住同一阈值）。
            effective = (budget.get("tokens")
                         if budget is not None and budget.get("tokens") is not None
                         else limit)
            out[k] = {"label": label, "limit": effective, "budget": budget}
    return _dedupe_key_labels(out)


def get_api_keys() -> dict:
    """需要鉴权的 API Key 表 {key: {label, limit, budget}}；空 = 不鉴权（纯局域网模式向后兼容）。"""
    return _parse_api_keys(load_config().get("api_keys", []))


def get_api_key_entries() -> list:
    """API key 明细（管理面板展示用；含 key 前缀脱敏，不含完整 key）。"""
    return [
        {"label": v["label"], "limit": v["limit"],
         "budget": v.get("budget"),
         "key_preview": k[:8] + "…" + k[-4:] if len(k) > 14 else k}
        for k, v in get_api_keys().items()
    ]
