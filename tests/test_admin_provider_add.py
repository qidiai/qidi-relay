# -*- coding: utf-8 -*-
"""管理端 F1/F2/F3 回归测试（不访问真实上游、不触碰真实数据文件）。

覆盖：
- F1 页面添加新渠道：合法插入（块结构正确 + pricing 重复键
  保留 + 块首插入 + 备份）/ name 重复 400 / name 非法字符
  400 / base_url 非 https 400 / key 空 400 / 写后复读校验
  失败 500 不动原文（mock）；F1→F2 端到端串联（add 未
  激活 → probe → 写回 models → all_models 含且 match 命中）
- F2 一键探测上游可用模型：probe 解析逻辑（models 列表 +
  keyword 过滤 + ok/fail 分类）/ 并发上限与超时行为（mock
  慢响应）/ models 写回替换正确且重复键保留 / provider 不
  存在 404 / 上游 /models 非 200 → 502
- 病理配置确定性拒绝（N-1）：providers 节内目标非对象
  （如 y 在前且 y.models 含字符串 "x"）→ 文本手术前
  ValueError / 端点 404 拒绝，config 逐字节不动
- F3 账号分组标签：group 三值写入正确 / 非白名单值 400 /
  uid 不存在 404 / span 编辑不碰其他账号（多账号 fixture）/
  写后 accounts.json 可解析且目标账号 group 正确 + 单例热
  重载 / 无 group 键条目插入分支 / 紧凑格式（单行与仅
  user_id 成员）写 group

单例隔离约定：与 test_provider_keys 相同——import routes.admin
前先把 settings 路径常量指到临时目录，绝不触碰仓库根数据文件。
（本文件收集序在所有测试文件之前，单例按本 preamble 的
9001-9003 合成账号池构建，与 test_provider_keys /
test_stream_failover 的 import 顺序契约一致。）
"""

import json
import pathlib
import sys
import tempfile
import threading
import time

import httpx
import pytest

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

# ─── 路径重定向（必须在 import routes.admin 之前）──────────────────────────
_TMP = pathlib.Path(tempfile.mkdtemp(prefix="qidi-relay-adminadd-"))
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
from proxy_core.state import am  # noqa: E402
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


