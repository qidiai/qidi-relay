"""冷却状态持久化（吸收-1 / 规格 §R-A）。

三套冷却中只有主账号池（accounts.mark_cooldown）自带落盘；
本模块补上另两套的持久化，消灭"重启即丢冷却"：

1. 静态 key provider 级的 `_prov_quota_cooldown`（routes/proxy.py，
   全 key 429 quota → 整渠道冷却 900s）
2. cline / minicode 通道级 `cooldown_until`（429 日配额冷却上限 6h，
   比 900s 更痛——重启丢一次等于白等几小时）

结构：::

    {"version": 1, "saved_at": <ts>,
     "providers": {name: until_ts},
     "channels":  {"prov|label": until_ts}}

设计纪律（与 affinity.py 同款，规格 §R-A 逐条）：
- 路径常量走 settings.COOLDOWNS_FILE（可注入；§7.7 纪律——
  禁止 BASE_DIR 兜底直写仓库数据文件）；
- 懒加载（首次访问 `_ensure_loaded_locked`），导入期绝不触盘；
- 原子写 = affinity._save_to_disk 同款（pid 后缀 tmp + os.replace，
  防双实例共写交错），**不学** stats._save 的非原子直写；
- 后台 daemon 扫尾线程（≤2s 周期，affinity._worker 同款）——但
  **不沿袭其丢窗**：持久化失败保留 dirty 不清、下一轮重试
  （affinity 失败即清 dirty，一次失败永久丢到下次写）。
- config `cooldown.persist`（默认 true）为 false 时**不读不写**，
  整套逻辑成空操作（`_prov_quota_cooldown` 仍纯内存生效）。
"""

import json
import logging
import os
import threading
import time

from proxy_core import settings
from proxy_core.settings import load_config

log = logging.getLogger("qidi-relay")

_VERSION = 1
_SWEEP_S = 2.0   # 后台扫尾间隔（≤2s，affinity 同款）

# 内存表：name -> until_ts / "prov|label" -> until_ts
_providers: dict = {}
_channels: dict = {}
_LOCK = threading.RLock()
_DIRTY = False          # 有未落盘改动
_LOADED = False         # 首次访问已做懒加载
_WORKER_STARTED = False
_WORKER_LOCK = threading.Lock()


def _cooldowns_file():
    """路径常量走 settings（可注入）：每次取现，测试改
    settings.COOLDOWNS_FILE 即生效（§7.7 纪律）。"""
    return settings.COOLDOWNS_FILE


def _enabled() -> bool:
    """config cooldown.persist（默认 true）。
    false → 不读不写（每次请求读 config，与 affinity 的
    features 检查同款；改 config 免重启即生效）。"""
    try:
        cfg = load_config()
    except Exception:
        return True   # 配置读失败按默认 true（宁持久化不丢）
    cooldown = cfg.get("cooldown") if isinstance(cfg, dict) else None
    if isinstance(cooldown, dict):
        return bool(cooldown.get("persist", True))
    return True


# ── provider 节（静态 key provider 级冷却）────────────────

def get_provider(name: str):
    """返回 provider 的有效冷却截止时间戳；无 / 已过期返回 None。"""
    if not _enabled():
        return None
    with _LOCK:
        _ensure_loaded_locked()
        ts = _providers.get(name)
    if ts and ts > time.time():
        return ts
    return None


def set_provider(name: str, until_ts: float) -> None:
    """记录 provider 冷却（内存 + dirty，后台线程落盘）。"""
    if not _enabled():
        return
    global _DIRTY
    with _LOCK:
        _ensure_loaded_locked()
        _providers[name] = until_ts
        _DIRTY = True
    _ensure_worker()


def clear_provider(name: str) -> None:
    """解除 provider 冷却（内存 + dirty；落盘后重启不复活）。"""
    if not _enabled():
        return
    global _DIRTY
    with _LOCK:
        _ensure_loaded_locked()
        if _providers.pop(name, None) is not None:
            _DIRTY = True
    _ensure_worker()


# ── channels 节（cline / minicode 通道级冷却）─────────────

def _ckey(name: str, label: str) -> str:
    return f"{name}|{label}"


def get_channel(name: str, label: str):
    """返回通道的有效冷却截止时间戳；无 / 已过期返回 None。"""
    if not _enabled():
        return None
    with _LOCK:
        _ensure_loaded_locked()
        ts = _channels.get(_ckey(name, label))
    if ts and ts > time.time():
        return ts
    return None


def set_channel(name: str, label: str, until_ts: float) -> None:
    """记录通道冷却（内存 + dirty，后台线程落盘）。"""
    if not _enabled():
        return
    global _DIRTY
    with _LOCK:
        _ensure_loaded_locked()
        _channels[_ckey(name, label)] = until_ts
        _DIRTY = True
    _ensure_worker()


