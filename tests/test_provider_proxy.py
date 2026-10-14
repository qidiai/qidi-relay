# -*- coding: utf-8 -*-
"""每 Provider 代理出口（吸收-1/2 §R-B）测试。

覆盖规格 5 条：
1. 解析四态（http/https/socks5/direct/缺省）+ 全局覆盖优先级
2. Client 按 (name, proxy_url) 隔离；流结束后缓存 Client 存活可用
3. trust_env=False 断言（环境代理变量不生效——"direct" 语义的前提）
4. 坏 proxy_url 走正常错误返回不崩；mtime 变更后 Client 重建并淘汰旧 Client
5. admin 探测 / water 探针路径的代理透传（范围表一致性）

全程零网络：
- "转发"用例打本文件内起的 mock 上游（127.0.0.1:随机端口）；
- "代理真的被传给 httpx" 类断言用假 httpx.Client 记录构造 kwargs
  （不真连代理，否则要起 socks 代理服务器，测试脆弱且与实现无关）。

跑法：py -3.12 -m pytest tests/test_provider_proxy.py -v
"""

import json
import os
import pathlib
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
os.environ.setdefault("CODELY_CLI_VERSION", "0.0.0-test")  # 免 subprocess 探测 CLI 版本

TMP = pathlib.Path(tempfile.mkdtemp(prefix="qidi-provproxy-test-"))
CFG_PATH = TMP / "config.json"
CFG_PATH.write_text(json.dumps({"port": 0, "providers": {}}), encoding="utf-8")
(TMP / "accounts.json").write_text(json.dumps({"accounts": []}), encoding="utf-8")

# ⚠ 路径/上游地址全局**不在 import 时**覆写（约定见 test_stream_sniff.py:305
# 的长注释）：pytest 按文件名收集，import 阶段的覆写"后到者赢"——
# test_stream_failover.py 在 import 时把 RP.LITELLM_API_BASE 覆写为它自己的
# mock 且没有 autouse fixture 还原；本文件（test_provider_* 之后、stream_*
# 之前）若也在 import 时覆写，会把它的用例请求打到本文件的 mock 上而全挂。
# 全部切换放到下面的 module 级 autouse fixture，用例结束原样还原。

import proxy_core.settings as S  # noqa: E402
import proxy_core.accounts as A  # noqa: E402
import proxy_core.stats as ST  # noqa: E402
import proxy_core.water as WA  # noqa: E402
import proxy_core.providers as PV  # noqa: E402

# ─── mock 上游：记录每次请求的 Authorization ──────────────────────

