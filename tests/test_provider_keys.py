# -*- coding: utf-8 -*-
"""外部 Provider API Key 自助管理端点的回归测试（不访问真实上游、不触碰真实 key）。

覆盖：
- 增 key：热生效（providers 重载）、写回 config.json、自动备份、label 自动编号
- 旧单 api_key 字段折叠进 api_keys
- 删 key：按 label、删空告警、provider 随之停用
- 重复 key 409 / 备注撞名 409 / AtomCode·Cline 通道 400 / provider 不存在 404
- 响应与 /api/providers/info 一律脱敏，绝不回完整 key

单例隔离约定（与 test_stream_failover 相同）：
proxy_core.state 的 am/token_stats 等单例在首次 import 时按 settings 路径常量
加载数据文件。本文件 import routes.admin 会触发该实例化——必须先把路径指到
临时目录，避免把真实 accounts.json / token_stats.json 装进内存；同时按约定
提供 9001-9003 合成账号，保证后收集的 test_stream_failover 拿到它期望的
账号池（import 顺序契约：本文件收集序在 stream 之前）。
"""

import json
import pathlib
import sys
import tempfile

import pytest

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

# ─── 路径重定向（必须在 import routes.admin 之前）──────────────────────────
_TMP = pathlib.Path(tempfile.mkdtemp(prefix="qidi-relay-keytest-"))
(_TMP / "config.json").write_text(json.dumps({"providers": {}}), encoding="utf-8")
(_TMP / "accounts.json").write_text(json.dumps({"accounts": [
    {"user_id": uid, "username": f"t{uid}", "org_id": "0", "access_token": "a.b.c",
     "enabled": True, "has_glm53": False, "budget_exceeded": False, "group": ""}
    for uid in ("9001", "9002", "9003")]}), encoding="utf-8")

import proxy_core.settings as _S  # noqa: E402

_S.BASE_DIR = _TMP
_S.CONFIG_FILE = _TMP / "config.json"
_S.ACCOUNTS_FILE = _TMP / "accounts.json"
_S.STATS_FILE = _TMP / "token_stats.json"
_S.WATER_FILE = _TMP / "water_check.json"
_S.LOG_FILE = _TMP / "proxy.log"

import proxy_core.accounts as _A  # noqa: E402
import proxy_core.stats as _ST  # noqa: E402
import proxy_core.water as _WA  # noqa: E402
import proxy_core.providers as _PV  # noqa: E402

_A.ACCOUNTS_FILE = _S.ACCOUNTS_FILE
_ST.STATS_FILE = _S.STATS_FILE
_WA.WATER_FILE = _S.WATER_FILE
_PV.CONFIG_FILE = _S.CONFIG_FILE

from flask import Flask  # noqa: E402

from proxy_core import providers, settings  # noqa: E402
from routes import admin as admin_mod  # noqa: E402
from routes.admin import bp as admin_bp  # noqa: E402

FULL_KEY = "sk-test-full-key-1234567890abcdef"


class _FakeResp:
    def __init__(self, code):
        self.status_code = code

    def json(self):
        return {}


class _FakeClient:
    """假 httpx.Client：让 /api/providers/info 的订阅探测不走网络。"""

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get(self, url, **kwargs):
        return _FakeResp(401)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    cfg = {
        "admin_password": None,
        "providers": {
            "agnes": {
                "base_url": "https://api.agnes-ai.cn/v1",
                "api_keys": ["sk-aaa", {"key": "sk-bbb", "label": "会员"}],
                "models": ["agnes-2.0-flash"],
            },
            "solo": {
                "base_url": "https://api.solo.example/v1",
                "api_key": "sk-solo-old",
                "models": ["solo-1"],
            },
            "onekey": {
                "base_url": "https://api.one.example/v1",
                "api_keys": ["sk-only"],
                "models": ["one-1"],
            },
            "atomcode": {"type": "atomcode", "models": ["deepseek-v4-flash"]},
            "cline": {"type": "cline", "models": ["cline-free/mimo-v2.6-flash"]},
        },
    }
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    monkeypatch.setattr(settings, "CONFIG_FILE", cfg_file)
    monkeypatch.setattr(providers, "CONFIG_FILE", cfg_file)
    monkeypatch.setattr(admin_mod.httpx, "Client", _FakeClient)
    providers._cache = None
    admin_mod._provider_cache.clear()
    admin_mod._provider_cache.update(ts=0, data=None)
    app = Flask(__name__)
    app.register_blueprint(admin_bp)
    app.config["TESTING"] = True
    return app.test_client(), cfg_file