def clear_channel(name: str, label: str) -> None:
    """解除通道冷却（内存 + dirty；落盘后重启不复活）。"""
    if not _enabled():
        return
    global _DIRTY
    with _LOCK:
        _ensure_loaded_locked()
        if _channels.pop(_ckey(name, label), None) is not None:
            _DIRTY = True
    _ensure_worker()


# ── 显式 API（规格 §R-A：persist() / load()）──────────────

def persist() -> bool:
    """立即把内存态原子落盘（成功 True）。persist=false 时空操作。
    成功即清 dirty（整表已写入，无残留改动）。"""
    global _DIRTY
    if not _enabled():
        return False
    with _LOCK:
        ok = _save_to_disk()
        if ok:
            _DIRTY = False
        return ok


def load() -> dict:
    """从 cooldowns.json 强制重读（丢弃过期条目），返回
    {"providers": ..., "channels": ...}（副本）。persist=false
    时清空并返回空表（不读）。"""
    global _LOADED
    with _LOCK:
        _LOADED = True
        if not _enabled():
            _providers.clear()
            _channels.clear()
            return {"providers": {}, "channels": {}}
        _load_file_locked()
        return {"providers": dict(_providers), "channels": dict(_channels)}


def snapshot() -> dict:
    """内存态副本（观测/测试用，不触盘）。"""
    with _LOCK:
        return {"providers": dict(_providers), "channels": dict(_channels)}


# ── 加载 / 落盘 / 后台线程（affinity 同款）─────────────────

def _ensure_loaded_locked() -> None:
    """懒加载（首次访问时做——与进程启动同效果，且测试
    注入路径后首次调用即读到注入文件）。调用方已持锁。"""
    global _LOADED
    if _LOADED:
        return
    _LOADED = True
    if not _enabled():
        return
    _load_file_locked()


def _load_file_locked() -> None:
    """从 cooldowns.json 载入（调用方已持锁）。文件缺失 → 空表；
    损坏 → 告警 + 空表（不抛——冷却记忆是可丢的加速状态）。
    载入即丢弃过期条目。"""
    path = _cooldowns_file()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        _providers.clear()
        _channels.clear()
        return
    except Exception as e:
        log.warning("cooldowns.json 读取失败（冷却状态按丢失处理）: %s", e)
        _providers.clear()
        _channels.clear()
        return
    _providers.clear()
    _channels.clear()
    if not isinstance(raw, dict):
        return
    now = time.time()
    for key, node, table in (("providers", raw.get("providers"), _providers),
                             ("channels", raw.get("channels"), _channels)):
        if not isinstance(node, dict):
            continue
        for k, v in node.items():
            if not isinstance(k, str):
                continue
            try:
                ts = float(v)
            except (TypeError, ValueError):
                continue
            if ts > now:   # 丢弃过期条目
                table[k] = ts


def _save_to_disk() -> bool:
    """pid 后缀 tmp + os.replace 原子写（affinity._save_to_disk 同款，
    防双实例共写交错）。调用方已持锁。成功 True，失败 False（保 dirty
    留给下一轮重试，仅告警）。"""
    path = _cooldowns_file()
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        payload = {
            "version": _VERSION,
            "saved_at": time.time(),
            "providers": dict(_providers),
            "channels": dict(_channels),
        }
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, str(path))
        return True
    except Exception as e:
        log.warning("cooldowns.json 落盘失败（保留 dirty，下轮重试）: %s", e)
        try:
            os.unlink(tmp)      # 删除写一半的残留
        except Exception:
            pass
        return False


def _sweep_once() -> None:
    """一轮扫尾：脏则原子落盘。**失败保留 dirty 不清**（下轮重试，
    R-004 可见），与 affinity._worker 的"失败也清 dirty"不同。"""
    global _DIRTY
    try:
        with _LOCK:
            if _DIRTY and _save_to_disk():
                _DIRTY = False
    except Exception as e:   # pragma: no cover - 兜底
        log.warning("cooldown_store 扫尾异常: %s", e)


def _worker() -> None:
    """后台扫尾线程（daemon）：≤2s 周期扫尾。全异常吞掉只告警。"""
    while True:
        time.sleep(_SWEEP_S)
        _sweep_once()


def _ensure_worker() -> None:
    """懒启动后台扫尾线程（首次写时——不污染零冷却请求零线程开销的进程）。"""
    global _WORKER_STARTED
    if _WORKER_STARTED:
        return
    with _WORKER_LOCK:
        if _WORKER_STARTED:
            return
        _WORKER_STARTED = True
        threading.Thread(target=_worker, daemon=True,
                         name="cooldown-save").start()
