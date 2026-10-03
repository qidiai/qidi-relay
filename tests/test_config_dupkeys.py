# -*- coding: utf-8 -*-
"""config 重复键检查（F 小修）回归：大小写折叠后重复的键在
load_config 时被收集进 CONFIG_DUPLICATE_KEY_WARNINGS
（上限 20 条、去重），/health 经 config_warnings 原样透传；
解析行为不变（重复键仍按 json 语义后者覆盖前者）。

运行：py -3.12 -m pytest tests/test_config_dupkeys.py -v
"""

import json
import os
import pathlib
import sys
import tempfile

import pytest

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
os.environ.setdefault("CODELY_CLI_VERSION", "0.0.0-test")

TMP = pathlib.Path(tempfile.mkdtemp(prefix="qidi-relay-dupkeys-"))


def _write_config(cfg):
    (TMP / "config.json").write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


_write_config({
    "port": 0,
    "pool_models": ["GLM-5.3"],
    "model_groups": {},
    "providers": {},
    "roles": {},
    "pricing": {},
})
(TMP / "accounts.json").write_text(
    json.dumps({"accounts": []}), encoding="utf-8")

# 路径重定向（条件化，与 test_stream_sniff.py 同思路——
# am 单例在首次 import proxy_core.state 时按 accounts.py
# 的 ACCOUNTS_FILE 快照加载账号）。
if "proxy_core.state" not in sys.modules:
    import proxy_core.settings as S  # noqa: E402
    import proxy_core.accounts as A  # noqa: E402
    import proxy_core.stats as ST  # noqa: E402
    import proxy_core.water as WA  # noqa: E402
    import proxy_core.providers as PV  # noqa: E402
    S.BASE_DIR = TMP
    S.CONFIG_FILE = TMP / "config.json"
    S.ACCOUNTS_FILE = TMP / "accounts.json"
    S.STATS_FILE = TMP / "token_stats.json"
    S.WATER_FILE = TMP / "water_check.json"
    S.LOG_FILE = TMP / "proxy.log"
    A.ACCOUNTS_FILE = S.ACCOUNTS_FILE
    ST.STATS_FILE = S.STATS_FILE
    WA.WATER_FILE = S.WATER_FILE
    PV.CONFIG_FILE = S.CONFIG_FILE

import proxy_core.settings as S  # noqa: E402
import proxy_core.accounts as A  # noqa: E402
import proxy_core.stats as ST  # noqa: E402
import proxy_core.water as WA  # noqa: E402
import proxy_core.providers as PV  # noqa: E402

import routes.proxy as RP  # noqa: E402

from flask import Flask  # noqa: E402

_app = Flask(__name__)
_app.register_blueprint(RP.bp)
client = _app.test_client()


@pytest.fixture(scope="module", autouse=True)
def _use_dupkeys_config():
    """把磁盘路径切到本文件的临时目录（用例后还原）。

    与 test_stream_sniff.py 的 fixture 同构：全量收集时
    am 等单例可能已由更早的文件实例化，import 时改路径
    对其内存状态无效——fixture 保证本文件用例期间
    load_config()/water 等读的是本文件临时目录。
    """
    saved = (S.BASE_DIR, S.CONFIG_FILE, S.ACCOUNTS_FILE,
             S.STATS_FILE, S.WATER_FILE, S.LOG_FILE,
             A.ACCOUNTS_FILE, ST.STATS_FILE, WA.WATER_FILE,
             PV.CONFIG_FILE)
    S.BASE_DIR = TMP
    S.CONFIG_FILE = TMP / "config.json"
    S.ACCOUNTS_FILE = TMP / "accounts.json"
    S.STATS_FILE = TMP / "token_stats.json"
    S.WATER_FILE = TMP / "water_check.json"
    S.LOG_FILE = TMP / "proxy.log"
    A.ACCOUNTS_FILE = S.ACCOUNTS_FILE
    ST.STATS_FILE = S.STATS_FILE
    WA.WATER_FILE = S.WATER_FILE
    PV.CONFIG_FILE = S.CONFIG_FILE
    PV._cache = None
    yield
    (S.BASE_DIR, S.CONFIG_FILE, S.ACCOUNTS_FILE,
     S.STATS_FILE, S.WATER_FILE, S.LOG_FILE,
     A.ACCOUNTS_FILE, ST.STATS_FILE, WA.WATER_FILE,
     PV.CONFIG_FILE) = saved
    PV._cache = None


