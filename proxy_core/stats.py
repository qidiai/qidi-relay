"""Token 用量统计（含小时级历史桶）与实时活动日志。

token_stats.json 兼容 v3 格式：顶层是 "user_id|model" -> 计数 的平铺字典，
v4 在此基础上增加 "__history__" 键存放小时桶，读旧文件不受影响。
"""

import json
import logging
import threading
from collections import deque
from datetime import datetime, timedelta

from .settings import STATS_FILE

log = logging.getLogger("qidi-relay")

HISTORY_KEY = "__history__"
HISTORY_KEEP_HOURS = 24 * 7  # 历史桶保留 7 天
KEY_STATS_KEY = "__keys__"   # 按 API key 的用量（label -> {date, total_tokens, requests, models:{...}}）


def _blank_entry():
    return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "requests": 0}


def _cache_read_tokens(usage: dict) -> int:
    """从上游 usage 字典提取缓存读 token 数（多协议兼容：
    OpenAI prompt_tokens_details.cached_tokens /
    Anthropic cache_read_input_tokens）。

    双字段并存时取 max() 而非求和（R5）：两字段是
    同一批缓存读 token 的两种协议表达，求和会把
    同一批重复计（上游不会同时用两字段报两批
    不同的缓存读）。"""
    if not isinstance(usage, dict):
        return 0
    parts = []
    details = usage.get("prompt_tokens_details")
    if isinstance(details, dict):
        try:
            parts.append(int(details.get("cached_tokens", 0) or 0))
        except (TypeError, ValueError):
            pass
    try:
        parts.append(int(usage.get("cache_read_input_tokens", 0) or 0))
    except (TypeError, ValueError):
        pass
    return max(parts) if parts else 0


