"""会话粘性（阶段 B / 方案 v2 §4，v1 仅主池）。

作用域（§4 B-2 裁定）：只覆盖主账号池选号路径
（accounts.AccountManager.get_next 的 prefer 注入，
routes/proxy._proxy_chat 接线）。cline/minicode/
external 三外部通道各有独立 pick，列二期。

设计要点（§4 双审修正）：
- 会话键四级 fallback（B-5/B-6）：chat["user"]
  （Anthropic metadata.user_id 已被
  proxy_core/anthropic.py:163-165 译入）→
  请求头 X-Session-Id → 首条 user 消息前 512B
  sha256[:16]（绝不用 system：_proxy_chat 把
  system 替换成固定串，system 哈希跨会话恒定）→
  key_label / remote_addr 兜底；
- turn 判定双协议：Anthropic 形 messages[-1] 含
  tool_result 块 / OpenAI chat 形 messages[-1]
  .role=="tool"（经 anthropic_to_chat 翻译后
  tool_result 已是 role=="tool"，两条入口同判）；
- 粘性策略 auto 档：turn 内必粘；跨 turn 仅当
  last cache_read≥1024 且距今<300s。cache_read
  从 usage 提取 cache_read_input_tokens（Anthropic
  形）或 prompt_tokens_details.cached_tokens
  （OpenAI 形），都没有 → cache_read=0 且标记
  无数据（跨 turn 判 no-data，§4 B-3）；
- 存储（§4 B-4）：热路径纯内存查表（0 IO）；
  后台线程 ≤2s 周期扫尾，脏时经 tmp+rename 原子
  落盘 conversations.json（不学 stats._save 的
  非原子全量重写）；启动（首次访问）与 mtime 变化
  时重读，兼容双实例并存（last-write-wins 只丢
  粘性记忆，良性——方案 §10 风险表）；512 条 LRU、
  24h 过期（magpie affinity.go:38-48 同常数，
  方案转述经核验准确）。

why 码（§4 affinity.go 语义全集）：
kept/turn/session/cache/new-turn/no-cache/no-data/
cold/resting/spent/disabled/off——v1 auto 档产出
turn/cache/no-cache/no-data/cold/resting/spent/
disabled；kept/session/new-turn/off 为策略档保留码
（off 在直接调用 pick_prefer 且开关关闭时产出）。
"""

import hashlib
import json
import logging
import os
import threading
import time
from collections import OrderedDict

from proxy_core import settings
from proxy_core.settings import load_config
from proxy_core.state import am

log = logging.getLogger("qidi-relay")

# ── 常数（方案 §4：magpie affinity.go:38-48 同常数）──
_MAX_SESSIONS = 512          # LRU 容量
_SESSION_TTL_S = 24 * 3600   # 24h 过期
_CACHE_READ_MIN = 1024       # 跨 turn 粘性最低 cache_read
_STICK_WINDOW_S = 300        # 跨 turn 粘性时间窗（5min）
_SWEEP_S = 2.0               # 后台扫尾间隔（≤2s）

# why 码（见模块 docstring）
WHY_KEPT = "kept"
WHY_TURN = "turn"
WHY_SESSION = "session"
WHY_CACHE = "cache"
WHY_NEW_TURN = "new-turn"
WHY_NO_CACHE = "no-cache"
WHY_NO_DATA = "no-data"
WHY_COLD = "cold"
WHY_RESTING = "resting"
WHY_SPENT = "spent"
WHY_DISABLED = "disabled"
WHY_OFF = "off"

# 内存表：session_key → {"uid", "model", "turn", "at",
# "cache_read", "cache_data"}。cache_data=False 标记
# 上游没回 cache 字段——cache_read=0 是"无数据"而非
# "真零"，跨 turn 判 no-data（§4 B-3）。
_TABLE: "OrderedDict[str, dict]" = OrderedDict()
# pick_prefer 决策 why 计数（/api/affinity/stats 分布）
_WHY: dict = {}
_LOCK = threading.Lock()
_DIRTY = False      # 有未落盘改动
_LOADED = False     # 启动重读已做（首次访问时）
_LAST_MTIME = None  # conversations.json 上次见到的 mtime_ns
_WORKER_STARTED = False
_WORKER_LOCK = threading.Lock()


