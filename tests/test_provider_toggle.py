# -*- coding: utf-8 -*-
"""Provider 独立开关机制（T10）回归测试。

覆盖：
- enabled 缺省=True（未写开关的 provider 视为启用）
- enabled=false：match 返回 None + all_models 不含其模型
  （干净的"池已关闭"语义）；key 池仍在、可再启用恢复
- 文本级结构保留式写盘：pricing 大小写折叠重复键
  不因回写丢失（json.dump 整文件回写会消除）
- 端点 200 且复读生效（磁盘文本/解析值/内存重载/面板）
- 坏输入 4xx（provider 不存在 / enabled 非布尔）且
  文件逐字节不变
- openrouter 块自带显式 enabled:true 兼容（替换值分支）

单例隔离约定：与 test_provider_keys 相同——import
routes.admin 前先把 settings 路径常量指到临时目录，
绝不触碰仓库根数据文件。
"""

import json
import pathlib
import sys
import tempfile

import pytest

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

# ─── 路径重定向（必须在 import routes.admin 之前）──────────────────────────
_TMP = pathlib.Path(tempfile.mkdtemp(prefix="qidi-relay-provtoggle-"))
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
            "onekey": {
                "base_url": "https://api.one.example/v1",
                "api_keys": ["sk-only"],
                "models": ["one-1"],
            },
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


def _info(c, name):
    r = c.get("/api/providers/info")
    assert r.status_code == 200
    return next(p for p in r.get_json()["providers"] if p["name"] == name)


def _reload():
    providers._cache = None
    return providers.all_providers()


def test_enabled_defaults_to_true(client):
    """① 未写 enabled 的 provider 缺省视为启用（含解析归一与面板暴露）。"""
    c, _ = client
    provs = providers.all_providers()
    assert provs["agnes"]["enabled"] is True
    assert provs["onekey"]["enabled"] is True
    # 面板 /providers/info 同样暴露 enabled=true
    assert _info(c, "agnes")["enabled"] is True
    assert _info(c, "onekey")["enabled"] is True


def test_disabled_provider_is_unroutable(client):
    """② enabled=false：match 返回 None、all_models 不含其模型；可再启用。"""
    c, _ = client
    r = c.post("/api/providers/agnes/enabled", json={"enabled": False})
    assert r.status_code == 200, r.get_data(as_text=True)
    provs = _reload()
    assert provs["agnes"]["enabled"] is False
    assert providers.match("agnes-2.0-flash") is None
    assert "agnes-2.0-flash" not in [m["id"] for m in providers.all_models()]
    # 面板仍列出（两态可见、可再启用），enabled=false
    assert _info(c, "agnes")["enabled"] is False
    # 重新启用 → 立即恢复路由与模型列表
    r = c.post("/api/providers/agnes/enabled", json={"enabled": True})
    assert r.status_code == 200
    _reload()
    assert providers.match("agnes-2.0-flash")["name"] == "agnes"
    assert "agnes-2.0-flash" in [m["id"] for m in providers.all_models()]


def test_text_write_preserves_duplicate_keys(client):
    """③ 文本级写盘保留 pricing 大小写折叠重复键（json.dump 整文件回写会消除）。"""
    c, cfg_file = client
    text = cfg_file.read_text(encoding="utf-8")
    assert '"providers": {' in text
    dup = ('"pricing": {'
           '"deepseek-v4-flash": {"input": 1, "output": 2},'
           '"DeepSeek-V4-Flash": {"input": 3, "output": 4}'
           '}, "providers": {')
    cfg_file.write_text(text.replace('"providers": {', dup, 1), encoding="utf-8")
    providers._cache = None
    r = c.post("/api/providers/agnes/enabled", json={"enabled": False})
    assert r.status_code == 200, r.get_data(as_text=True)
    after = cfg_file.read_text(encoding="utf-8")
    # 重复键仍在（文本级保留，未被 json.dump 归一消除）
    assert '"deepseek-v4-flash"' in after
    assert '"DeepSeek-V4-Flash"' in after
    # 且开关确实写入、全文可解析
    assert json.loads(after)["providers"]["agnes"]["enabled"] is False


def test_endpoint_roundtrip_persists(client):
    """④ 端点 200 且复读生效：磁盘文本、解析值、内存重载、面板四处一致。"""
    c, cfg_file = client
    r = c.post("/api/providers/onekey/enabled", json={"enabled": False})
    assert r.status_code == 200
    assert r.get_json() == {"provider": "onekey", "enabled": False}
    # 复读：磁盘文本
    on_disk = json.loads(cfg_file.read_text(encoding="utf-8"))
    assert on_disk["providers"]["onekey"]["enabled"] is False
    # 内存热生效 + 面板
    assert providers.all_providers()["onekey"]["enabled"] is False
    assert _info(c, "onekey")["enabled"] is False
    # 再切换回来（覆盖"已有 enabled 键则原地替换值"分支）
    r = c.post("/api/providers/onekey/enabled", json={"enabled": True})
    assert r.status_code == 200
    assert json.loads(cfg_file.read_text(encoding="utf-8"))["providers"]["onekey"]["enabled"] is True
    _reload()
    assert providers.match("one-1")["name"] == "onekey"


def test_endpoint_rejects_bad_input_without_touching_file(client):
    """⑤ 坏输入 4xx：provider 不存在 / enabled 非布尔；文件逐字节不变。"""
    c, cfg_file = client
    before = cfg_file.read_text(encoding="utf-8")
    assert c.post("/api/providers/nope/enabled", json={"enabled": True}).status_code == 404
    assert c.post("/api/providers/agnes/enabled", json={"enabled": "false"}).status_code == 400
    assert c.post("/api/providers/agnes/enabled", json={"enabled": 1}).status_code == 400
    assert c.post("/api/providers/agnes/enabled", json={"enabled": None}).status_code == 400
    assert c.post("/api/providers/agnes/enabled", json={}).status_code == 400
    assert cfg_file.read_text(encoding="utf-8") == before


def test_explicit_enabled_true_openrouter_block(client):
    """⑥ openrouter 块自带显式 enabled:true：正常路由、面板暴露 true、可切换。"""
    c, cfg_file = client
    text = cfg_file.read_text(encoding="utf-8")
    or_block = (
        '    "openrouter": {\n'
        '      "enabled": true,\n'
        '      "base_url": "https://openrouter.ai/api/v1",\n'
        '      "api_keys": ["sk-or-v1-test"],\n'
        '      "models": ["deepseek/deepseek-chat"]\n'
        '    },\n'
    )
    cfg_file.write_text(
        text.replace('"providers": {', '"providers": {\n' + or_block, 1),
        encoding="utf-8")
    providers._cache = None
    prov = providers.match("deepseek/deepseek-chat")
    assert prov is not None and prov["name"] == "openrouter"
    assert prov["enabled"] is True
    assert _info(c, "openrouter")["enabled"] is True
    # 显式 true 也能切到 false（原地替换值分支）
    r = c.post("/api/providers/openrouter/enabled", json={"enabled": False})
    assert r.status_code == 200
    _reload()
    assert providers.match("deepseek/deepseek-chat") is None
    assert "deepseek/deepseek-chat" not in [m["id"] for m in providers.all_models()]
