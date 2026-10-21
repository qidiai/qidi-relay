# -*- coding: utf-8 -*-
"""「刷新模型」端点回归测试（GET /api/providers/<name>/models/upstream）。

覆盖：
- 成功：带第一个启用中的 key 打 GET {base_url}/models，返回
  {"ok": true, "models": [id...]}（兼容 OpenAI data / 纯字符串 /
  无 id 条目剔除）
- 上游 401 / 404 / 超时：一律 HTTP 200 + {"ok": false, "error"}，
  文案带状态码/超时等可读信息
- 特殊渠道（atomcode/cline/minicode/opencode-serve）：明确
  「该渠道类型不支持列表刷新」，且不发起任何上游请求
- 本地校验：provider 不存在 404 / 缺 base_url / 无启用中的 key
  → ok=false（不访问上游）

不访问真实上游、不触碰真实数据文件。单例隔离约定同
test_provider_keys：import routes.admin 前先把 settings 路径
常量指到临时目录，并放 9001-9003 合成账号池。
"""

import json
import pathlib
import sys
import tempfile

import httpx
import pytest

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

# ─── 路径重定向（必须在 import routes.admin 之前）──────────────────────────
_TMP = pathlib.Path(tempfile.mkdtemp(prefix="qidi-relay-modelrefresh-"))
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
import proxy_core.accounts as accounts_mod  # noqa: E402
from routes import admin as admin_mod  # noqa: E402
from routes.admin import bp as admin_bp  # noqa: E402


class _FakeResp:
    def __init__(self, code, payload=None):
        self.status_code = code
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload

    @property
    def text(self):
        return json.dumps(self._payload, ensure_ascii=False)


class _ModelsClient:
    """假 httpx.Client：记录 GET /models 请求，按类级配置返回/抛错。"""

    status = 200
    payload = {"data": []}
    exc = None
    calls = []

    @classmethod
    def reset(cls, status=200, payload=None, exc=None):
        cls.status = status
        cls.payload = payload if payload is not None else {"data": []}
        cls.exc = exc
        cls.calls = []

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get(self, url, **kwargs):
        type(self).calls.append((url, kwargs))
        if type(self).exc is not None:
            raise type(self).exc
        return _FakeResp(type(self).status, type(self).payload)


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
            # 第一个 key 停用 → 刷新必须跳过，用第一个启用中的 key
            "halfoff": {
                "base_url": "https://api.half.example/v1",
                "api_keys": [
                    {"key": "sk-disabled", "label": "停用", "disabled": True},
                    {"key": "sk-live", "label": "在用"},
                ],
                "models": ["half-1"],
            },
            "atomcode": {"type": "atomcode", "models": ["deepseek-v4-flash"]},
            "cline": {"type": "cline", "models": ["cline-free/mimo-v2.6-flash"]},
            "minicode": {"type": "minicode", "models": ["mcode-1"]},
            "ocserve": {"type": "opencode-serve", "models": ["free-1"]},
            "nokey": {"base_url": "https://api.nokey.example/v1", "models": ["x"]},
            "nobase": {"api_keys": ["sk-x"], "models": ["y"]},
        },
    }
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    acct_file = tmp_path / "accounts.json"
    acct_file.write_text(json.dumps({"accounts": [
        {"user_id": "9001", "username": "t9001", "org_id": "0", "access_token": "a.b.c",
         "enabled": True, "has_glm53": False, "budget_exceeded": False, "group": ""},
    ]}, ensure_ascii=False, indent=2), encoding="utf-8")
    monkeypatch.setattr(settings, "CONFIG_FILE", cfg_file)
    monkeypatch.setattr(settings, "ACCOUNTS_FILE", acct_file)
    monkeypatch.setattr(providers, "CONFIG_FILE", cfg_file)
    monkeypatch.setattr(accounts_mod, "ACCOUNTS_FILE", acct_file)
    _ModelsClient.reset()
    monkeypatch.setattr(admin_mod.httpx, "Client", _ModelsClient)
    providers._cache = None
    admin_mod._provider_cache.clear()
    admin_mod._provider_cache.update(ts=0, data=None)
    app = Flask(__name__)
    app.register_blueprint(admin_bp)
    app.config["TESTING"] = True
    tc = app.test_client()
    yield tc, cfg_file
    providers._cache = None