def _conversations_file():
    """路径常量走 settings（可注入）：每次取现，
    测试改 settings.CONVERSATIONS_FILE 即生效
    （方案 §7.7 纪律：禁止 seedguard 式 BASE_DIR
    兜底直写仓库数据文件）。"""
    return settings.CONVERSATIONS_FILE


# ── 会话键（§4 B-5/B-6 四级 fallback）──────────────────

def session_key(data: dict, headers, key_label: str | None,
                remote_addr: str | None) -> str:
    """计算会话键。四级 fallback：
    1. chat["user"]（Anthropic metadata.user_id 已被
       anthropic.anthropic_to_chat 译入）
    2. 请求头 X-Session-Id
    3. 首条 user 消息前 512B sha256[:16]（不含
       system——_proxy_chat 把 system 替换成固定串，
       system 哈希跨会话恒定、无区分度）
    4. 兜底：客户端 key 名（label）/ 来源 IP
    """
    user = data.get("user")
    if isinstance(user, str) and user:
        return "u:" + user
    sid = headers.get("X-Session-Id") if headers is not None else None
    if isinstance(sid, str) and sid:
        return "h:" + sid
    for m in data.get("messages") or []:
        if not isinstance(m, dict) or m.get("role") != "user":
            continue
        content = m.get("content")
        if isinstance(content, str):
            text = content
        elif content is not None:
            # 多模态 content 是块列表：序列化后取哈希
            #（键序排序消除抖动，与纯文本消息区分）
            text = json.dumps(content, ensure_ascii=False,
                              sort_keys=True)
        else:
            continue
        if not text:
            continue
        digest = hashlib.sha256(
            text.encode("utf-8", errors="ignore")[:512]
        ).hexdigest()[:16]
        return "m:" + digest
    if key_label:
        return "k:" + str(key_label)
    if remote_addr:
        return "r:" + str(remote_addr)
    return "anon"


# ── turn 判定（§4 双协议）───────────────────────────────

def is_turn_continuation(data: dict) -> bool:
    """turn 判定：最后一条消息是工具回传 → 同一
    turn 的工具循环尚未结束（与上一请求同 turn）。

    双协议：OpenAI chat 形 messages[-1].role=="tool"；
    Anthropic 形 messages[-1] 的 content 块含
    tool_result。注意 /v1/messages 入口的 chat_data
    已经过 anthropic_to_chat 翻译（tool_result →
    role=="tool"），两种原始协议在此同判。
    """
    msgs = data.get("messages")
    if not isinstance(msgs, list) or not msgs:
        return False
    last = msgs[-1]
    if not isinstance(last, dict):
        return False
    if last.get("role") == "tool":
        return True
    content = last.get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                return True
    return False


# ── 粘性决策（auto 档）──────────────────────────────────

def _account_state(uid: str) -> tuple:
    """查账号当前状态（只读，不改任何状态）。

    返回 (why, reason)：可粘 → (WHY_KEPT, "")；
    不可粘 → (WHY_DISABLED/WHY_SPENT/WHY_RESTING/
    WHY_COLD, 原因)。冷却判定与
    accounts._clear_expired_cooldowns 同口径：
    budget_exceeded 且未到期才算冷却中（无时间戳
    的旧数据视为已到期）；402 与普通冷却按
    cooldown_reason 区分（mark_budget_402 写
    "配额耗尽（402 第N次）"）。
    """
    for a in am.accounts:
        if a["user_id"] != uid:
            continue
        if not a.get("enabled", True):
            return WHY_DISABLED, "账号已禁用"
        if a.get("budget_exceeded"):
            until = a.get("budget_cooldown_until") or 0
            if time.time() < until:
                reason = a.get("cooldown_reason") or ""
                if "402" in reason:
                    return WHY_SPENT, reason
                return WHY_RESTING, reason
        return WHY_KEPT, ""
    return WHY_COLD, "账号不在池中"