HITS = []


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *args):
        pass

    def _read(self):
        return self.rfile.read(int(self.headers.get("content-length") or 0))

    def do_GET(self):  # noqa: N802
        HITS.append(self.path)
        body = b'{"data": []}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        self._read()
        HITS.append(self.headers.get("authorization", ""))
        body = json.dumps({
            "model": "myp-model",
            "choices": [{"message": {"role": "assistant", "content": "hi"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                      "total_tokens": 2},
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


_srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
_srv.daemon_threads = True
threading.Thread(target=_srv.serve_forever, daemon=True).start()
MOCK = f"http://127.0.0.1:{_srv.server_address[1]}"


class _SSEHandler(BaseHTTPRequestHandler):
    """流式 mock：SSE 200（首帧即内容 → 门判健康）。"""

    protocol_version = "HTTP/1.0"

    def log_message(self, *args):
        pass

    def do_POST(self):  # noqa: N802
        self.rfile.read(int(self.headers.get("content-length") or 0))
        HITS.append(self.headers.get("authorization", ""))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for line in ('data: {"model":"myp-model","choices":[{"delta":{"content":"he"}}]}\n\n',
                     "data: [DONE]\n\n"):
            self.wfile.write(line.encode("utf-8"))
            self.wfile.flush()


_sse_srv = ThreadingHTTPServer(("127.0.0.1", 0), _SSEHandler)
_sse_srv.daemon_threads = True
threading.Thread(target=_sse_srv.serve_forever, daemon=True).start()
MOCK_SSE = f"http://127.0.0.1:{_sse_srv.server_address[1]}"

import routes.proxy as RP  # noqa: E402
import routes.admin as admin_mod  # noqa: E402
from routes.admin import bp as admin_bp  # noqa: E402

from flask import Flask  # noqa: E402

_app = Flask(__name__)
_app.register_blueprint(RP.bp)
_app.register_blueprint(admin_bp)
client = _app.test_client()


# ─── 工具 ────────────────────────────────────────────────────────

_mtime_seq = [time.time() + 1000.0]


def _write_config(providers, requests=None):
    """写 config.json 并把 mtime 前推（保证 _load_providers 判定为变更）。"""
    cfg = {"port": 0, "pool_models": ["GLM-5.3"], "providers": providers}
    if requests is not None:
        cfg["requests"] = requests
    CFG_PATH.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    _mtime_seq[0] += 1.0
    os.utime(CFG_PATH, (_mtime_seq[0], _mtime_seq[0]))
    PV._cache = None          # providers 配置热重载：清缓存即强制重建
    return cfg


def _prov(name="myp", base_url=MOCK, keys=None, models=("myp-model",),
          extra=None):
    p = {"base_url": base_url,
         "api_keys": keys or ["k-1"],
         "models": list(models)}
    p.update(extra or {})
    return {name: p}


@pytest.fixture(scope="module", autouse=True)
def _use_proxy_env():
    """把配置/数据路径与上游地址切到本文件 TMP/mock（用例后原样还原）。

    与 test_stream_sniff.py 同约定：不在 import 时覆写这些全局（收集期
    "后到者赢"会污染 test_stream_failover），只在 fixture 内切换。saved
    在 fixture 建立时抓取，此刻本文件尚未改过任何全局，故还原是有效的。
    """
    saved = (S.BASE_DIR, S.CONFIG_FILE, S.ACCOUNTS_FILE,
             S.STATS_FILE, S.WATER_FILE, S.LOG_FILE,
             A.ACCOUNTS_FILE, ST.STATS_FILE, WA.WATER_FILE,
             PV.CONFIG_FILE, RP.LITELLM_API_BASE, WA.LITELLM_API_BASE)
    S.BASE_DIR = TMP
    S.CONFIG_FILE = CFG_PATH
    S.ACCOUNTS_FILE = TMP / "accounts.json"
    S.STATS_FILE = TMP / "token_stats.json"
    S.WATER_FILE = TMP / "water_check.json"
    S.LOG_FILE = TMP / "proxy.log"
    A.ACCOUNTS_FILE = S.ACCOUNTS_FILE
    ST.STATS_FILE = S.STATS_FILE
    WA.WATER_FILE = S.WATER_FILE
    PV.CONFIG_FILE = S.CONFIG_FILE
    PV._cache = None
    RP.LITELLM_API_BASE = MOCK
    WA.LITELLM_API_BASE = MOCK
    yield
    PV.close_all_clients()
    (S.BASE_DIR, S.CONFIG_FILE, S.ACCOUNTS_FILE,
     S.STATS_FILE, S.WATER_FILE, S.LOG_FILE,
     A.ACCOUNTS_FILE, ST.STATS_FILE, WA.WATER_FILE,
     PV.CONFIG_FILE, RP.LITELLM_API_BASE, WA.LITELLM_API_BASE) = saved
    PV._cache = None


def _reset(providers=None, requests=None):
    HITS.clear()
    RP._prov_quota_cooldown.clear()
    for p in list(PV._clients):
        PV._clients.pop(p, None)
    admin_mod._provider_cache.update(ts=0, data=None)
    return _write_config(providers if providers is not None else {}, requests)


class _FakeResp:
    """假响应：GET /models 与 POST /chat/completions 都够用。"""

    status_code = 200
    headers = {"content-type": "application/json"}
    text = '{"data": [], "model": "m", "choices": [{"message": {"content": "x"}}], "usage": {}}'
    content = text.encode("utf-8")

    def json(self):
        return {"data": [], "model": "m",
                "choices": [{"message": {"content": "x"}}], "usage": {}}

    def read(self):
        return self.content

    def close(self):
        pass


class _RecClient:
    """假 httpx.Client：记录构造 kwargs（proxy/trust_env/timeout）。"""

    RECORDED = []

    def __init__(self, *a, **kw):
        _RecClient.RECORDED.append(kw)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def close(self):
        pass

    def get(self, url, **kw):
        return _FakeResp()

    def post(self, url, **kw):
        return _FakeResp()

    def build_request(self, method, url, **kw):
        return {"method": method, "url": url, **kw}

    def send(self, req, stream=False):
        return _FakeResp()


# ─── ① 解析四态 + 全局覆盖优先级 ─────────────────────────────────

def test_proxy_url_parse_four_states():
    """①parse：http / https / socks5 / direct / 缺省（含空串、非字符串防御）。"""
    assert PV.parse_proxy_url({"proxy_url": "http://127.0.0.1:8080"}) \
        == (True, "http://127.0.0.1:8080")
    assert PV.parse_proxy_url({"proxy_url": "https://user:pw@h:8443"}) \
        == (True, "https://user:pw@h:8443")
    assert PV.parse_proxy_url({"proxy_url": "socks5://127.0.0.1:1080"}) \
        == (True, "socks5://127.0.0.1:1080")
    # direct：显式直连（大小写不敏感、首尾空白容忍）
    assert PV.parse_proxy_url({"proxy_url": "direct"}) == (True, None)
    assert PV.parse_proxy_url({"proxy_url": "  DIRECT  "}) == (True, None)
    # 缺省 / 空串 / 空白 / 非字符串 → 未配置（回退全局）
    assert PV.parse_proxy_url({}) == (False, None)
    assert PV.parse_proxy_url({"proxy_url": ""}) == (False, None)
    assert PV.parse_proxy_url({"proxy_url": "   "}) == (False, None)
    assert PV.parse_proxy_url({"proxy_url": 123}) == (False, None)
    assert PV.parse_proxy_url(None) == (False, None)


def test_global_proxy_fallback_and_priority_chain():
    """①优先级链：per-provider > 全局 > 无；"direct" 覆盖全局。"""
    # 无全局、无 per-provider → 直连
    _reset(_prov("a"))
    assert PV.all_providers()["a"]["proxy_url"] is None
    # 全局设置 → 未配置 per-provider 的 provider 捡起全局
    _reset(_prov("a"), requests={"proxy_url": "socks5://global:1080"})
    assert PV.all_providers()["a"]["proxy_url"] == "socks5://global:1080"
    # per-provider 覆盖全局
    _reset(_prov("a", extra={"proxy_url": "http://per-prov:3128"}),
           requests={"proxy_url": "socks5://global:1080"})
    assert PV.all_providers()["a"]["proxy_url"] == "http://per-prov:3128"
    # "direct" 覆盖全局 → 直连（None）
    _reset(_prov("a", extra={"proxy_url": "direct"}),
           requests={"proxy_url": "socks5://global:1080"})
    assert PV.all_providers()["a"]["proxy_url"] is None
    # 原始 config 条目形态（未解析）同样走优先级链
    assert PV.effective_proxy_url({"proxy_url": "direct"}) is None
    assert PV.effective_proxy_url({"proxy_url": "http://x:1"}) == "http://x:1"
    # 已解析 prov dict（proxy_resolved）不再回退全局——防 "direct" 被
    # 二次解析成"未配置"而错误捡起全局代理
    assert PV.effective_proxy_url(
        {"proxy_url": None, "proxy_resolved": True}) is None
    assert PV.effective_proxy_url(
        {"proxy_url": "http://y:2", "proxy_resolved": True}) == "http://y:2"
    _reset({})


def test_keepalive_setting_defaults_and_off():
    """R-C 配套读取：requests.keepalive_seconds 缺省 15s，0/负=关，坏值回默认。"""
    _write_config({})
    assert S.get_keepalive_seconds() == 15.0
    _write_config({}, requests={"keepalive_seconds": 0})
    assert S.get_keepalive_seconds() == 0.0
    _write_config({}, requests={"keepalive_seconds": -3})
    assert S.get_keepalive_seconds() == 0.0
    _write_config({}, requests={"keepalive_seconds": "7.5"})
    assert S.get_keepalive_seconds() == 7.5
    _write_config({}, requests={"keepalive_seconds": "oops"})
    assert S.get_keepalive_seconds() == 15.0
    _write_config({}, requests={"keepalive_seconds": True})
    assert S.get_keepalive_seconds() == 15.0
    _reset({})


# ─── ② Client 按 (name, proxy_url) 隔离 + 流结束后存活 ──────────

def test_client_isolated_by_name_and_proxy_url():
    """②同一 (name, proxy_url) 复用同一 Client；任一维度不同即隔离；
    全部 trust_env=False。"""
    PV.close_all_clients()
    a = PV.get_client("p1", None)
    assert PV.get_client("p1", None) is a
    b = PV.get_client("p1", "http://x:1")
    c = PV.get_client("p2", None)
    assert b is not a and c is not a and b is not c
    assert set(PV._clients) == {("p1", None), ("p1", "http://x:1"), ("p2", None)}
    for cl in PV._clients.values():
        assert cl.trust_env is False
    PV.close_all_clients()
    assert PV._clients == {}


def test_shared_client_not_closed_by_upstream_open_close():
    """②_UpstreamOpen(client_shared=True).close() 只关 resp 不关 client；
    非共享（主池）仍双关——语义分化正确。"""
    shared = PV.get_client("sharep", None)
    resp_box = []

    class _Resp:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    r1 = _Resp()
    RP._UpstreamOpen(status=200, resp=r1, client=shared,
                     client_shared=True).close()
    assert r1.closed is True
    assert shared.is_closed is False          # 共享 Client 必须活着
    assert PV.get_client("sharep", None) is shared

    owned = PV.get_client("ownp", None)
    r2 = _Resp()
    RP._UpstreamOpen(status=200, resp=r2, client=owned).close()
    assert r2.closed is True
    assert owned.is_closed is True            # 非共享：随流关闭
    PV.close_all_clients()


def test_shared_client_survives_stream_end_and_is_reused():
    """②端到端：外部 provider 流式请求走共享 Client；流结束后该 Client
    仍在缓存、未关闭，且下一次请求复用同一实例（连接池不重建）。"""
    _reset(_prov("myp", base_url=MOCK_SSE, keys=["k-1"]))
    body_req = {"model": "myp-model", "stream": True,
                "messages": [{"role": "user", "content": "hi"}]}
    r1 = client.post("/v1/chat/completions", json=body_req)
    text1 = r1.get_data(as_text=True)
    assert r1.status_code == 200, r1.status_code
    assert "data: [DONE]" in text1
    cached = PV._clients.get(("myp", None))
    assert cached is not None, PV._clients.keys()
    assert cached.is_closed is False, "流结束后共享 Client 不得被关闭"
    # 第二次请求复用同一 Client（pick_key 同 provider 同 proxy → 同键）
    r2 = client.post("/v1/chat/completions", json=body_req)
    assert r2.status_code == 200, r2.status_code
    assert "data: [DONE]" in r2.get_data(as_text=True)
    assert PV._clients.get(("myp", None)) is cached
    assert cached.is_closed is False


def test_nonstream_external_uses_cached_client_without_closing_it():
    """②非流式路径（原 `with httpx.Client()`）同样不关闭共享 Client
    ——否则每个非流式请求都会关掉别的请求正在用的连接池。"""
    _reset(_prov("myp2", base_url=MOCK, keys=["k-1"], models=("myp2-m",)))
    r = client.post("/v1/chat/completions", json={
        "model": "myp2-m", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200, r.status_code
    cached = PV._clients.get(("myp2", None))
    assert cached is not None and cached.is_closed is False
    # 再来一次仍复用
    r2 = client.post("/v1/chat/completions", json={
        "model": "myp2-m", "messages": [{"role": "user", "content": "hi"}]})
    assert r2.status_code == 200
    assert PV._clients.get(("myp2", None)) is cached
    assert cached.is_closed is False


# ─── ③ trust_env=False ──────────────────────────────────────────

def test_trust_env_false_ignores_environment_proxy(monkeypatch):
    """③环境代理变量对按 provider 构造的 Client 不生效：
    HTTP(S)_PROXY / ALL_PROXY 指向黑洞地址，请求仍直达上游。
    若 trust_env 未关，httpx 会把流量塞进 127.0.0.1:1 而必失败。"""
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")
    monkeypatch.delenv("NO_PROXY", raising=False)
    _reset(_prov("envp", base_url=MOCK, keys=["k-1"], models=("envp-m",)))
    c = PV.get_client("envp", None)
    assert c.trust_env is False
    r = c.get(MOCK + "/models")
    assert r.status_code == 200, "trust_env=False 时环境代理必须被忽略"
    # 端到端同样成立
    r2 = client.post("/v1/chat/completions", json={
        "model": "envp-m", "messages": [{"role": "user", "content": "hi"}]})
    assert r2.status_code == 200, r2.status_code


def test_make_client_is_trust_env_false_too(monkeypatch):
    """③探针用的一次性 Client 同样 trust_env=False（同一纪律）。"""
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    with PV.make_client({"proxy_url": "direct"}) as c:
        assert c.trust_env is False


def test_socks5_client_constructs_when_socksio_installed():
    """部署面回归：socks5:// 出口要求 socksio（requirements.txt 的
    httpx[socks] + 启动.bat/deploy.sh 三处同步）。装了才能构造——
    未装时 httpx 抛 ImportError（这正是存量机缺 socksio 的症状）。"""
    pytest.importorskip("socksio")
    c = PV.get_client("socksp", "socks5://127.0.0.1:1080")
    assert c.trust_env is False and c.is_closed is False
    PV.close_all_clients()


# ─── ④ 坏 proxy_url + mtime 重建淘汰 ────────────────────────────

def test_bad_proxy_url_returns_normal_error_not_crash():
    """④坏代理（无可达代理）走既有失败路径：如实 502 + JSON 错误体，
    不抛异常、不炸 500 页。"""
    _reset(_prov("badp", base_url=MOCK, keys=["k-1"], models=("bad-m",),
                 extra={"proxy_url": "http://127.0.0.1:1"}))
    r = client.post("/v1/chat/completions", json={
        "model": "bad-m", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 502, r.status_code
    assert isinstance(r.get_json(), dict) and "error" in r.get_json()
    # 流式同路径
    r2 = client.post("/v1/chat/completions", json={
        "model": "bad-m", "stream": True,
        "messages": [{"role": "user", "content": "hi"}]})
    assert r2.status_code == 502, r2.status_code


def test_mtime_change_rebuilds_and_closes_old_client():
    """④config mtime 变更 → providers 重建时关闭并淘汰旧键的 Client
    （proxy_url 变更 / provider 删除），新键拿到新 Client（防泄漏）。

    注：这里用 http:// 代理构造真实 Client（不依赖 socksio——socks5 的
    构造需要额外依赖，规格测试只要求"解析四态"覆盖 socks5 字面量）。
    """
    old_proxy, new_proxy = "http://127.0.0.1:1080", "http://127.0.0.1:3128"
    _reset(_prov("rp", base_url=MOCK, keys=["k-1"], models=("rp-m",),
                 extra={"proxy_url": old_proxy}))
    PV.all_providers()                       # 触发热重载（写入 proxy_url）
    old = PV.get_client("rp", old_proxy)
    assert old.is_closed is False
    # 改 proxy_url → 旧键失效
    _write_config(_prov("rp", base_url=MOCK, keys=["k-1"], models=("rp-m",),
                        extra={"proxy_url": new_proxy}))
    PV.all_providers()                       # mtime 变更 → 重建 + 淘汰
    assert ("rp", old_proxy) not in PV._clients
    assert old.is_closed is True, "被移除键的旧 Client 必须 close（防泄漏）"
    new = PV.get_client("rp", new_proxy)
    assert new is not old and new.is_closed is False

    # provider 整体删除 → 其 Client 一并淘汰
    _write_config({})
    PV.all_providers()
    assert PV._clients == {}, PV._clients
    assert new.is_closed is True
    _reset({})


# ─── ⑤ admin 探测 / water 探针透传（范围表一致性） ───────────────

def test_admin_probe_and_water_probe_forward_per_provider_proxy(monkeypatch):
    """⑤admin /models 探测、admin 面板订阅探测、water 金丝雀探针
    三处出口都带上同一 per-provider 代理（范围表一致性——不会出现
    "面板探测 403 而转发成功"的自相矛盾 UX），且全部 trust_env=False。"""
    proxy = "socks5://127.0.0.1:1080"
    _reset(_prov("prox", base_url="http://upstream.invalid/v1",
                 keys=["k-1"], models=("prox-m",),
                 extra={"proxy_url": proxy}))
    _RecClient.RECORDED = []
    monkeypatch.setattr(PV.httpx, "Client", _RecClient)   # PV.httpx 即全局 httpx 模块

    # (a) admin 一键探测（GET /models 那一发）
    r = client.post("/api/providers/prox/probe", json={"keyword": ""})
    assert r.status_code == 200, r.status_code
    assert _RecClient.RECORDED, "探测应构造 Client"
    assert _RecClient.RECORDED[0]["proxy"] == proxy
    assert _RecClient.RECORDED[0]["trust_env"] is False

    # (b) admin 面板订阅探测（/api/providers/info）
    _RecClient.RECORDED = []
    admin_mod._provider_cache.update(ts=0, data=None)
    r2 = client.get("/api/providers/info")
    assert r2.status_code == 200, r2.status_code
    assert _RecClient.RECORDED, "面板探测应构造 Client"
    assert _RecClient.RECORDED[0]["proxy"] == proxy
    assert _RecClient.RECORDED[0]["trust_env"] is False

    # (c) water 金丝雀探针（外部 provider 通道）
    _RecClient.RECORDED = []
    prov = dict(PV.all_providers()["prox"])
    prov["api_key"] = "k-1"
    WA.water._probe_one("provider", "prox", prov, "prox-m", lambda a: "k-1")
    assert _RecClient.RECORDED, "金丝雀探针应构造 Client"
    assert _RecClient.RECORDED[0]["proxy"] == proxy
    assert _RecClient.RECORDED[0]["trust_env"] is False


def test_water_pool_probe_stays_direct(monkeypatch):
    """⑤池探针（kind=="pool"）是主池路径，不属外部 provider——保持直连
    （规格显式排除，且池出口若需代理由全局 requests.proxy_url 兜底）。"""
    _reset({})
    _RecClient.RECORDED = []
    monkeypatch.setattr(PV.httpx, "Client", _RecClient)
    acc = {"user_id": "u1"}
    WA.water._probe_one("pool", "u1", acc, "GLM-5.3", lambda a: "key-u1")
    assert _RecClient.RECORDED, "池探针也构造 Client"
    # 池探针不传 proxy（httpx 缺省=直连/吃环境变量口径不变）
    assert _RecClient.RECORDED[0].get("proxy") is None
