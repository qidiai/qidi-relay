"""按 key 的 RPM（每分钟请求数）滑动窗口调度器。

为媒体透传路由（routes/media.py）提供统一的 key 选号与限流门：
同一 provider 下多把 key 的并发请求量被限制在
``config.providers.<name>.rpm`` 之内（缺省 0 = 不限制）；
上游 429 时给该 key 记一个冷却窗（``providers.<name>.key_cooldown_s``，
缺省 60 秒），冷却期内 pick 直接跳过该 key。

设计要点
========
- 线程安全：所有读写在单把 ``threading.Lock`` 内完成；
  窗口用 ``collections.deque`` 存时间戳，O(1) 弹出过期项。
- 时间源可注入：``KeyScheduler(now_fn=time.time)`` 构造，
  测试可传 ``lambda: fake_clock`` 做窗口滑动断言，
  也可对同一实例 monkeypatch ``scheduler.now_fn``。
- 模块级单例 ``scheduler``：全局一份，供 ``routes/media.py``
  直接使用；测试可 ``reset()`` 清空状态。
- 键 = ``(provider_name, key_label)``：provider 内不同 key 独立计数，
  跨 provider 同名 label 互不干扰。

RPM 语义
========
- ``rpm = 0`` 或缺省 → 该 provider 不做限流（只记录不拦截）。
- ``rpm = N`` → 滑动窗口（默认 60s）内该 key 最多 N 次 ``record``。
- ``pick`` 跳过已达上限的 key；全满返回 ``None``。
- ``record(ok, status)``：成功/失败都计入窗口计数；
  ``status == 429`` 额外给该 key 记冷却。
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any, Deque, Dict, Optional, Tuple

from .settings import load_config

# 默认参数（config 缺省时使用）
DEFAULT_WINDOW_S = 60.0        # 滑动窗口秒数
DEFAULT_COOLDOWN_S = 60.0      # 429 冷却秒数

_KeyState = Tuple[deque, float]   # (window: deque of timestamps, cooldown_until: float)


class KeyScheduler:
    """线程安全的 per-(provider, key) RPM 滑动窗口调度器。

    Args:
        now_fn: 时间源，缺省 ``time.time``。测试注入 fake clock 做窗口断言。
        window_s: 滑动窗口秒数，缺省 60.
        default_cooldown_s: 429 冷却秒数缺省值，缺省 60（config 可覆盖）。
    """

    def __init__(
        self,
        now_fn: Any = None,
        window_s: float = DEFAULT_WINDOW_S,
        default_cooldown_s: float = DEFAULT_COOLDOWN_S,
    ) -> None:
        self._now_fn = now_fn or time.time
        self._window_s = float(window_s)
        self._default_cooldown_s = float(default_cooldown_s)
        self._lock = threading.Lock()
        # key: (provider_name, key_label) -> _KeyState
        self._states: Dict[Tuple[str, str], _KeyState] = {}

    # ── 公开 API ────────────────────────────────────────────────

    def pick(
        self,
        provider_name: str,
        keys: list,
        now: Optional[float] = None,
    ) -> Optional[dict]:
        """从 keys 列表挑一个未达 RPM 且不在冷却中的 key。

        Args:
            provider_name: provider 名（config.providers 的键）。
            keys: provider 的 key 列表，每个元素是
                ``{"key": ..., "label": ..., ...}``（providers._parse_keys 产物）。
                空列表 / None 返回 None。
            now: 可注入当前时间戳（测试用）；缺省取 ``self._now_fn()``。

        Returns:
            选中的 key dict，或 None（全满 / 全冷却 / 无 key）。
        """
        if not keys:
            return None
        if now is None:
            now = self._now_fn()
        rpm = self._rpm_for(provider_name)
        with self._lock:
            # 先按顺序遍历，第一个"可用"的即返回
            # （"可用" = 不在冷却且窗口未满；rpm=0 时只查冷却）
            for entry in keys:
                label = entry.get("label") or entry.get("key", "")
                state = self._states.get((provider_name, label))
                if state is None:
                    state = (deque(), 0.0)
                    self._states[(provider_name, label)] = state
                window, cooldown_until = state
                # 清理过期时间戳
                if window:
                    cutoff = now - self._window_s
                    while window and window[0] < cutoff:
                        window.popleft()
                # 冷却检查
                if cooldown_until > now:
                    continue
                # RPM 检查（rpm=0 不限）
                if rpm > 0 and len(window) >= rpm:
                    continue
                # 审计修复（2026-10-22）：pick 即**原子预占**一个窗口名额，
                # 否则并发请求会在任何 record 落库前全部选中同一把 key（超发）。
                window.append(now)
                return entry
            return None

    def record(
        self,
        provider_name: str,
        key: dict,
        ok: bool,
        status: Optional[int] = None,
    ) -> None:
        """记录一次 key 使用（成功/失败均计数；429 额外记冷却）。

        Args:
            provider_name: provider 名。
            key: key dict（``{"key": ..., "label": ...}``）。
            ok: 上游是否 2xx。
            status: 上游状态码；``429`` 时给该 key 记冷却。
        """
        label = key.get("label") or key.get("key", "")
        now = self._now_fn()
        with self._lock:
            state = self._states.get((provider_name, label))
            if state is None:
                window: Deque[float] = deque()
                self._states[(provider_name, label)] = (window, 0.0)
                cooldown_until = 0.0
            else:
                window, cooldown_until = state
            # 审计修复：窗口名额已在 pick() 预占，这里只处理 429 冷却，
            # 不再重复 append（否则一次请求会被计两次，RPM 被腰斩）。
            if status == 429:
                cooldown_until = now + self._cooldown_s_for(provider_name)
                self._states[(provider_name, label)] = (window, cooldown_until)

    def stats(self, provider_name: str) -> Dict[str, Dict[str, Any]]:
        """返回该 provider 下所有已记录 key 的统计（供面板用）。

        Returns:
            ``{label: {"used_in_window": int, "cooldown_left": float}}``
            - ``used_in_window``: 滑动窗口内的请求数
            - ``cooldown_left``: 剩余冷却秒数（<=0 = 无冷却）
        """
        now = self._now_fn()
        with self._lock:
            result = {}
            for (pname, label), (window, cooldown_until) in self._states.items():
                if pname != provider_name:
                    continue
                # 清理过期
                cutoff = now - self._window_s
                while window and window[0] < cutoff:
                    window.popleft()
                result[label] = {
                    "used_in_window": len(window),
                    "cooldown_left": max(0.0, cooldown_until - now),
                }
            return result

    def reset(self) -> None:
        """清空所有状态（测试用）。"""
        with self._lock:
            self._states.clear()

    # ── config 读取 ─────────────────────────────────────────────

    def _rpm_for(self, provider_name: str) -> int:
        """从 config 读 providers.<name>.rpm；缺省 / 0 = 不限制。"""
        try:
            cfg = load_config()
        except Exception:
            return 0
        prov = (cfg.get("providers") or {}).get(provider_name)
        if not isinstance(prov, dict):
            return 0
        rpm = prov.get("rpm")
        if isinstance(rpm, bool):
            return 0
        if isinstance(rpm, int) and rpm > 0:
            return rpm
        return 0

    def _cooldown_s_for(self, provider_name: str) -> float:
        """从 config 读 providers.<name>.key_cooldown_s；缺省 DEFAULT_COOLDOWN_S。"""
        try:
            cfg = load_config()
        except Exception:
            return self._default_cooldown_s
        prov = (cfg.get("providers") or {}).get(provider_name)
        if not isinstance(prov, dict):
            return self._default_cooldown_s
        val = prov.get("key_cooldown_s")
        if isinstance(val, (int, float)) and not isinstance(val, bool) and val > 0:
            return float(val)
        return self._default_cooldown_s

    # ── 测试辅助 ────────────────────────────────────────────────

    @property
    def now_fn(self) -> Any:
        return self._now_fn

    @now_fn.setter
    def now_fn(self, fn: Any) -> None:
        self._now_fn = fn


# 模块级单例：routes/media.py 直接使用
scheduler = KeyScheduler()