class _ProbeFakeClient:
    """假 httpx.Client（探测专用）：GET /models 返回类级
    models_payload；POST /chat/completions 按模型名分类
    （ok_models 200 / fail_models 状态码 / timeout_models
    抛 TimeoutException），slow_delay 模拟慢响应，
    线程安全地记录最大并发数。"""

    models_payload = {"data": []}
    ok_models = set()
    fail_models = {}
    timeout_models = set()
    slow_delay = 0.0
    _inflight = 0
    _max_inflight = 0
    _lock = threading.Lock()

    @classmethod
    def _reset(cls, models_payload=None, ok_models=None, fail_models=None,
               timeout_models=None, slow_delay=0.0):
        cls.models_payload = models_payload if models_payload is not None else {"data": []}
        cls.ok_models = set(ok_models or [])
        cls.fail_models = dict(fail_models or {})
        cls.timeout_models = set(timeout_models or [])
        cls.slow_delay = slow_delay
        with cls._lock:
            cls._inflight = 0
            cls._max_inflight = 0

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get(self, url, **kwargs):
        return _FakeResp(200, _ProbeFakeClient.models_payload)

    def post(self, url, **kwargs):
        model_id = (kwargs.get("json") or {}).get("model")
        with _ProbeFakeClient._lock:
            _ProbeFakeClient._inflight += 1
            _ProbeFakeClient._max_inflight = max(
                _ProbeFakeClient._max_inflight, _ProbeFakeClient._inflight)
        try:
            if _ProbeFakeClient.slow_delay:
                time.sleep(_ProbeFakeClient.slow_delay)
            if model_id in _ProbeFakeClient.timeout_models:
                raise httpx.TimeoutException("mock timeout")
            if model_id in _ProbeFakeClient.ok_models:
                return _FakeResp(200, {})
            if model_id in _ProbeFakeClient.fail_models:
                return _FakeResp(
                    _ProbeFakeClient.fail_models[model_id],
                    {"error": {"message": "mock fail"}})
            return _FakeResp(200, {})
        finally:
            with _ProbeFakeClient._lock:
                _ProbeFakeClient._inflight -= 1


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
        },
    }
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    acct_file = tmp_path / "accounts.json"
    acct_file.write_text(json.dumps({"accounts": [
        {"user_id": "9001", "username": "t9001", "org_id": "0", "access_token": "a.b.c",
         "enabled": True, "has_glm53": False, "budget_exceeded": False, "group": ""},
        {"user_id": "9002", "username": "t9002", "org_id": "0", "access_token": "a.b.c",
         "enabled": True, "has_glm53": False, "budget_exceeded": False, "group": "free"},
        {"user_id": "9003", "username": "t9003", "org_id": "0", "access_token": "a.b.c",
         "enabled": True, "has_glm53": False, "budget_exceeded": False, "group": "pro"},
    ]}, ensure_ascii=False, indent=2), encoding="utf-8")
    monkeypatch.setattr(settings, "CONFIG_FILE", cfg_file)
    monkeypatch.setattr(settings, "ACCOUNTS_FILE", acct_file)
    monkeypatch.setattr(providers, "CONFIG_FILE", cfg_file)
    monkeypatch.setattr(accounts_mod, "ACCOUNTS_FILE", acct_file)
    monkeypatch.setattr(admin_mod.httpx, "Client", _FakeClient)
    providers._cache = None
    admin_mod._provider_cache.clear()
    admin_mod._provider_cache.update(ts=0, data=None)
    app = Flask(__name__)
    app.register_blueprint(admin_bp)
    app.config["TESTING"] = True
    tc = app.test_client()
    yield tc, cfg_file, acct_file
    # 还原共享单例 am 的分组态（F3 测试经端点 am._load() 载入了
    # fixture 的分组值——还原为基线全 ""，防污染后续测试文件的
    # 路由判定：group 参与账号选择与 model_groups 绑定）
    for a in am.accounts:
        a["group"] = ""


def _cfg_text(cfg_file):
    return cfg_file.read_text(encoding="utf-8")


def _cfg_of(cfg_file):
    return json.loads(_cfg_text(cfg_file))


def _acct_text(acct_file):
    return acct_file.read_text(encoding="utf-8")


def _acct_of(acct_file):
    return json.loads(_acct_text(acct_file))


def _inject_dup_pricing(cfg_file):
    """构造含大小写折叠重复键的 pricing 节（与真实 config.json 同病）。"""
    text = _cfg_text(cfg_file)
    assert '"providers": {' in text
    dup = ('"pricing": {'
           '"deepseek-v4-flash": {"input": 1, "output": 2},'
           '"DeepSeek-V4-Flash": {"input": 3, "output": 4}'
           '}, "providers": {')
    cfg_file.write_text(text.replace('"providers": {', dup, 1), encoding="utf-8")
    providers._cache = None


# ─── F1：页面添加新渠道 ──────────────────────────────────────────

def test_add_provider_roundtrip(client):
    """F1 合法插入：块结构正确 + pricing 重复键保留 + 块首插入 + 备份。"""
    c, cfg_file, _ = client
    _inject_dup_pricing(cfg_file)
    r = c.post("/api/providers/add", json={
        "name": "myrelay",
        "base_url": "https://api.myrelay.example/v1",
        "api_key": "sk-new-relay-key",
    })
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json() == {"provider": "myrelay",
                            "base_url": "https://api.myrelay.example/v1",
                            "enabled": True}
    # 块结构正确（api_keys 主key / models 空初始 / enabled）
    got = _cfg_of(cfg_file)["providers"]["myrelay"]
    assert got == {
        "base_url": "https://api.myrelay.example/v1",
        "api_keys": [{"key": "sk-new-relay-key", "label": "主key"}],
        "models": [],
        "enabled": True,
    }
    # pricing 重复键未被消除（文本级保留）
    after = _cfg_text(cfg_file)
    assert '"deepseek-v4-flash"' in after
    assert '"DeepSeek-V4-Flash"' in after
    # 块首插入：新块在 providers 节现有成员之前
    assert after.find('"myrelay"') < after.find('"agnes"')
    # 自动备份
    baks = list(cfg_file.parent.glob("config.json.bak-provadd-*"))
    assert baks, "应自动备份 config.json"
    # 热生效：models 空初始——provider 在 config 中但按既有
    # "配置不完整（需 base_url/api_keys/models）"纪律暂不激活
    # （与 _load_providers 对无 models provider 的处理一致；
    # F1→F2 端到端串联激活见 test_f1_to_f2_end_to_end）
    providers._cache = None
    assert "myrelay" not in providers.all_providers()


