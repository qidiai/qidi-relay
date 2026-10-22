# -*- coding: utf-8 -*-
"""proxy_core.key_scheduler 单元测试（零网络、零磁盘）。

覆盖规格 5 条：
1. 未达 RPM 时能连续 pick 到同一 key；达到上限后 pick 返回 None
2. 窗口滑动后（注入 now）重新可用
3. 429 → 冷却期内被跳过，冷却结束恢复
4. 多 key 轮流（pick 应返回不同 key，直到都满）
5. 并发 pick 不超发（20 线程，rpm=10，成功 pick 数 == 10）

跑法：py -3.12 -m pytest tests/test_key_scheduler.py -v
"""

import json
import pathlib
import sys
import tempfile
import threading

import pytest

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

# ─── 路径重定向（必须在 import proxy_core.settings 之前）──────────
_TMP = pathlib.Path(tempfile.mkdtemp(prefix="qidi-key-sched-test-"))
(_TMP / "config.json").write_text(json.dumps({"providers": {}}), encoding="utf-8")
(_TMP / "accounts.json").write_text(json.dumps({"accounts": []}), encoding="utf-8")

import proxy_core.settings as _S  # noqa: E402

_S.BASE_DIR = _TMP
_S.CONFIG_FILE = _TMP / "config.json"
_S.ACCOUNTS_FILE = _TMP / "accounts.json"
_S.STATS_FILE = _TMP / "token_stats.json"
_S.WATER_FILE = _TMP / "water_check.json"
_S.LOG_FILE = _TMP / "proxy.log"

from proxy_core.key_scheduler import KeyScheduler  # noqa: E402


def _make_keys(n: int = 3) -> list:
    """构造 providers._parse_keys 产物形态的 key 列表。"""
    return [{"key": f"sk-{i}", "label": f"k{i}"} for i in range(n)]


class _FakeClock:
    """可注入的 fake clock。"""
    def __init__(self, start: float = 1000.0):
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


@pytest.fixture()
def clock():
    return _FakeClock()


@pytest.fixture()
def sched(clock, monkeypatch, tmp_path):
    """带 fake clock 的调度器，config 指向 tmp config（rpm=5, cooldown=60）。"""
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({
        "providers": {
            "testprov": {"rpm": 5, "key_cooldown_s": 60},
        }
    }), encoding="utf-8")
    # 让 load_config 读这个文件
    import proxy_core.settings as S
    monkeypatch.setattr(S, "CONFIG_FILE", cfg_file)
    s = KeyScheduler(now_fn=clock)
    yield s


def test_pick_same_key_until_rpm_reached(sched, clock):
    """规格 1：未达 RPM 时连续 pick 到同一 key；达到上限后 None。"""
    keys = _make_keys(1)  # 只有一把 key
    clock.advance(0)  # now = 1000
    for _ in range(5):
        picked = sched.pick("testprov", keys, now=clock())
        assert picked is not None, "RPM=5 内应能 pick 到"
        sched.record("testprov", picked, ok=True)
    # 第 6 次：窗口内已 5 条，rpm=5 → 拒绝
    assert sched.pick("testprov", keys, now=clock()) is None


def test_window_slide_releases_keys(sched, clock):
    """规格 2：窗口滑动后（注入 now）重新可用。

    语义修正（2026-10-22 审计）：名额由 ``pick`` 原子预占，因此这里必须
    用 pick 占满窗口；只用 record 不再计入（否则并发会超发）。
    """
    keys = _make_keys(1)
    for _ in range(5):
        assert sched.pick("testprov", keys, now=clock()) is not None
    assert sched.pick("testprov", keys, now=clock()) is None
    # 滑动窗口 60s：推进 61 秒
    clock.advance(61)
    picked = sched.pick("testprov", keys, now=clock())
    assert picked is not None, "窗口滑动后应重新可用"


