# -*- coding: utf-8 -*-
"""凭证权重（吸收-2 / 规格 §R-E）测试。

覆盖规格 §R-E 测试清单：
1. weight 缺省=1 → 与现状逐位一致（含 tiebreak 轮转；len==1 早退在过滤后可用集上）
2. weight=0 不参与；disabled 优先于 weight=0；单 key 且 weight=0 → None
3. 同负载下 weight=2 先于 weight=1；越界/字符串容错（钳制时发 warning）
4. accounts 侧：weight 透传（_load 白名单，防 _save 回写抹除）+ 面板展示；
   不参与 get_next
5. _parse_keys 对 weight 的白名单透传单测

零网络：pick_key 的负载来自全局 registry，直接 monkeypatch load_of。

跑法：python -m pytest tests/test_key_weight.py -q
"""

import json
import logging
import pathlib
import sys

import pytest

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import proxy_core.providers as PV          # noqa: E402
import proxy_core.accounts as A            # noqa: E402
from proxy_core.router import registry     # noqa: E402


def _prov(entries, name="tp"):
    return {"name": name, "keys": entries}


def _loads(mapping):
    """load_of 替身：key 形如 "key:<prov>|<label>"，取 label 查表。"""
    return lambda k: mapping.get(k.rsplit("|", 1)[-1], 0)


# ─── 1. 缺省权重 = 现状逐位一致 ─────────────────────────────────────

def test_default_weight_matches_baseline_rotation(monkeypatch):
    """缺省 weight=1：同负载保留 _key_rr 轮转起点，逐位复现旧行为。"""
    monkeypatch.setattr(registry, "load_of", _loads({"a": 0, "b": 0, "c": 0}))
    PV._key_rr.clear()
    p = _prov([{"key": "kA", "label": "a"},
               {"key": "kB", "label": "b"},
               {"key": "kC", "label": "c"}])
    assert [PV.pick_key(p)["label"] for _ in range(3)] == ["a", "b", "c"]


def test_default_weight_min_load_priority(monkeypatch):
    """负载最低优先：b 负载 0 连续被选中（与升级前一致）。"""
    monkeypatch.setattr(registry, "load_of", _loads({"a": 1, "b": 0, "c": 2}))
    PV._key_rr.clear()
    p = _prov([{"key": "kA", "label": "a"},
               {"key": "kB", "label": "b"},
               {"key": "kC", "label": "c"}])
    assert PV.pick_key(p)["label"] == "b"
    assert PV.pick_key(p)["label"] == "b"


def test_len1_early_return_after_filtering(monkeypatch):
    """len==1 早退发生在 disabled 过滤后的可用集上（单可用 key 直选）。"""
    monkeypatch.setattr(registry, "load_of", _loads({}))
    PV._key_rr.clear()
    p = _prov([{"key": "k1", "label": "a", "disabled": True},
               {"key": "k2", "label": "b"}])
    assert PV.pick_key(p)["label"] == "b"


# ─── 2. weight=0 软停用 / disabled 优先 ─────────────────────────────

def test_zero_weight_soft_disabled_and_single_none(monkeypatch):
    monkeypatch.setattr(registry, "load_of", _loads({"a": 0, "b": 0}))
    PV._key_rr.clear()
    # weight=0 不参与选号 → 只 b 可选
    assert PV.pick_key(_prov([{"key": "k1", "label": "a", "weight": 0},
                              {"key": "k2", "label": "b"}]))["label"] == "b"
    # 单 key 且 weight=0 → None（过滤在 len==1 早退之前）
    assert PV.pick_key(_prov([{"key": "k1", "label": "a", "weight": 0}])) is None
    # disabled 与 weight=0 并存：都不可选 → None
    assert PV.pick_key(_prov([
        {"key": "k1", "label": "a", "weight": 0, "disabled": True},
        {"key": "k2", "label": "b", "weight": 0}])) is None


# ─── 3. 同负载 weight 偏置 + 钳制容错 ───────────────────────────────

def test_higher_weight_wins_under_equal_load(monkeypatch):
    """同负载（=2）下 weight=2 的 effective_load=1 < weight=1 的 2 → b 先。"""
    monkeypatch.setattr(registry, "load_of", _loads({"a": 2, "b": 2}))
    PV._key_rr.clear()
    p = _prov([{"key": "k1", "label": "a", "weight": 1},
               {"key": "k2", "label": "b", "weight": 2}])
    assert PV.pick_key(p)["label"] == "b"


def test_weight_clamp_string_tolerance_and_warning(caplog):
    PV._WEIGHT_WARNED.clear()
    with caplog.at_level(logging.WARNING, logger="qidi-relay"):
        keys = PV._parse_keys("tp", {"api_keys": [
            {"key": "kNeg", "label": "neg", "weight": -5},
            {"key": "kBig", "label": "big", "weight": 5000},
            {"key": "kStr", "label": "str", "weight": "2"},
            {"key": "kBad", "label": "bad", "weight": "abc"},
            {"key": "kOne", "label": "one", "weight": 1},
        ]})
    by = {e["label"]: e for e in keys}
    assert by["neg"]["weight"] == 0          # 负数→0（软停用）
    assert by["big"]["weight"] == 1000       # 超 1000→1000
    assert by["str"]["weight"] == 2          # 字符串 "2"→int 容错
    assert "weight" not in by["bad"]         # 非法→缺省 1（省略）
    assert "weight" not in by["one"]         # 缺省=1 不输出（config 干净）

    msgs = [r.getMessage() for r in caplog.records if "权重配置" in r.getMessage()]
    assert any("负数" in m for m in msgs), msgs
    assert any("超上限" in m for m in msgs), msgs
    assert any("非整数" in m for m in msgs), msgs

    # 逐 key 一次性：重复解析不重复告警
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="qidi-relay"):
        PV._parse_keys("tp", {"api_keys": [{"key": "kNeg", "label": "neg", "weight": -5}]})
    assert not [r for r in caplog.records if "权重配置" in r.getMessage()]