def test_f1_to_f2_end_to_end(client, monkeypatch):
    """N-6 F1→F2 端到端：add（models 空未激活）→ probe
    （mock 上游）→ 探测结果写回 models → 模型进
    all_models 且 match 命中新 provider（单用例闭环，
    替代 roundtrip 尾部与 probe 用例的两段拼合）。"""
    c, cfg_file, _ = client
    # F1：页面添加新渠道（models 空初始 → 暂不激活）
    r = c.post("/api/providers/add", json={
        "name": "newchan",
        "base_url": "https://api.newchan.example/v1",
        "api_key": "sk-new-chan-key"})
    assert r.status_code == 200, r.get_data(as_text=True)
    providers._cache = None
    assert "newchan" not in providers.all_providers()
    assert "新渠道模型" not in [m["id"] for m in providers.all_models()]
    # F2：一键探测上游可用模型（mock 上游 /models + chat）
    _ProbeFakeClient._reset(
        models_payload={"data": [{"id": "新渠道模型"}, {"id": "other"}]})
    monkeypatch.setattr(admin_mod.httpx, "Client", _ProbeFakeClient)
    r = c.post("/api/providers/newchan/probe", json={"keyword": "新渠道"})
    assert r.status_code == 200, r.get_data(as_text=True)
    d = r.get_json()
    assert d["total"] == 1 and d["probed"] == 1
    assert [m["id"] for m in d["ok"]] == ["新渠道模型"]
    assert d["fail"] == []
    # F2 收尾：把探测到的模型写回 provider 配置
    r = c.post("/api/providers/newchan/models",
               json={"models": [m["id"] for m in d["ok"]]})
    assert r.status_code == 200, r.get_data(as_text=True)
    # 端到端闭环：模型进 all_models，match 命中新 provider
    providers._cache = None
    assert "新渠道模型" in [m["id"] for m in providers.all_models()]
    hit = providers.match("新渠道模型")
    assert hit is not None
    assert hit["name"] == "newchan"
    assert hit["enabled"] is True


def test_add_provider_duplicate_name_400(client):
    """F1 name 重复 → 400 且 config 逐字节不动。"""
    c, cfg_file, _ = client
    before = _cfg_text(cfg_file)
    r = c.post("/api/providers/add", json={
        "name": "agnes", "base_url": "https://x.example/v1", "api_key": "sk-x"})
    assert r.status_code == 400
    assert "已存在" in r.get_json()["error"]
    assert _cfg_text(cfg_file) == before


def test_add_provider_bad_name_400(client):
    """F1 name 非法字符（含空格/中文/空）→ 400 且 config 不动。"""
    c, cfg_file, _ = client
    before = _cfg_text(cfg_file)
    for bad in ("my relay!", "中文渠道", "a/b", ""):
        r = c.post("/api/providers/add", json={
            "name": bad, "base_url": "https://x.example/v1", "api_key": "sk-x"})
        assert r.status_code == 400, bad
    assert _cfg_text(cfg_file) == before


def test_add_provider_bad_base_url_400(client):
    """F1 base_url 非 https:// 开头 → 400 且 config 不动。"""
    c, cfg_file, _ = client
    before = _cfg_text(cfg_file)
    for bad in ("http://api.example.com/v1", "api.example.com/v1", ""):
        r = c.post("/api/providers/add", json={
            "name": "myrelay", "base_url": bad, "api_key": "sk-x"})
        assert r.status_code == 400, bad
    assert _cfg_text(cfg_file) == before