def test_429_triggers_cooldown_then_recovers(sched, clock):
    """规格 3：429 → 冷却期内被跳过，冷却结束恢复。"""
    keys = _make_keys(1)
    sched.record("testprov", keys[0], ok=False, status=429)
    # 冷却期（60s）内 pick 返回 None（即使窗口未满）
    assert sched.pick("testprov", keys, now=clock()) is None
    # stats 应反映 cooldown
    st = sched.stats("testprov")
    assert st["k0"]["cooldown_left"] > 0
    # 冷却结束（推进 61 秒）
    clock.advance(61)
    picked = sched.pick("testprov", keys, now=clock())
    assert picked is not None


def test_multi_key_rotation(sched, clock):
    """规格 4：多 key 轮流（pick 返回不同 key，直到都满）。"""
    keys = _make_keys(3)  # k0, k1, k2
    # rpm=5，3 把 key 各有 5 次配额
    picked_labels = []
    for _ in range(15):  # 3 keys × rpm 5
        p = sched.pick("testprov", keys, now=clock())
        if p is None:
            break
        picked_labels.append(p["label"])
        sched.record("testprov", p, ok=True)
    # 每把 key 最多 5 次
    from collections import Counter
    counts = Counter(picked_labels)
    for label in ("k0", "k1", "k2"):
        assert counts.get(label, 0) <= 5, f"{label} 超过 rpm=5"
    # 全部用完后 pick 返回 None
    assert sched.pick("testprov", keys, now=clock()) is None


def test_concurrent_pick_no_overshoot(sched, clock, monkeypatch):
    """规格 5：并发 pick 不超发（20 线程，rpm=10，成功 pick 数 == 10）。"""
    import json as _json
    import pathlib as _pl
    import tempfile as _tf
    import proxy_core.settings as S

    # 临时 config（rpm=10），patch S.CONFIG_FILE 让 load_config 读它
    tmp_dir = _pl.Path(_tf.mkdtemp(prefix="qidi-kc-"))
    cf = tmp_dir / "config.json"
    cf.write_text(_json.dumps({"providers": {"testprov": {"rpm": 10}}}),
                  encoding="utf-8")
    monkeypatch.setattr(S, "CONFIG_FILE", cf)

    s2 = KeyScheduler(now_fn=clock)
    keys = _make_keys(1)
    results = []
    lock = threading.Lock()

    def worker():
        p = s2.pick("testprov", keys, now=clock())
        with lock:
            results.append(p)
        # 模拟请求：记录成功
        if p is not None:
            s2.record("testprov", p, ok=True)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    successful = [r for r in results if r is not None]
    assert len(successful) == 10, f"期望 10 次成功 pick，实际 {len(successful)}"


def test_pick_respects_cooldown_across_keys(sched, clock):
    """补充：一把 key 429 冷却中，pick 跳过它选下一把。"""
    keys = _make_keys(2)  # k0, k1
    # k0 429 冷却
    sched.record("testprov", keys[0], ok=False, status=429)
    picked = sched.pick("testprov", keys, now=clock())
    assert picked is not None
    assert picked["label"] == "k1", "k0 冷却中应跳过，pick k1"


def test_pick_only_concurrency_never_overshoots(clock, monkeypatch):
    """审计补充：**只 pick 不 record** 的并发路径也不得超发。

    原实现只在 record 里计数，20 个并发 pick 会在任何 record 之前全部
    选中同一把 key（RPM 打穿）。修复后 pick 原子预占，成功数必须恰好 == rpm。
    """
    import json as _json, pathlib as _pl, tempfile as _tf
    import proxy_core.settings as S

    d = _pl.Path(_tf.mkdtemp(prefix="qidi-kc2-"))
    cf = d / "config.json"
    cf.write_text(_json.dumps({"providers": {"testprov": {"rpm": 10}}}), encoding="utf-8")
    monkeypatch.setattr(S, "CONFIG_FILE", cf)

    s = KeyScheduler(now_fn=clock)
    keys = _make_keys(1)
    ok = []
    lock = threading.Lock()

    def worker():
        p = s.pick("testprov", keys, now=clock())
        if p is not None:
            with lock:
                ok.append(p)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    assert len(ok) == 10, "并发 pick 超发：%d > rpm=10" % len(ok)