# ─── 4. accounts 侧：透传/展示 + 不参与 get_next ────────────────────

def test_accounts_weight_passthrough_display_and_no_erasure(tmp_path, monkeypatch):
    acc_file = tmp_path / "accounts.json"
    acc_file.write_text(json.dumps({"accounts": [
        {"user_id": "u1", "access_token": "t", "weight": 3},
        {"user_id": "u2", "access_token": "t"},
    ]}), encoding="utf-8")
    monkeypatch.setattr(A, "ACCOUNTS_FILE", acc_file)

    mgr = A.AccountManager()
    assert mgr.accounts[0]["weight"] == 3
    assert mgr.accounts[1]["weight"] == 1          # 缺省 1

    # _save 全量回写不抹除 weight（白名单透传的意义）
    mgr._save()
    saved = json.loads(acc_file.read_text(encoding="utf-8"))
    assert saved["accounts"][0]["weight"] == 3

    # 面板展示透传
    st = {s["user_id"]: s for s in mgr.get_all_status()}
    assert st["u1"]["weight"] == 3
    assert st["u2"]["weight"] == 1


def test_accounts_weight_not_used_by_get_next(tmp_path, monkeypatch):
    """本批不改 get_next 选号：weight 不影响轮转结果。"""
    acc_file = tmp_path / "accounts.json"
    acc_file.write_text(json.dumps({"accounts": [
        {"user_id": "u1", "access_token": "t", "weight": 100},
        {"user_id": "u2", "access_token": "t", "weight": 1},
    ]}), encoding="utf-8")
    monkeypatch.setattr(A, "ACCOUNTS_FILE", acc_file)
    mgr = A.AccountManager()
    mgr.index = 0
    picks = {mgr.get_next()["user_id"] for _ in range(2)}
    assert picks == {"u1", "u2"}                  # 轮转口径与 weight 无关

    # add_account 透传 weight 入参（缺省 1；非法防御→1）
    mgr.add_account("u3", "t", weight=5)
    assert next(a for a in mgr.accounts if a["user_id"] == "u3")["weight"] == 5
    mgr.add_account("u4", "t", weight="bad")
    assert next(a for a in mgr.accounts if a["user_id"] == "u4")["weight"] == 1


def test_get_next_model_none_skips_model_accounts(tmp_path, monkeypatch):
    """回归（1019 定向绑定）：model=None 不得进 model_accounts 查表。

    曾因 `ma.get(model.lower())` 对 None 调 .lower() 抛 AttributeError；
    空 model 应跳过定向绑定、保持全池轮转；有 model 时定向绑定照旧生效。
    """
    import proxy_core.settings as S

    acc_file = tmp_path / "accounts.json"
    acc_file.write_text(json.dumps({"accounts": [
        {"user_id": "u1", "access_token": "t"},
        {"user_id": "u2", "access_token": "t"},
    ]}), encoding="utf-8")
    monkeypatch.setattr(A, "ACCOUNTS_FILE", acc_file)
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"model_accounts": {"GLM-5.3": ["u1"]}}),
                   encoding="utf-8")
    monkeypatch.setattr(S, "CONFIG_FILE", cfg)

    mgr = A.AccountManager()
    mgr.index = 0
    assert {mgr.get_next()["user_id"] for _ in range(2)} == {"u1", "u2"}
    # 语义不变：显式 model 命中定向绑定时仍收窄到指定账号
    assert mgr.get_next(model="GLM-5.3")["user_id"] == "u1"


# ─── 5. _parse_keys 白名单透传单测 ──────────────────────────────────

def test_parse_keys_weight_whitelist():
    keys = PV._parse_keys("tp", {"api_keys": [
        {"key": "kA", "label": "a", "weight": 4},
        {"key": "kB", "label": "b"},
    ]})
    m = {e["label"]: e for e in keys}
    assert m["a"]["weight"] == 4
    assert "weight" not in m["b"] and m["b"].get("weight", 1) == 1

    # disabled 与 weight 并存时两键都在（面板徽章区分依据）
    keys2 = PV._parse_keys("tp", {"api_keys": [
        {"key": "kD", "label": "d", "disabled": True, "weight": 0},
    ]})
    assert keys2[0]["disabled"] is True and keys2[0]["weight"] == 0

    # 旧的 api_key 单值（无权重）→ 缺省 1，entry 不含 weight
    keys3 = PV._parse_keys("tp", {"api_key": "kSolo"})
    assert "weight" not in keys3[0]