def test_add_provider_empty_key_400(client):
    """F1 api_key 空 → 400 且 config 不动。"""
    c, cfg_file, _ = client
    before = _cfg_text(cfg_file)
    r = c.post("/api/providers/add", json={
        "name": "myrelay", "base_url": "https://x.example/v1", "api_key": ""})
    assert r.status_code == 400
    assert _cfg_text(cfg_file) == before


def test_add_provider_write_validation_failure_keeps_original(client, monkeypatch):
    """F1 写后复读校验失败 → 500 且原文一字不动（mock 文本手术抛错）。"""
    c, cfg_file, _ = client
    before = _cfg_text(cfg_file)

    def _boom(*args, **kwargs):
        raise ValueError("复读校验失败：providers.myrelay 字段未正确落地")

    monkeypatch.setattr(admin_mod, "_add_provider_in_text", _boom)
    r = c.post("/api/providers/add", json={
        "name": "myrelay", "base_url": "https://x.example/v1", "api_key": "sk-x"})
    assert r.status_code == 500
    assert "复读校验失败" in r.get_json()["error"]
    assert _cfg_text(cfg_file) == before


# ─── F2：一键探测上游可用模型 ──────────────────────────────────────

def test_probe_models_filter_and_classification(client, monkeypatch):
    """F2 probe 解析逻辑：models 列表提取 + keyword 过滤（大小写
    不敏感子串）+ ok/fail 分类 + 1-token chat 请求体。"""
    c, cfg_file, _ = client
    seen_bodies = []

    class _RecClient(_ProbeFakeClient):
        def post(self, url, **kwargs):
            seen_bodies.append(kwargs.get("json"))
            return _ProbeFakeClient.post(self, url, **kwargs)

    _ProbeFakeClient._reset(
        models_payload={"data": [
            {"id": "deepseek-chat"}, {"id": "DeepSeek-R1"},
            {"id": "glm-5.3"}, "plain-string-model",
            {"no-id": 1}, {"id": ""}]},
        ok_models={"deepseek-chat"},
        fail_models={"DeepSeek-R1": 429})
    monkeypatch.setattr(admin_mod.httpx, "Client", _RecClient)
    r = c.post("/api/providers/agnes/probe", json={"keyword": "deepseek"})
    assert r.status_code == 200, r.get_data(as_text=True)
    d = r.get_json()
    # keyword 过滤：deepseek-chat / DeepSeek-R1 命中（大小写不敏感），
    # glm-5.3 / plain-string-model 不命中；无 id 与空 id 条目被剔除
    assert d["total"] == 2
    assert d["probed"] == 2
    assert [m["id"] for m in d["ok"]] == ["deepseek-chat"]
    assert d["ok"][0]["latency_ms"] >= 0
    assert d["fail"][0]["id"] == "DeepSeek-R1"
    assert d["fail"][0]["status"] == 429
    assert d["fail"][0]["why"]
    # 探测请求体是 1-token chat
    assert seen_bodies == [
        {"model": "deepseek-chat",
         "messages": [{"role": "user", "content": "hi"}],
         "max_tokens": 1},
        {"model": "DeepSeek-R1",
         "messages": [{"role": "user", "content": "hi"}],
         "max_tokens": 1}]


