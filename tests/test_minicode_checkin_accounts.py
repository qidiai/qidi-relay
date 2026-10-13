# -*- coding: utf-8 -*-
"""MiniCode 多账号签到（accounts.json）加载与临时通道构建测试
全程本地 mock（临时 auth.json，token 未过期不走网络），零真实请求。"""

import json
import pathlib
import sys
import time

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "minicode"))

from proxy_core import minicode  # noqa: E402
import minicode_checkin as MC    # noqa: E402


def _auth_doc(access="mmoat_aaa", refresh="mmort_rrr", exp_ms=None):
    return {
        "schemaVersion": 1,
        "records": {
            "com.minimax.mcode.oauth.prod.cn\u0000abc": {
                "schemaVersion": 1,
                "accessToken": access,
                "refreshToken": refresh,
                "tokenType": "Bearer",
                "clientId": "mcode-public",
                "scopes": ["agent.default"],
                "audience": "agent-backend",
                "expiresAtMs": exp_ms if exp_ms is not None
                else int((time.time() + 3600) * 1000),
                "generation": 1,
                "loginEpoch": "epoch-1",
            },
        },
    }


def test_load_accounts_missing_file(tmp_path):
    """accounts.json 缺失 -> []（仅主号签到，行为同旧版）"""
    MC.LOG = tmp_path / "test-checkin.log"
    assert MC.load_accounts(tmp_path / "nope.json") == []


def test_load_accounts_list_and_filter(tmp_path):
    """dict{accounts}/裸 list、enabled=false 与缺 auth_path 过滤、label 回退"""
    MC.LOG = tmp_path / "test-checkin.log"
    f = tmp_path / "accounts.json"
    f.write_text(json.dumps({
        "accounts": [
            {"label": "小号1", "auth_path": str(tmp_path / "a.json")},
            {"label": "停用号", "auth_path": str(tmp_path / "b.json"),
             "enabled": False},
            {"auth_path": str(tmp_path / "c.json")},   # label 回退=文件名 stem
            {"label": "缺path"},
        ]}, ensure_ascii=False), encoding="utf-8")
    out = MC.load_accounts(f)
    assert [a["label"] for a in out] == ["小号1", "c"]
    assert all(a["auth_path"] for a in out)


def test_adhoc_channel_token_local_file(tmp_path):
    """accounts.json 条目 -> 临时通道 get_token（仅读本地文件，无网络）"""
    MC.LOG = tmp_path / "test-checkin.log"
    ap = tmp_path / "x.json"
    ap.write_text(json.dumps(_auth_doc(access="mmoat_xxx")),
                  encoding="utf-8")
    ch = minicode.MiniCodeChannel(
        "minicode", {"auth_path": str(ap), "region": "cn"}, label="小号X")
    assert ch.get_token() == "mmoat_xxx"
    assert ch.auth_path == str(ap)


def test_load_accounts_corrupt_returns_empty(tmp_path):
    """损坏 JSON -> []，不抛异常"""
    MC.LOG = tmp_path / "test-checkin.log"
    f = tmp_path / "accounts.json"
    f.write_text("{not json", encoding="utf-8")
    assert MC.load_accounts(f) == []


def test_checkin_channel_uid_dedup(tmp_path, monkeypatch):
    """同 uid 二次进入 -> ALREADY，且不再发任何 claim/status 请求（零网络）"""
    MC.LOG = tmp_path / "t-dedup.log"
    ap = tmp_path / "y.json"
    ap.write_text(json.dumps(_auth_doc(access="mmoat_yyy")),
                  encoding="utf-8")
    ch = minicode.MiniCodeChannel(
        "minicode", {"auth_path": str(ap), "region": "cn"}, label="小号Y")
    monkeypatch.setattr(MC, "_fetch_real_user_id",
                        lambda token, timeout: "UID-DUP")

    def _boom(*a, **k):
        raise AssertionError("uid 去重命中后不应再发请求")
    monkeypatch.setattr(MC, "_request", _boom)

    seen = {"UID-DUP"}
    r = MC.checkin_channel(ch, 5, True, seen)
    assert r == "ALREADY"

    # 首次进入（seen 不含 uid）-> 正常放行去 fetch 身份后加入集合
    seen2 = set()
    monkeypatch.setattr(MC, "_request",
                        lambda *a, **k: (200, '{"base_resp":{"status_code":0},'
                                          '"data":{"days":[]}}'))
    r2 = MC.checkin_channel(ch, 5, True, seen2)
    assert "UID-DUP" in seen2
    assert r2 == "UNAVAILABLE"   # days 空 -> 无可签日