def test_upstream_models_success(client):
    """成功：解析 id 列表（对象/纯字符串/坏条目剔除）+ 首个 key 鉴权。"""
    c, _ = client
    _ModelsClient.reset(payload={"data": [
        {"id": "agnes-2.5-flash"}, {"id": "agnes-3.0-flash"},
        "plain-string-model", {"no-id": 1}, {"id": ""}]})
    r = c.get("/api/providers/agnes/models/upstream")
    assert r.status_code == 200
    d = r.get_json()
    assert d == {"ok": True,
                 "models": ["agnes-2.5-flash", "agnes-3.0-flash",
                            "plain-string-model"]}
    assert len(_ModelsClient.calls) == 1
    url, kwargs = _ModelsClient.calls[0]
    assert url == "https://api.agnes-ai.cn/v1/models"
    assert kwargs["headers"]["Authorization"] == "Bearer sk-aaa"


def test_upstream_models_skips_disabled_key(client):
    """F14：停用的 key 不参与刷新鉴权，取第一个启用中的 key。"""
    c, _ = client
    _ModelsClient.reset(payload={"data": [{"id": "half-2"}]})
    r = c.get("/api/providers/halfoff/models/upstream")
    assert r.status_code == 200
    assert r.get_json()["ok"] is True
    assert _ModelsClient.calls[0][1]["headers"]["Authorization"] == "Bearer sk-live"


def test_upstream_models_401_ok_false(client):
    """上游 401 → HTTP 200 + ok=false + 可读 key 错误文案。"""
    c, _ = client
    _ModelsClient.reset(status=401)
    r = c.get("/api/providers/agnes/models/upstream")
    assert r.status_code == 200
    d = r.get_json()
    assert d["ok"] is False
    assert "401" in d["error"]
    assert "Key" in d["error"]


def test_upstream_models_404_ok_false(client):
    """上游 404（无 /models 端点）→ ok=false + 明确文案。"""
    c, _ = client
    _ModelsClient.reset(status=404)
    d = c.get("/api/providers/agnes/models/upstream").get_json()
    assert d["ok"] is False
    assert "404" in d["error"]
    assert "/models" in d["error"]


def test_upstream_models_timeout_ok_false(client):
    """上游超时 → ok=false + 超时文案。"""
    c, _ = client
    _ModelsClient.reset(exc=httpx.TimeoutException("mock timeout"))
    d = c.get("/api/providers/agnes/models/upstream").get_json()
    assert d["ok"] is False
    assert "超时" in d["error"]


def test_upstream_models_connect_error_ok_false(client):
    """连接失败（DNS/拒绝）→ ok=false + 连接失败文案。"""
    c, _ = client
    _ModelsClient.reset(exc=httpx.ConnectError("mock connect error"))
    d = c.get("/api/providers/agnes/models/upstream").get_json()
    assert d["ok"] is False
    assert "连接" in d["error"]


@pytest.mark.parametrize("pname", ["atomcode", "minicode", "ocserve"])
def test_special_channel_unsupported(client, pname):
    """特殊渠道（签名/非 OpenAI 协议）→ 明确错误且不发请求。

    cline 自 T22（2026-10-20）起有独立的上游列表通道（OAuth token + 指纹头
    拉 api.cline.bot/api/v1/models），不再属于"不支持"集合，故从参数里移除。
    """
    c, _ = client
    _ModelsClient.reset()
    r = c.get(f"/api/providers/{pname}/models/upstream")
    assert r.status_code == 200
    d = r.get_json()
    assert d["ok"] is False
    assert "无 OpenAI 兼容上游列表" in d["error"]
    assert _ModelsClient.calls == []


def test_upstream_models_provider_not_found_404(client):
    """provider 不存在 → 404（本地校验，不访问上游）。"""
    c, _ = client
    _ModelsClient.reset()
    r = c.get("/api/providers/nope/models/upstream")
    assert r.status_code == 404
    assert r.get_json()["ok"] is False
    assert _ModelsClient.calls == []


def test_upstream_models_missing_base_or_key(client):
    """缺 base_url / 无启用中的 key → ok=false，不访问上游。"""
    c, _ = client
    _ModelsClient.reset()
    d = c.get("/api/providers/nobase/models/upstream").get_json()
    assert d["ok"] is False and "base_url" in d["error"]
    d = c.get("/api/providers/nokey/models/upstream").get_json()
    assert d["ok"] is False and "API Key" in d["error"]
    assert _ModelsClient.calls == []