def test_probe_concurrency_cap_and_timeout(client, monkeypatch):
    """F2 并发上限（≤4）与超时行为：8 模型慢响应 0.3s——
    最大并发 ≤4 且总耗时远低于串行；超时模型入 fail(timeout)。"""
    c, cfg_file, _ = client
    models = [f"m-{i}" for i in range(8)]
    _ProbeFakeClient._reset(
        models_payload={"data": [{"id": m} for m in models]},
        ok_models=set(models), slow_delay=0.3)
    monkeypatch.setattr(admin_mod.httpx, "Client", _ProbeFakeClient)
    t0 = time.perf_counter()
    r = c.post("/api/providers/agnes/probe", json={"keyword": ""})
    elapsed = time.perf_counter() - t0
    d = r.get_json()
    assert d["total"] == 8 and d["probed"] == 8
    assert len(d["ok"]) == 8 and d["fail"] == []
    # 并发上限 4（ThreadPoolExecutor max_workers=4）
    assert _ProbeFakeClient._max_inflight <= 4
    assert _ProbeFakeClient._max_inflight >= 2  # 并发真实发生
    # 串行需 8×0.3=2.4s；并发 4 理论 ~0.6s——留足余量断言 <1.8s
    assert elapsed < 1.8, f"elapsed={elapsed:.2f}s 疑似串行"

    # 超时分类：timeout_models 抛 httpx.TimeoutException → fail(status=timeout)
    _ProbeFakeClient._reset(
        models_payload={"data": [{"id": "m-ok"}, {"id": "m-slow"}]},
        ok_models={"m-ok"}, timeout_models={"m-slow"})
    r = c.post("/api/providers/agnes/probe", json={"keyword": ""})
    d = r.get_json()
    assert [m["id"] for m in d["ok"]] == ["m-ok"]
    assert d["fail"][0]["id"] == "m-slow"
    assert d["fail"][0]["status"] == "timeout"
    assert "超时" in d["fail"][0]["why"]


def test_probe_models_endpoint_error_502(client, monkeypatch):
    """F2 上游 /models 非 200（无端点/鉴权失败）→ 502 + 可读错误。"""
    c, cfg_file, _ = client

    class _ErrClient(_FakeClient):
        def get(self, url, **kwargs):
            return _FakeResp(401, {})

    monkeypatch.setattr(admin_mod.httpx, "Client", _ErrClient)
    r = c.post("/api/providers/agnes/probe", json={"keyword": ""})
    assert r.status_code == 502
    assert "/models" in r.get_json()["error"]


def test_probe_models_writeback_preserves_duplicate_keys(client):
    """F2 models 写回：文本级替换 models 数组正确 + pricing 重复键保留。"""
    c, cfg_file, _ = client
    _inject_dup_pricing(cfg_file)
    r = c.post("/api/providers/agnes/models",
               json={"models": ["agnes-2.0-flash", "agnes-2.5-flash"]})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json() == {"provider": "agnes",
                            "models": ["agnes-2.0-flash", "agnes-2.5-flash"]}
    # 替换正确：旧 models 被整体替换
    assert _cfg_of(cfg_file)["providers"]["agnes"]["models"] == \
        ["agnes-2.0-flash", "agnes-2.5-flash"]
    # pricing 重复键仍在
    after = _cfg_text(cfg_file)
    assert '"deepseek-v4-flash"' in after
    assert '"DeepSeek-V4-Flash"' in after
    # 热生效：模型立即进 all_models（守卫白名单每请求读 config）
    providers._cache = None
    ids = [m["id"] for m in providers.all_models()]
    assert "agnes-2.0-flash" in ids and "agnes-2.5-flash" in ids
    # 坏输入：models 非字符串数组 → 400
    r = c.post("/api/providers/agnes/models", json={"models": "not-a-list"})
    assert r.status_code == 400


def test_probe_provider_not_found_404(client):
    """F2 provider 不存在 → 404（probe 与 models 写回同）。"""
    c, _, _ = client
    assert c.post("/api/providers/nope/probe",
                  json={"keyword": ""}).status_code == 404
    assert c.post("/api/providers/nope/models",
                  json={"models": ["m"]}).status_code == 404


# ─── 病理配置确定性拒绝（N-1）──────────────────────────────

def test_toggle_provider_non_dict_entry_rejected(client):
    """N-1 病理配置：provider「y」在前且 y.models 含字符串
    "x"（providers 节内无「x」条目）——切 x 的开关 / 写 x
    的 models 在文本手术前确定性 ValueError，端点 404 拒绝，
    config.json 逐字节不动。"""
    c, cfg_file, _ = client
    cfg = {
        "admin_password": None,
        "providers": {
            "y": {
                "base_url": "https://y.example/v1",
                "api_keys": ["sk-y"],
                "models": ["x"],
            },
            "z": {
                "base_url": "https://z.example/v1",
                "api_keys": ["sk-z"],
                "models": ["z-1"],
            },
        },
    }
    cfg_file.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    providers._cache = None
    before = _cfg_text(cfg_file)
    # helper 层：文本手术前确定性 ValueError（直接调用）
    with pytest.raises(ValueError):
        admin_mod._toggle_provider_in_text(before, "x", False)
    with pytest.raises(ValueError):
        admin_mod._set_provider_models("x", ["m-1"])
    # 端点层：确定性拒绝（既有 404 前置校验先于 helper 到达）
    r = c.post("/api/providers/x/enabled", json={"enabled": False})
    assert r.status_code == 404
    r = c.post("/api/providers/x/models", json={"models": ["m-1"]})
    assert r.status_code == 404
    # 原文逐字节不动
    assert _cfg_text(cfg_file) == before