class TokenStats:
    """统计各账号、各模型的 token 用量，持久化到 token_stats.json。"""

    def __init__(self):
        self.stats = {}     # "user_id|model" -> 计数
        self.history = {}   # "YYYY-MM-DDTHH" -> 计数
        self.key_stats = {} # API key label -> 当日用量 {date, total_tokens, requests, models}
        self._lock = threading.Lock()
        self._load()

    def _load(self):
        if not STATS_FILE.exists():
            return
        try:
            with open(STATS_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
            self.history = raw.pop(HISTORY_KEY, {}) if isinstance(raw, dict) else {}
            self.key_stats = raw.pop(KEY_STATS_KEY, {}) if isinstance(raw, dict) else {}
            self.stats = raw if isinstance(raw, dict) else {}
            self._rollover_key_stats()
        except Exception as e:
            log.error("读取 token_stats.json 失败: %s", e)

    def _save(self):
        try:
            data = dict(self.stats)
            data[HISTORY_KEY] = self.history
            data[KEY_STATS_KEY] = self.key_stats
            with open(STATS_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            log.error("保存 token_stats.json 失败: %s", e)

    # ─── 按 API key 的用量（朋友共享场景） ──────────────────

    def _rollover_key_stats(self):
        """跨天自动清零（读取与记账时都会调，保证旧日期数据不残留）。"""
        today = datetime.now().strftime("%Y-%m-%d")
        for label, e in list(self.key_stats.items()):
            if e.get("date") != today:
                # 实际行为：旧日期条目立即清零为当日空条目
                # （不留历史——"保留 7 天"是注释漂移，代码
                # 从未实现过历史保留；R5 如实改正）
                self.key_stats[label] = self._blank_key_entry(today)

    def _blank_key_entry(self, date: str) -> dict:
        return {"date": date, "total_tokens": 0, "requests": 0,
                "models": {}, "cache_tokens": 0}

    def record_key(self, label: str, model: str, usage: dict):
        """按 API key 标签记录一次请求用量（跨天自动清零）。

        cache_tokens 单独累计（客户端预算 cache_reads=false
        时不计入预算；total_tokens 口径不变——旧限额检查与
        面板展示不受影响）。
        """
        if not usage or not label:
            return
        with self._lock:
            today = datetime.now().strftime("%Y-%m-%d")
            e = self.key_stats.get(label)
            if not e or e.get("date") != today:
                e = self._blank_key_entry(today)
                self.key_stats[label] = e
            e["total_tokens"] += usage.get("total_tokens", 0)
            e["requests"] += 1
            m = e["models"].get(model, 0)
            e["models"][model] = m + usage.get("total_tokens", 0)
            e["cache_tokens"] = (e.get("cache_tokens", 0)
                                  + _cache_read_tokens(usage))
            self._save()

    def key_exceeded(self, label: str, limit: int) -> bool:
        """该 key 今日用量是否已达限额（请求前检查）。"""
        with self._lock:
            today = datetime.now().strftime("%Y-%m-%d")
            e = self.key_stats.get(label)
            return bool(e and e.get("date") == today and e.get("total_tokens", 0) >= limit)

    def get_key_stats(self) -> list:
        """各 key 今日用量（面板展示）。"""
        with self._lock:
            self._rollover_key_stats()
            out = []
            for label, e in self.key_stats.items():
                out.append({
                    "label": label,
                    "date": e.get("date", ""),
                    "total_tokens": e.get("total_tokens", 0),
                    "requests": e.get("requests", 0),
                    "models": dict(e.get("models", {})),
                    "cache_tokens": e.get("cache_tokens", 0),
                })
            out.sort(key=lambda x: x["total_tokens"], reverse=True)
            return out

    def record(self, user_id: str, model: str, usage: dict):
        """记录一次请求的 token 用量（累计 + 小时桶）。"""
        if not usage:
            return
        with self._lock:
            key = f"{user_id}|{model}"
            entry = self.stats.get(key, _blank_entry())
            entry["prompt_tokens"] += usage.get("prompt_tokens", 0)
            entry["completion_tokens"] += usage.get("completion_tokens", 0)
            entry["total_tokens"] += usage.get("total_tokens", 0)
            entry["requests"] += 1
            self.stats[key] = entry

            bucket = datetime.now().strftime("%Y-%m-%dT%H")
            h = self.history.get(bucket, _blank_entry())
            h["prompt_tokens"] += usage.get("prompt_tokens", 0)
            h["completion_tokens"] += usage.get("completion_tokens", 0)
            h["total_tokens"] += usage.get("total_tokens", 0)
            h["requests"] += 1
            self.history[bucket] = h
            self._prune_history()
            self._save()

    def _prune_history(self):
        cutoff = (datetime.now() - timedelta(hours=HISTORY_KEEP_HOURS)).strftime("%Y-%m-%dT%H")
        for b in [b for b in self.history if b < cutoff]:
            del self.history[b]

    def get_all(self):
        """按总 token 降序返回明细列表。"""
        result = []
        for key, entry in self.stats.items():
            parts = key.split("|", 1)
            result.append({
                "user_id": parts[0] if parts else "",
                "model": parts[1] if len(parts) > 1 else "",
                "prompt_tokens": entry.get("prompt_tokens", 0),
                "completion_tokens": entry.get("completion_tokens", 0),
                "total_tokens": entry.get("total_tokens", 0),
                "requests": entry.get("requests", 0),
            })
        result.sort(key=lambda x: x["total_tokens"], reverse=True)
        return result

    def get_summary(self):
        return {
            "prompt_tokens": sum(e.get("prompt_tokens", 0) for e in self.stats.values()),
            "completion_tokens": sum(e.get("completion_tokens", 0) for e in self.stats.values()),
            "total_tokens": sum(e.get("total_tokens", 0) for e in self.stats.values()),
            "requests": sum(e.get("requests", 0) for e in self.stats.values()),
        }

    def get_history(self, hours: int = 24):
        """返回最近 N 小时的零填充时间序列，供趋势图使用。"""
        hours = max(1, min(hours, HISTORY_KEEP_HOURS))
        now = datetime.now().replace(minute=0, second=0, microsecond=0)
        out = []
        for i in range(hours - 1, -1, -1):
            t = now - timedelta(hours=i)
            bucket = t.strftime("%Y-%m-%dT%H")
            h = self.history.get(bucket, _blank_entry())
            out.append({
                "bucket": bucket,
                "label": t.strftime("%H:%M") if hours <= 48 else t.strftime("%m-%d %H时"),
                "prompt_tokens": h.get("prompt_tokens", 0),
                "completion_tokens": h.get("completion_tokens", 0),
                "total_tokens": h.get("total_tokens", 0),
                "requests": h.get("requests", 0),
            })
        return out

    def reset(self):
        with self._lock:
            self.stats = {}
            self.history = {}
            self._save()


class ActivityLog:
    """内存环形缓冲：最近的代理请求动态（不落盘，重启即清）。"""

    def __init__(self, maxlen: int = 200):
        self._events = deque(maxlen=maxlen)
        self._seq = 0
        self._lock = threading.Lock()

    def add(self, kind: str, message: str, **fields):
        with self._lock:
            self._seq += 1
            self._events.appendleft({
                "seq": self._seq,
                "time": datetime.now().strftime("%H:%M:%S"),
                "kind": kind,  # request / ok / error / info
                "message": message,
                **fields,
            })

    def list(self, limit: int = 100):
        with self._lock:
            return list(self._events)[:limit]