def _info(client, name):
    r = client.get("/api/providers/info")
    assert r.status_code == 200
    return next(p for p in r.get_json()["providers"] if p["name"] == name)


def _cfg_of(cfg_file):
    return json.loads(cfg_file.read_text(encoding="utf-8"))


def test_add_key_roundtrip(client):
    c, cfg_file = client
    r = c.post("/api/provider/key/add", json={"provider": "agnes", "key": FULL_KEY})
    assert r.status_code == 200, r.get_data(as_text=True)
    d = r.get_json()
    assert d["key_count"] == 3
    assert d["label"] == "agnes·3"          # 自动编号
    assert d["preview"].startswith("sk-tes") and d["preview"] != FULL_KEY

    # 写回 config.json（api_keys 列表，含旧条目）
    prov = _cfg_of(cfg_file)["providers"]["agnes"]
    keys = [e["key"] for e in prov["api_keys"]]
    assert keys == ["sk-aaa", "sk-bbb", FULL_KEY]
    assert "api_key" not in prov
    # 自动备份存在
    baks = list(cfg_file.parent.glob("config.json.bak-keyadd-*"))
    assert baks, "应自动备份 config.json"
    # 热生效：providers 重载后看到新 key
    agnes = providers.all_providers()["agnes"]
    assert [k["key"] for k in agnes["keys"]] == keys
    # 面板脱敏列表
    info = _info(c, "agnes")
    assert info["key_count"] == 3
    assert all(e["preview"] != e.get("key") for e in info["key_entries"])
    # 全响应不泄露完整 key
    assert FULL_KEY not in r.get_data(as_text=True)
    assert FULL_KEY not in c.get("/api/providers/info").get_data(as_text=True)


def test_add_key_with_label_and_conflicts(client):
    c, _ = client
    r = c.post("/api/provider/key/add",
               json={"provider": "agnes", "key": FULL_KEY, "label": "备用号"})
    assert r.status_code == 200 and r.get_json()["label"] == "备用号"
    # 同 key 重复 → 409
    r = c.post("/api/provider/key/add",
               json={"provider": "agnes", "key": FULL_KEY, "label": "再来"})
    assert r.status_code == 409
    # 备注撞名 → 409
    r = c.post("/api/provider/key/add",
               json={"provider": "agnes", "key": "sk-other-999", "label": "备用号"})
    assert r.status_code == 409


def test_add_key_rejects_non_static_and_unknown(client):
    c, _ = client
    assert c.post("/api/provider/key/add",
                  json={"provider": "atomcode", "key": "sk-x"}).status_code == 400
    assert c.post("/api/provider/key/add",
                  json={"provider": "cline", "key": "sk-x"}).status_code == 400
    assert c.post("/api/provider/key/add",
                  json={"provider": "nope", "key": "sk-x"}).status_code == 404
    assert c.post("/api/provider/key/add", json={"provider": "agnes"}).status_code == 400


def test_remove_key_roundtrip(client):
    c, cfg_file = client
    r = c.post("/api/provider/key/remove", json={"provider": "agnes", "label": "会员"})
    assert r.status_code == 200
    assert r.get_json()["key_count"] == 1
    prov = _cfg_of(cfg_file)["providers"]["agnes"]
    labels = [e["label"] for e in prov["api_keys"]]
    assert labels == ["agnes·1"]
    # 再删一次 → 404
    assert c.post("/api/provider/key/remove",
                  json={"provider": "agnes", "label": "会员"}).status_code == 404
    # 删不存在的 provider → 404
    assert c.post("/api/provider/key/remove",
                  json={"provider": "nope", "label": "x"}).status_code == 404


def test_remove_last_key_warns_and_disables_provider(client):
    c, _ = client
    r = c.post("/api/provider/key/remove", json={"provider": "onekey", "label": "onekey·1"})
    assert r.status_code == 200
    d = r.get_json()
    assert d["key_count"] == 0 and "warning" in d
    # 无 key 的静态 provider 被 providers 层忽略（暂停服务）
    assert "onekey" not in providers.all_providers()


def test_legacy_single_api_key_folds_into_list(client):
    c, cfg_file = client
    r = c.post("/api/provider/key/add", json={"provider": "solo", "key": FULL_KEY})
    assert r.status_code == 200
    prov = _cfg_of(cfg_file)["providers"]["solo"]
    assert "api_key" not in prov
    assert [e["key"] for e in prov["api_keys"]] == ["sk-solo-old", FULL_KEY]
    assert [e["label"] for e in prov["api_keys"]] == ["solo·1", "solo·2"]