# ─── F3：账号分组标签选择器 ────────────────────────────────────────

def test_account_group_three_values(client):
    """F3 group 三值（""/free/pro）写入正确。"""
    c, _, acct_file = client
    for g in ("free", "pro", ""):
        r = c.post("/api/accounts/9001/group", json={"group": g})
        assert r.status_code == 200, r.get_data(as_text=True)
        assert r.get_json() == {"user_id": "9001", "group": g}
        accts = _acct_of(acct_file)["accounts"]
        tgt = next(a for a in accts if a["user_id"] == "9001")
        assert tgt["group"] == g


def test_account_group_bad_value_400(client):
    """F3 非白名单值（lite/null/缺字段）→ 400 且文件逐字节不动。"""
    c, _, acct_file = client
    before = _acct_text(acct_file)
    for bad in ("lite", None, 1, "FREE"):
        r = c.post("/api/accounts/9001/group", json={"group": bad})
        assert r.status_code == 400, bad
    r = c.post("/api/accounts/9001/group", json={})
    assert r.status_code == 400
    assert _acct_text(acct_file) == before


def test_account_group_uid_not_found_404(client):
    """F3 uid 不存在 → 404 且文件不动。"""
    c, _, acct_file = client
    before = _acct_text(acct_file)
    r = c.post("/api/accounts/99999/group", json={"group": "free"})
    assert r.status_code == 404
    assert _acct_text(acct_file) == before


def test_account_group_span_edit_isolated(client):
    """F3 span 编辑不碰其他账号：改 9002，9001/9003 逐字段不变。"""
    c, _, acct_file = client
    before_accounts = _acct_of(acct_file)["accounts"]
    before_9001 = next(a for a in before_accounts if a["user_id"] == "9001")
    before_9003 = next(a for a in before_accounts if a["user_id"] == "9003")
    r = c.post("/api/accounts/9002/group", json={"group": "pro"})
    assert r.status_code == 200
    after_accounts = _acct_of(acct_file)["accounts"]
    assert len(after_accounts) == 3
    tgt = next(a for a in after_accounts if a["user_id"] == "9002")
    assert tgt["group"] == "pro"
    # 其他账号逐字段不变
    assert next(a for a in after_accounts if a["user_id"] == "9001") == before_9001
    assert next(a for a in after_accounts if a["user_id"] == "9003") == before_9003
    # 自动备份
    baks = list(acct_file.parent.glob("accounts.json.bak-acctgroup-*"))
    assert baks, "应自动备份 accounts.json"


def test_account_group_postwrite_parseable_and_hot_reload(client):
    """F3 写后 accounts.json 可解析且目标账号 group 正确 + 单例热重载。"""
    c, _, acct_file = client
    r = c.post("/api/accounts/9003/group", json={"group": ""})
    assert r.status_code == 200
    # 文件可解析且目标 group 落地
    accts = _acct_of(acct_file)["accounts"]
    assert next(a for a in accts if a["user_id"] == "9003")["group"] == ""
    # AccountManager 单例热重载生效（端点内 am._load()）
    tgt = next((a for a in am.accounts if a["user_id"] == "9003"), None)
    assert tgt is not None and tgt["group"] == ""