def pick_prefer(session_key: str,
                turn_boundary_changed: bool = True) -> tuple:
    """auto 档粘性决策，返回 (prefer_uid, why)。

    prefer_uid=None 表示不粘（走账号轮换）。
    turn_boundary_changed=True（本请求是新 turn）
    时走跨 turn 规则；False（工具循环内的续请求）
    必粘。缺省 True——保守：未知时不凭记忆强粘
    （调用方 _proxy_chat 在请求层算好传入）。
    """
    if not load_config().get("features", {}).get("affinity", True):
        _bump_why(WHY_OFF)
        return None, WHY_OFF
    with _LOCK:
        _ensure_loaded_locked()
        entry = _TABLE.get(session_key)
        if entry is None:
            _bump_why(WHY_COLD)
            return None, WHY_COLD
        _TABLE.move_to_end(session_key)   # LRU 触摸
        now = time.time()
        if now - entry["at"] > _SESSION_TTL_S:
            # 24h 过期：视为无记忆
            _bump_why(WHY_COLD)
            return None, WHY_COLD
        uid = entry["uid"]
        state, _reason = _account_state(uid)
        if state != WHY_KEPT:
            # 账号冷却/配额耗尽/禁用 → 不粘（误判
            # 慢账号不能把它冷却掉，这里只是不选它）
            _bump_why(state)
            return None, state
        if not turn_boundary_changed:
            # turn 内（工具循环）：必粘
            _bump_why(WHY_TURN)
            return uid, WHY_TURN
        # 跨 turn：仅当 last cache_read≥1024 且距今<300s
        if not entry.get("cache_data"):
            _bump_why(WHY_NO_DATA)
            return None, WHY_NO_DATA
        if entry["cache_read"] < _CACHE_READ_MIN:
            _bump_why(WHY_NO_CACHE)
            return None, WHY_NO_CACHE
        if now - entry["at"] > _STICK_WINDOW_S:
            _bump_why(WHY_COLD)
            return None, WHY_COLD
        _bump_why(WHY_CACHE)
        return uid, WHY_CACHE


# ── 记录（成功服务后更新内存表）─────────────────────────

def _extract_cache_read(usage) -> tuple:
    """cache_read 提取（§4 B-3）：Anthropic 形
    cache_read_input_tokens / OpenAI 形
    prompt_tokens_details.cached_tokens；都没有 →
    (0, False)（"无数据"标记，跨 turn 判 no-data）。"""
    if not isinstance(usage, dict):
        return 0, False
    cr = usage.get("cache_read_input_tokens")
    if isinstance(cr, (int, float)) and cr >= 0:
        return int(cr), True
    details = usage.get("prompt_tokens_details")
    if isinstance(details, dict):
        cached = details.get("cached_tokens")
        if isinstance(cached, (int, float)) and cached >= 0:
            return int(cached), True
    return 0, False


def record(session_key: str, uid: str, model: str, usage,
           turn_boundary_changed: bool = True) -> None:
    """记录本次成功服务的账号（§4）：热路径只写
    内存（0 IO），后台线程 ≤2s 内原子落盘。

    turn_boundary_changed=True（本请求是新 turn）
    → 内存表 turn 计数 +1；工具循环内的续请求
    不计数。turn 缺省 True（保守同 pick_prefer）。
    """
    global _DIRTY
    if turn_boundary_changed is None:
        turn_boundary_changed = True
    cache_read, has_cache = _extract_cache_read(usage)
    with _LOCK:
        _ensure_loaded_locked()
        entry = _TABLE.get(session_key)
        turn = 1 if entry is None else \
            entry["turn"] + (1 if turn_boundary_changed else 0)
        _TABLE[session_key] = {
            "uid": uid, "model": model, "turn": turn,
            "at": time.time(), "cache_read": cache_read,
            "cache_data": has_cache,
        }
        _TABLE.move_to_end(session_key)
        _evict_locked()
        _DIRTY = True
    _ensure_worker()


# ── 观测（/api/affinity/stats）──────────────────────────

def snapshot() -> tuple:
    """锁内快照：(stuck_sessions, why_distribution)。
    stuck_sessions = 当前粘连会话数（24h 过期条目
    惰性清出后再计数）。"""
    global _DIRTY
    with _LOCK:
        _ensure_loaded_locked()
        now = time.time()
        stale = [k for k, e in _TABLE.items()
                 if now - e["at"] > _SESSION_TTL_S]
        for k in stale:
            _TABLE.pop(k, None)
        if stale:
            _DIRTY = True
        return len(_TABLE), dict(_WHY)


# ── 内存表维护（LRU / 后台线程 / 原子落盘）──────────────

def _bump_why(why: str) -> None:
    """why 计数 +1（调用方已持锁）。"""
    _WHY[why] = _WHY.get(why, 0) + 1