# ─── 用例 ─────────────────────────────────────────────────────────

def test_case_duplicate_keys_collected_and_load_succeeds():
    """大小写折叠后重复的键（模拟真实 config.json pricing 节
    :61 vs :101 形态）被收集；load 成功且解析语义不变
   （json 语义：大小写变体是两个独立键，各保留原值；
    仅完全相同的键才后者覆盖前者）。"""
    _write_config({
        "port": 0,
        "pool_models": ["GLM-5.3"],
        "pricing": {
            "deepseek-v4-flash": {"input": 0, "output": 0},
            "DeepSeek-V4-Flash": {"input": 1, "output": 2},
            "GLM-5.3": {"input": 3, "output": 4},
        },
    })
    cfg = S.load_config()
    assert isinstance(cfg, dict)
    # json 语义：大小写变体是两个独立键，各保留原值
    # （这正是告警意义：同一名义模型有两个不同定价条目）
    assert cfg["pricing"]["deepseek-v4-flash"] == {"input": 0, "output": 0}
    assert cfg["pricing"]["DeepSeek-V4-Flash"] == {"input": 1, "output": 2}
    assert cfg["pricing"]["GLM-5.3"] == {"input": 3, "output": 4}
    # 重复键路径被点名（大小写折叠后重复）
    assert any(
        "pricing.deepseek-v4-flash" in w
        and "pricing.DeepSeek-V4-Flash" in w
        for w in S.CONFIG_DUPLICATE_KEY_WARNINGS
    ), S.CONFIG_DUPLICATE_KEY_WARNINGS


def test_no_duplicate_keys_warnings_empty():
    """无重复键 → warnings 空表（缺省形态）。"""
    _write_config({
        "port": 0,
        "pool_models": ["GLM-5.3"],
        "pricing": {"GLM-5.3": {"input": 3, "output": 4}},
    })
    cfg = S.load_config()
    assert isinstance(cfg, dict)
    assert S.CONFIG_DUPLICATE_KEY_WARNINGS == []


def test_health_exposes_config_warnings():
    """/health 新增 config_warnings 键：重复键告警原样透传，
    既有字段一个不动（只增键）。"""
    _write_config({
        "port": 0,
        "pool_models": ["GLM-5.3"],
        "pricing": {
            "deepseek-v4-flash": {"input": 0, "output": 0},
            "DeepSeek-V4-Flash": {"input": 1, "output": 2},
        },
    })
    S.load_config()
    assert S.CONFIG_DUPLICATE_KEY_WARNINGS
    r = client.get("/health")
    assert r.status_code == 200
    body = r.get_json()
    # 既有字段一个不动，只增 config_warnings
    for key in ("status", "version", "accounts", "accounts_enabled",
                "endpoint", "uptime_seconds", "time"):
        assert key in body, key
    assert isinstance(body["config_warnings"], list)
    assert len(body["config_warnings"]) == len(S.CONFIG_DUPLICATE_KEY_WARNINGS)
    assert any("DeepSeek-V4-Flash" in w for w in body["config_warnings"])
    # 无告警时缺省空表
    _write_config({"port": 0, "pool_models": ["GLM-5.3"], "pricing": {}})
    S.load_config()
    assert S.CONFIG_DUPLICATE_KEY_WARNINGS == []
    r = client.get("/health")
    assert r.get_json()["config_warnings"] == []


def test_warnings_capped_at_twenty_and_deduplicated():
    """告警上限 20 条（防坏配置灌爆日志/面板），且去重。"""
    pricing = {}
    for i in range(25):
        pricing[f"m{i}"] = {"input": i}
        pricing[f"M{i}"] = {"input": i + 100}
    _write_config({"port": 0, "pool_models": ["GLM-5.3"], "pricing": pricing})
    cfg = S.load_config()
    assert isinstance(cfg, dict)
    assert 0 < len(S.CONFIG_DUPLICATE_KEY_WARNINGS) <= 20
    # 去重：同一条目不出现两次
    assert len(set(S.CONFIG_DUPLICATE_KEY_WARNINGS)) \
        == len(S.CONFIG_DUPLICATE_KEY_WARNINGS)