def test_account_group_insert_when_key_missing(client):
    """F3 条目无 group 键 → 条目内末尾前插入（不碰其他字段）。"""
    c, _, acct_file = client
    # 构造 9002 条目无 group 键的 accounts.json
    accts = _acct_of(acct_file)["accounts"]
    for a in accts:
        if a["user_id"] == "9002":
            a.pop("group", None)
    acct_file.write_text(
        json.dumps({"accounts": accts}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    before_9001 = next(a for a in accts if a["user_id"] == "9001")
    before_9003 = next(a for a in accts if a["user_id"] == "9003")
    r = c.post("/api/accounts/9002/group", json={"group": "free"})
    assert r.status_code == 200, r.get_data(as_text=True)
    after_accounts = _acct_of(acct_file)["accounts"]
    assert len(after_accounts) == 3
    tgt = next(a for a in after_accounts if a["user_id"] == "9002")
    assert tgt["group"] == "free"
    # 其他账号逐字段不变
    assert next(a for a in after_accounts if a["user_id"] == "9001") == before_9001
    assert next(a for a in after_accounts if a["user_id"] == "9003") == before_9003
    # 文本形态：group 成员落在条目内（缩进与兄弟成员一致）
    text = _acct_text(acct_file)
    i = text.find('"user_id": "9002"')
    j = text.find('"user_id": "9003"')
    seg = text[i:j]
    assert '"group": "free"' in seg
    assert text.count('"group": "free"') == 1


def _restore_am_pool(acct_file):
    """还原共享单例 am 的标准账号池（N-2 紧凑格式用例的
    条目缺 token / 缺字段，AccountManager._load 会跳过
    或变形——恢复 9001-9003 标准池，防污染后续测试
    文件的单例契约）。"""
    acct_file.write_text(json.dumps({"accounts": [
        {"user_id": uid, "username": f"t{uid}", "org_id": "0",
         "access_token": "a.b.c", "enabled": True,
         "has_glm53": False, "budget_exceeded": False,
         "group": ""} for uid in ("9001", "9002", "9003")]},
        ensure_ascii=False, indent=2), encoding="utf-8")
    am._load()


def test_account_group_compact_single_line(client):
    """N-2 紧凑格式（单行 accounts.json）写 group：逗号
    补在 head 末尾分隔前成员与新成员——JSON 合法（无尾
    逗号）、目标落地、其余账号逐字段不变。"""
    c, _, acct_file = client
    # 紧凑格式：整文件一行、条目内无 group 键
    acct_file.write_text(json.dumps({"accounts": [
        {"user_id": "9001", "access_token": "a.b.c"},
        {"user_id": "9002", "access_token": "d.e.f"},
    ]}, ensure_ascii=False), encoding="utf-8")
    r = c.post("/api/accounts/9001/group", json={"group": "free"})
    assert r.status_code == 200, r.get_data(as_text=True)
    # 精确文本断言：逗号在 head 末尾、新成员无尾逗号
    assert _acct_text(acct_file) == (
        '{"accounts": [{"user_id": "9001", "access_token": "a.b.c",'
        '"group": "free"}, {"user_id": "9002", "access_token": "d.e.f"}]}'
    )
    # JSON 合法且目标 group 落地
    accts = _acct_of(acct_file)["accounts"]
    assert next(a for a in accts if a["user_id"] == "9001")["group"] == "free"
    # 其余账号逐字段不变
    assert next(
        a for a in accts if a["user_id"] == "9002") == {
            "user_id": "9002", "access_token": "d.e.f"}
    _restore_am_pool(acct_file)


def test_account_group_compact_user_id_only_entry(client):
    """N-2 紧凑格式条目只有 user_id 一个成员 → 写 group
    成功（head 以引号结尾仍正确补逗号分隔）。"""
    c, _, acct_file = client
    acct_file.write_text('[{"user_id": "9001"}]', encoding="utf-8")
    r = c.post("/api/accounts/9001/group", json={"group": "free"})
    assert r.status_code == 200, r.get_data(as_text=True)
    # JSON 合法（无尾逗号）且 group 落地
    # （紧凑 fixture 是裸数组形态——端点与
    # AccountManager._load 均支持）
    text = _acct_text(acct_file)
    assert text == '[{"user_id": "9001","group": "free"}]'
    accts = _acct_of(acct_file)
    assert next(a for a in accts if a["user_id"] == "9001")["group"] == "free"
    _restore_am_pool(acct_file)