def _evict_locked() -> None:
    """LRU 容量驱逐（调用方已持锁）：超 512 条逐出
    最久未访问。"""
    while len(_TABLE) > _MAX_SESSIONS:
        _TABLE.popitem(last=False)


def _ensure_loaded_locked() -> None:
    """启动重读（首次访问时做——与进程启动同效果，
    且测试注入路径后首次调用即读到注入文件）。"""
    global _LOADED
    if _LOADED:
        return
    _LOADED = True
    _load_file_locked()


def _load_file_locked() -> bool:
    """从 conversations.json 载入（调用方已持锁）。
    文件缺失 → 空表；损坏 → 告警 + 空表（不抛，
    粘性记忆是良性可丢状态）。"""
    global _LAST_MTIME
    path = _conversations_file()
    try:
        _LAST_MTIME = path.stat().st_mtime_ns
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        _LAST_MTIME = None
        return False
    except Exception as e:
        log.warning("conversations.json 读取失败"
                    "（粘性记忆按丢失处理）: %s", e)
        return False
    _TABLE.clear()
    sessions = raw.get("sessions") if isinstance(raw, dict) else None
    if not isinstance(sessions, dict):
        return False
    now = time.time()
    for k, v in sessions.items():
        if not (isinstance(k, str) and isinstance(v, dict)
                and isinstance(v.get("uid"), str)):
            continue
        at = float(v.get("at") or now)
        if now - at > _SESSION_TTL_S:
            continue   # 载入即清出 24h 过期条目
        _TABLE[k] = {
            "uid": v["uid"],
            "model": v.get("model") or "",
            "turn": int(v.get("turn") or 0),
            "at": at,
            "cache_read": int(v.get("cache_read") or 0),
            "cache_data": bool(v.get("cache_data")),
        }
    return True


def _save_to_disk() -> None:
    """tmp+rename 原子写（§4 B-4：不学 stats._save
    的非原子全量重写）。调用方已持锁。

    tmp 名带本进程 pid——双实例并存时不再共写同一 tmp
    （各写各的 <file>.<pid>.tmp，os.replace 原子替换主
    文件）；写一半崩溃的残留 tmp 在异常路径删除，
    主文件永不受损。"""
    global _LAST_MTIME
    path = _conversations_file()
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"sessions": {k: _TABLE[k] for k in _TABLE}},
                      f, ensure_ascii=False)
        os.replace(tmp, str(path))
        try:
            _LAST_MTIME = path.stat().st_mtime_ns
        except Exception:
            pass
    except Exception as e:
        log.warning("conversations.json 落盘失败: %s", e)
        try:
            os.unlink(tmp)      # 删除写一半的残留
        except Exception:
            pass


def _reload_if_changed_locked() -> None:
    """mtime 变化时重读（双实例并存兼容，§4 B-4）。
    调用方已持锁。last-write-wins：重读会丢弃本
    进程未落盘的改动（只丢粘性记忆，良性——方案
    §10 风险表）。"""
    global _LAST_MTIME
    path = _conversations_file()
    try:
        mtime = path.stat().st_mtime_ns
    except FileNotFoundError:
        return
    except Exception:
        return
    if mtime == _LAST_MTIME:
        return
    _LAST_MTIME = mtime
    _load_file_locked()


def _worker() -> None:
    """后台扫尾线程（daemon）：≤2s 周期——脏则原子
    落盘；mtime 变化则重读。全异常吞掉只告警
    （采集器/扫尾线程拖垮主链路是方案 §10 风险）。"""
    global _DIRTY
    while True:
        time.sleep(_SWEEP_S)
        try:
            with _LOCK:
                if _DIRTY:
                    _save_to_disk()
                    _DIRTY = False
                _reload_if_changed_locked()
        except Exception as e:   # pragma: no cover - 线程兜底
            log.warning("affinity 后台线程异常: %s", e)


def _ensure_worker() -> None:
    """懒启动后台扫尾线程（首次 record 时——不污染
    零粘性请求零线程开销的进程）。"""
    global _WORKER_STARTED
    if _WORKER_STARTED:
        return
    with _WORKER_LOCK:
        if _WORKER_STARTED:
            return
        _WORKER_STARTED = True
        threading.Thread(target=_worker, daemon=True,
                         name="affinity-save").start()
