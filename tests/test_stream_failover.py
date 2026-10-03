"""流式转发换号 / 状态码透出的回归测试（全程打本地 mock 上游，不碰真实上游）。

跑法：py -3 -m pytest tests -q
"""

import json
import os
import pathlib
import socket
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
os.environ.setdefault("CODELY_CLI_VERSION", "0.0.0-test")  # 免 subprocess 探测 CLI 版本

TMP = pathlib.Path(tempfile.mkdtemp(prefix="qidi-relay-test-"))
(TMP / "config.json").write_text(json.dumps({
    "port": 0,
    "pool_models": ["GLM-5.3", "KIMI-K3"],
    "model_groups": {},
    "providers": {},
    "roles": {},
    "vision_model": "GLM-5.3",
    "vision_capable_models": [],
    "pricing": {},
}), encoding="utf-8")
(TMP / "accounts.json").write_text(json.dumps({"accounts": [
    {"user_id": uid, "username": f"t{uid}", "org_id": "0", "access_token": "a.b.c",
     "enabled": True, "has_glm53": False, "budget_exceeded": False, "group": ""}
    for uid in ("9001", "9002", "9003")]}), encoding="utf-8")

# 路径常量必须在实例化单例（proxy_core.state）之前替换掉，否则会写进真实数据文件
import proxy_core.settings as S  # noqa: E402

S.BASE_DIR = TMP
S.CONFIG_FILE = TMP / "config.json"
S.ACCOUNTS_FILE = TMP / "accounts.json"
S.STATS_FILE = TMP / "token_stats.json"
S.WATER_FILE = TMP / "water_check.json"
S.LOG_FILE = TMP / "proxy.log"

import proxy_core.accounts as A  # noqa: E402
import proxy_core.stats as ST  # noqa: E402
import proxy_core.water as WA  # noqa: E402
import proxy_core.providers as PV  # noqa: E402

A.ACCOUNTS_FILE = S.ACCOUNTS_FILE
ST.STATS_FILE = S.STATS_FILE
WA.WATER_FILE = S.WATER_FILE
PV.CONFIG_FILE = S.CONFIG_FILE

# ─── mock 上游 ───────────────────────────────────────────────────────────────

HITS = []          # 依次记录被命中的账号 key
MODELS = []        # 依次记录上游收到的请求 model 名（守卫改写断言用）
BEHAVIOR = {}      # "key-9001" -> "402" | "200" | "400" | "broken"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *args):
        pass

    def do_POST(self):  # noqa: N802
        raw = self.rfile.read(int(self.headers.get("content-length") or 0))
        req = json.loads(raw.decode("utf-8"))    # 请求体必须仍是合法 JSON
        auth = self.headers.get("authorization", "").removeprefix("Bearer ").strip()
        mode = BEHAVIOR.get(auth, "402")
        HITS.append(auth)
        MODELS.append(req.get("model"))
        if mode == "200":
            if not req.get("stream"):
                # 非流式：普通 chat 完成响应（SSE 文本会让 r.json() 解析失败）
                body = json.dumps({
                    "model": "GLM-5.3",
                    "choices": [{"message": {"role": "assistant", "content": "hello"},
                                 "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 11, "completion_tokens": 2,
                              "total_tokens": 13},
                }).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for line in (
                'data: {"model":"GLM-5.3","choices":[{"delta":{"content":"he"}}]}\n\n',
                'data: {"model":"GLM-5.3","choices":[{"delta":{"content":"llo"}}],'
                '"usage":{"prompt_tokens":11,"completion_tokens":2,"total_tokens":13}}\n\n',
                "data: [DONE]\n\n",
            ):
                self.wfile.write(line.encode("utf-8"))
                self.wfile.flush()
            return
        if mode == "truncated":
            # 声明的 Content-Length 大于实发字节 → httpx 迭代响应体时真的抛错
            payload = b'data: {"model":"GLM-5.3","choices":[{"delta":{"content":"x"}}]}\n\n'
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(payload) + 500))
            self.end_headers()
            self.wfile.write(payload)
            self.wfile.flush()
            self.connection.shutdown(socket.SHUT_WR)
            return
        if mode == "broken":
            # 只吐一个 chunk 就掐线：模拟首字节之后的上游中断
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b'data: {"model":"GLM-5.3","choices":[{"delta":{"content":"x"}}]}\n\n')
            self.wfile.flush()
            self.connection.shutdown(socket.SHUT_WR)
            return
        parts = mode.split("#")           # "<状态码>[#team][#gate]"
        code = int(parts[0])
        body = json.dumps({"error": _error_payload(code, parts[1:])}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if code == 429:
            self.send_header("Retry-After", "1")
        self.end_headers()
        self.wfile.write(body)


def _error_payload(code: int, flags):
    """上游错误体：402 带 team id（②的 team 去重靠它），401#gate 复现签名安全门文案。"""
    if code == 402:
        team = flags[0] if flags else "secret-uuid"
        return {"message": f"Budget has been exceeded! Team={team} "
                           f"Current cost: 9999, Max budget: -1.0",
                "type": "budget_exceeded", "param": None, "code": "402"}
    if code in (401, 403):
        if "gate" in flags:
            return {"message": "由于安全问题，请升级到最新版 Codely "
                               "(Due to a security issue, please upgrade to the latest version)",
                    "type": "auth_error", "param": "x-codely-signature", "code": str(code)}
        if "access" in flags:
            # 复现真实形态：free 组账号的 team 白名单里没有订阅模型
            return {"message": "team not allowed to access model. This team can only "
                               "access models=['alias-only-proxy-models']. Tried to access GLM-5.3",
                    "type": "team_model_access_denied", "param": "model", "code": str(code)}
        return {"message": "No auth credential passed in.",
                "type": "auth_error", "param": None, "code": str(code)}
    if code == 429 and "quota" in flags:
        # 复现 InternAI discovery-api 配额耗尽的真实形态（0929 provider 冷却靠它识别）
        return {"message": "quota exceeded", "type": "quota_exceeded",
                "param": None, "code": "quota_exceeded"}
    return {"message": f"upstream rejects request ({code})",
            "type": "invalid_request_error", "param": None, "code": str(code)}


_srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
_srv.daemon_threads = True
threading.Thread(target=_srv.serve_forever, daemon=True).start()
MOCK = f"http://127.0.0.1:{_srv.server_address[1]}"

import routes.proxy as RP  # noqa: E402
from proxy_core.state import am, api_cache  # noqa: E402
from proxy_core.keystore import session_pool  # noqa: E402

RP.LITELLM_API_BASE = MOCK
WA.LITELLM_API_BASE = MOCK
api_cache.get = lambda acc: f"key-{acc['user_id']}"      # 免向上游换 key

from flask import Flask  # noqa: E402

_app = Flask(__name__)
_app.register_blueprint(RP.bp)
client = _app.test_client()


def _reset(behavior):
    HITS.clear()
    BEHAVIOR.clear()
    BEHAVIOR.update(behavior)
    am.index = 0
    api_cache.cache.clear()
    RP._prov_quota_cooldown.clear()   # 0929：provider 冷却是模块级状态，必须清
    for a in am.accounts:
        a["enabled"] = True
        a["budget_exceeded"] = False
        a["budget_cooldown_until"] = 0
        a["cooldown_reason"] = ""
        a["quota_team"] = ""     # ②的 team 学习结果：不清会污染后续用例的命中次数
        a["group"] = ""          # 分组用例会临时改组，必须还原


def _post(stream=True):
    return client.post("/v1/chat/completions", json={
        "model": "GLM-5.3", "stream": stream,
        "messages": [{"role": "user", "content": "hi"}]})


# ─── 用例 ────────────────────────────────────────────────────────────────────

def test_stream_fails_over_to_healthy_account():
    """两个账号 402：流式应换到可用账号，客户端拿到真实 200 SSE。"""
    _reset({"key-9001": "402", "key-9002": "402", "key-9003": "200"})
    r = _post()
    assert r.status_code == 200, r.status_code
    body = r.get_data(as_text=True)
    assert '"content":"he"' in body and '"content":"llo"' in body
    assert "data: [DONE]" in body
    assert HITS[0] == "key-9001" and HITS[-1] == "key-9003"
    assert "Content-Type" in r.headers and "text/event-stream" in r.headers["Content-Type"]


def test_stream_402_cools_account_without_permanent_disable():
    """402 只进冷却窗口，不得永久置 enabled=False（否则要人工点启用）。"""
    _reset({"key-9001": "402", "key-9002": "200", "key-9003": "200"})
    assert _post().status_code == 200
    a1 = next(a for a in am.accounts if a["user_id"] == "9001")
    assert a1["budget_exceeded"] is True and a1["enabled"] is True
    assert a1["budget_cooldown_until"] > 0


def test_stream_all_accounts_402_returns_real_status_not_200():
    """全池 402：客户端必须看到 402（旧行为是 200 + 空回复导致无限重试）。"""
    _reset({f"key-{u}": "402" for u in ("9001", "9002", "9003")})
    r = _post()
    assert r.status_code == 402, r.status_code
    err = r.get_json()["error"]
    assert isinstance(err, dict) and err["code"] == "upstream_402"
    assert r.headers.get("Retry-After")
    assert len(HITS) == 3
    # 上游内部信息（team id / 实际消费）不外泄给客户端
    assert "secret-uuid" not in r.get_data(as_text=True)
    for a in am.accounts:
        assert a["enabled"] is True


def test_400_does_not_amplify_to_every_account():
    """请求体类错误不换号：只打一次上游（避免对着上游白刷 N 次）。"""
    _reset({f"key-{u}": "400" for u in ("9001", "9002", "9003")})
    r = _post()
    assert r.status_code == 400
    assert len(HITS) == 1


def test_midstream_cut_detected_via_missing_done_sentinel():
    """首字节已出后上游掐线：状态码改不了，但必须识别出截断并在面板标 warn。"""
    from proxy_core.state import activity
    _reset({"key-9001": "broken", "key-9002": "broken", "key-9003": "broken"})
    activity._events.clear()
    r = _post()
    assert r.status_code == 200                  # 首字节已出，只能继续 200
    body = r.get_data(as_text=True)
    assert '"content":"x"' in body
    assert "data: [DONE]" not in body            # 上游确实没吐终止符
    assert any(e.get("status") == "截断" for e in activity.list(50)), \
        "缺 [DONE] 终止符应被识别为截断"


def test_body_error_after_first_byte_uses_inband_frame():
    """已吐字节后 httpx 抛错：状态码无法再改，必须用 in-band 错误事件明确收尾。"""
    _reset({"key-9001": "truncated", "key-9002": "truncated", "key-9003": "truncated"})
    r = _post()
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert '"content":"x"' in body
    assert "proxy_stream_error" in body and body.rstrip().endswith("data: [DONE]")


def test_pool_cooling_down_rejects_with_retry_after():
    """全池冷却中：新请求直接 503 + Retry-After，不再打上游。"""
    _reset({f"key-{u}": "402" for u in ("9001", "9002", "9003")})
    import time as _t
    for a in am.accounts:
        a["budget_exceeded"] = True
        a["budget_cooldown_until"] = _t.time() + 300
    HITS.clear()
    r = _post()
    assert r.status_code == 503
    assert r.get_json()["error"]["code"] == "account_pool_cooling_down"
    assert r.headers.get("Retry-After")
    assert HITS == []


def test_cooldown_expires_and_account_returns_to_pool():
    """冷却到期自动恢复，无需人工干预。"""
    _reset({f"key-{u}": "402" for u in ("9001", "9002", "9003")})
    import time as _t
    a1 = am.accounts[0]
    a1["budget_exceeded"] = True
    a1["budget_cooldown_until"] = _t.time() - 1
    assert am.get_next("GLM-5.3") is not None
    assert a1["budget_exceeded"] is False


def test_mark_cooldown_is_idempotent_and_recovers():
    _reset({})
    import time as _t
    assert am.mark_cooldown("9001", 600, "test") == ["9001"]
    assert am.next_recovery_in() and am.next_recovery_in() > 0
    assert am.get_next("GLM-5.3")["user_id"] != "9001"
    am.accounts[0]["budget_cooldown_until"] = _t.time() - 1
    picked = {am.get_next("GLM-5.3")["user_id"] for _ in range(3)}
    assert "9001" in picked                      # 到期后重新进入候选（游标位置不影响结论）
    assert am.accounts[0]["budget_exceeded"] is False
    assert am.next_recovery_in() is None


# ─── ② team 去重 + 熔断 ──────────────────────────────────────────────────────

def test_team_from_snippet_extracts_quota_team():
    assert RP._team_from_snippet(
        'Budget has been exceeded! Team=a95dcded-7cf6 Current cost: 11492.7') == "a95dcded-7cf6"
    assert RP._team_from_snippet('{"error":"no team here"}') is None
    assert RP._team_from_snippet("") is None


def test_402_same_team_cools_all_accounts_and_stops_probing():
    """同队共享预算：首个请求把 team 学全，之后的请求零上游命中。"""
    _reset({f"key-{u}": "402#team-a" for u in ("9001", "9002", "9003")})
    assert _post().status_code == 402
    assert len(HITS) == 3                        # 学习代价：每队首次各探一次
    assert {a.get("quota_team") for a in am.accounts} == {"team-a"}
    assert all(a["budget_exceeded"] for a in am.accounts)

    HITS.clear()
    r = _post()                                  # 第二个请求不该再打上游
    assert r.status_code == 503
    assert r.get_json()["error"]["code"] == "account_pool_cooling_down"
    assert HITS == []


def test_402_cools_only_the_same_team():
    """同队联动不能牵连无辜：异队账号必须还能被选去服务请求。"""
    _reset({"key-9001": "402#team-a", "key-9002": "200", "key-9003": "200"})
    assert _post().status_code == 200
    by_uid = {a["user_id"]: a for a in am.accounts}
    assert by_uid["9001"]["quota_team"] == "team-a"
    assert by_uid["9001"]["budget_exceeded"] is True
    # 其余账号 team 未知，也没被 9001 的 402 顺带关进冷宫
    assert by_uid["9002"]["quota_team"] == "" and not by_uid["9002"]["budget_exceeded"]


def test_mark_cooldown_fans_out_within_team_only():
    _reset({})
    am.accounts[0]["quota_team"] = "team-a"
    am.accounts[1]["quota_team"] = "team-b"
    cooled = am.mark_cooldown("9003", 600, "配额耗尽", team="team-a")
    assert set(cooled) == {"9001", "9003"}      # 同队联动，异队不受影响
    by_uid = {a["user_id"]: a for a in am.accounts}
    assert not by_uid["9002"]["budget_exceeded"]
    assert by_uid["9003"]["quota_team"] == "team-a"
    # 拿不到 team 时不做任何推断，只冷却这一个账号
    assert am.mark_cooldown("9002", 600, "配额耗尽") == ["9002"]


def test_signature_gate_cools_pool_for_long_window():
    """401 安全门（要求升级客户端）：换号无意义，冷却整池并把窗口放长。"""
    from proxy_core.accounts import AUTH_COOLDOWN_S, QUOTA_COOLDOWN_S
    _reset({f"key-{u}": "401#gate" for u in ("9001", "9002", "9003")})
    r = _post()
    assert r.status_code == 401
    assert len(HITS) == 3
    assert all(a["budget_cooldown_until"] - time.time()
               > QUOTA_COOLDOWN_S for a in am.accounts)
    assert AUTH_COOLDOWN_S > QUOTA_COOLDOWN_S
    assert am.accounts[0]["cooldown_reason"] == "上游安全门拒绝签名"

    HITS.clear()
    assert _post().status_code == 503 and HITS == []   # 窗口内不再重复撞门


def test_plain_401_evicts_cached_key_and_uses_short_window():
    """非安全门的 401 多半是缓存 key 失效：剔除 key + 短冷却，别把账号关 30 分钟。"""
    from proxy_core.accounts import AUTH_COOLDOWN_S
    _reset({"key-9001": "401", "key-9002": "200", "key-9003": "200"})
    api_cache.cache["9001"] = {"key": "stale", "expires": time.time() + 999}
    assert _post().status_code == 200
    assert "9001" not in api_cache.cache
    left = am.accounts[0]["budget_cooldown_until"] - time.time()
    assert left < AUTH_COOLDOWN_S


def test_nonstream_shares_the_same_penalty_rules():
    """非流式与流式共用一套账号判定：客户端会在两种模式间切换。"""
    _reset({"key-9001": "402#team-a", "key-9002": "200", "key-9003": "200"})
    r = _post(stream=False)
    assert r.status_code == 200
    assert am.accounts[0]["budget_exceeded"] is True
    assert am.accounts[0]["quota_team"] == "team-a"
    n = len(HITS)
    _post(stream=False)                          # 冷却窗口内不再重复探测 9001
    assert all(h != "key-9001" for h in HITS[n:])


def test_sessions_released_after_failover():
    """换号与冷却路径不能泄漏 session 占用计数。"""
    _reset({f"key-{u}": "402" for u in ("9001", "9002", "9003")})
    _post()
    assert all(e[1] == 0 for pool in session_pool._pools.values() for e in pool)


# ─── 外部 provider 通道：同一类缺陷（流式假 200）的回归 ──────────────────────

def _use_external_provider(keys, models=("testp-flash",)):
    """把 TMP/config.json 的 providers 段指向 mock 上游（providers 按 mtime 热重载）。"""
    cfg = json.loads((TMP / "config.json").read_text(encoding="utf-8"))
    cfg["providers"] = {"testp": {"base_url": MOCK, "models": list(models),
                                  "api_keys": keys}}
    (TMP / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
    PV._cache = None


def _post_external(stream=True):
    return client.post("/v1/chat/completions", json={
        "model": "testp-flash", "stream": stream,
        "messages": [{"role": "user", "content": "hi"}]})


def test_external_stream_non200_returns_real_status():
    """外部通道上游回 402：客户端要看到 402（旧行为是 200 + 把错误体当 SSE 吐）。"""
    _reset({"pk-1": "402#team-a"})
    _use_external_provider([{"key": "pk-1", "label": "k1"}])
    r = _post_external()
    assert r.status_code == 402, r.get_data(as_text=True)[:200]
    assert r.get_json()["error"]["code"] == "upstream_402"
    assert HITS == ["pk-1"]


def test_external_stream_fails_over_across_keys():
    """换 key 必须发生在吐流之前：第一个 key 被拒后应由第二个 key 正常服务。"""
    _reset({"pk-1": "402#team-a", "pk-2": "200"})
    _use_external_provider([{"key": "pk-1", "label": "k1"},
                            {"key": "pk-2", "label": "k2"}])
    r = _post_external()
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert '"content":"he"' in body and body.rstrip().endswith("data: [DONE]")
    assert HITS == ["pk-1", "pk-2"]


def test_external_stream_shares_truncation_detection():
    """外部通道与主池共用吐流逻辑：缺 [DONE] 终止符同样要识别为截断。"""
    from proxy_core.state import activity
    _reset({"pk-1": "broken"})
    _use_external_provider([{"key": "pk-1", "label": "k1"}])
    activity._events.clear()
    r = _post_external()
    assert '"content":"x"' in r.get_data(as_text=True)   # 必须消费完整条流
    assert any(e.get("status") == "截断" for e in activity.list(50))


def test_external_stream_books_usage_under_key_label(monkeypatch):
    """换 key 后记账要跟着实际服务请求的那个 key 走。"""
    seen = []
    _reset({"pk-1": "402#team-a", "pk-2": "200"})
    _use_external_provider([{"key": "pk-1", "label": "k1"},
                            {"key": "pk-2", "label": "k2"}])
    monkeypatch.setattr(RP.token_stats, "record",
                        lambda uid, model, usage: seen.append((uid, usage["total_tokens"])))
    r = _post_external()
    assert r.status_code == 200
    r.get_data()
    assert seen == [("k2", 13)]


def test_external_load_registry_clean_after_stream():
    """开闸换 key 与吐流结束都不能留下进行中的负载计数。"""
    _reset({"pk-1": "402#team-a", "pk-2": "200"})
    _use_external_provider([{"key": "pk-1", "label": "k1"},
                            {"key": "pk-2", "label": "k2"}])
    r = _post_external()
    assert r.status_code == 200
    r.get_data()
    assert RP.registry.load_of("key:testp|k1") == 0
    assert RP.registry.load_of("key:testp|k2") == 0


# ─── 模型权限错误（0829/0830 整池 503 事故的成因）─────────────────────────────

def test_model_access_denied_fails_over_but_never_cools():
    """team 白名单外模型（全池都无权）：打满换号尝试后如实 401，且不冷却账号。

    0929 语义变更：各账号 team 权限已分化（<账号ID>/<账号ID> 实测），access_denied
    也换号再试（≤MAX_ACCOUNT_ATTEMPTS 兜底）；但依然不是账号故障——冷却会
    复现 0829/0830 整池 503 事故，绝不允许。
    """
    _reset({f"key-{u}": "401#access" for u in ("9001", "9002", "9003")})
    r = _post()
    assert r.status_code == 401, r.status_code
    assert len(HITS) == 3, HITS
    assert r.get_json()["error"]["code"] == "upstream_401"
    for a in am.accounts:
        assert a["budget_exceeded"] is False and a["enabled"] is True
        assert a["cooldown_reason"] == ""


def test_access_denied_leaves_account_selectable_next_time():
    """权限错误不留后遗症：下一个请求照样能用同一个账号打上游。"""
    _reset({f"key-{u}": "401#access" for u in ("9001", "9002", "9003")})
    assert _post().status_code == 401
    first = list(HITS)
    assert _post().status_code == 401
    assert len(HITS) == len(first) + 3


def test_signature_gate_still_cools_but_access_denied_does_not():
    """两种 401 必须区分对待：签名安全门冷却，权限拒绝不冷却。"""
    _reset({"key-9001": "401#gate", "key-9002": "200", "key-9003": "200"})
    assert _post().status_code == 200
    cooled = [a["user_id"] for a in am.accounts if a["budget_exceeded"]]
    assert cooled == ["9001"]
    _reset({f"key-{u}": "401#access" for u in ("9001", "9002", "9003")})
    assert _post().status_code == 401
    assert [a["user_id"] for a in am.accounts if a["budget_exceeded"]] == []


def test_subscription_model_does_not_fall_back_to_free_group():
    """无可用订阅账号时不回退 free 组：零上游请求，直接 503 + Retry-After。"""
    _reset({f"key-{u}": "200" for u in ("9001", "9002", "9003")})
    by_id = {a["user_id"]: a for a in am.accounts}
    by_id["9001"]["group"] = "free"          # 无订阅，请求 GLM-5.3 必被上游 401
    am.mark_cooldown("9002", 600, "配额耗尽")
    am.mark_cooldown("9003", 600, "配额耗尽")
    r = _post()
    assert r.status_code == 503, r.status_code
    assert "cooling" in r.get_data(as_text=True)
    assert r.headers.get("Retry-After")
    assert HITS == []                        # 一次上游都不该打


def test_pool_without_ungrouped_accounts_keeps_fallback():
    """整池都配了分组时（无未分组账号），回退全池的旧行为要保留。"""
    _reset({f"key-{u}": "200" for u in ("9001", "9002", "9003")})
    for a in am.accounts:
        a["group"] = "free"
    assert _post().status_code == 200
    assert HITS == ["key-9001"]


def test_nonstream_access_denied_tries_all_accounts_without_cooling():
    """非流式共用同一条 failover 判定（0929：access_denied 也换号，不冷却）。"""
    _reset({f"key-{u}": "401#access" for u in ("9001", "9002", "9003")})
    r = _post(stream=False)
    assert r.status_code == 401, r.status_code
    assert len(HITS) == 3, HITS
    assert all(not a["budget_exceeded"] for a in am.accounts)


# ─── Anthropic Messages API 适配端点 ─────────────────────────────────────────


def test_anthropic_messages_nonstream_uses_shared_proxy_chain(monkeypatch):
    """/v1/messages 非流式：请求转 OpenAI，响应转回 Anthropic，仍复用 _proxy_chat。"""
    from flask import jsonify
    seen = {}

    def fake_proxy(data):
        seen.update(data)
        return jsonify({
            "id": "chatcmpl-test",
            "model": data["model"],
            "choices": [{"finish_reason": "stop", "message": {
                "role": "assistant", "content": "已完成"}}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 7,
                      "total_tokens": 18},
        })

    monkeypatch.setattr(RP, "_proxy_chat", fake_proxy)
    r = client.post("/v1/messages", json={
        "model": "GLM-5.3",
        "system": "你是助手",
        "messages": [{"role": "user", "content": [{"type": "text", "text": "你好"}]}],
        "max_tokens": 128,
    })
    assert r.status_code == 200, r.get_data(as_text=True)
    assert seen["messages"] == [
        {"role": "system", "content": "你是助手"},
        {"role": "user", "content": [{"type": "text", "text": "你好"}]},
    ]
    body = r.get_json()
    assert body["type"] == "message"
    assert body["content"] == [{"type": "text", "text": "已完成"}]
    assert body["stop_reason"] == "end_turn"
    assert body["usage"] == {"input_tokens": 11, "output_tokens": 7}


def test_anthropic_messages_stream_bridges_openai_sse(monkeypatch):
    """/v1/messages 流式：OpenAI data-only SSE 桥成 Anthropic event/data SSE。"""
    def fake_proxy(data):
        assert data["stream"] is True
        chunks = [
            'data: {"choices":[{"delta":{"content":"你"}}]}\n\n'.encode("utf-8"),
            'data: {"choices":[{"delta":{"content":"好"}}],'
            '"usage":{"prompt_tokens":2,"completion_tokens":5}}\n\n'.encode("utf-8"),
            b'data: {"choices":[{"finish_reason":"stop"}]}\n\n',
            b'data: [DONE]\n\n',
        ]
        return RP.Response(iter(chunks), content_type="text/event-stream")

    monkeypatch.setattr(RP, "_proxy_chat", fake_proxy)
    r = client.post("/v1/messages", json={
        "model": "GLM-5.3", "stream": True,
        "messages": [{"role": "user", "content": "你好"}],
        "max_tokens": 128,
    })
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "event: message_start" in body
    assert '"type": "text_delta"' in body and "你" in body and "好" in body
    assert '"stop_reason": "end_turn"' in body
    assert '"output_tokens": 5' in body
    assert body.rstrip().endswith('data: {"type": "message_stop"}')


def test_anthropic_count_tokens_stays_local_and_never_calls_upstream():
    """/v1/messages/count_tokens：本地估算、零上游命中（客户端每轮都调，不能烧配额）。"""
    _reset({f"key-{u}": "200" for u in ("9001", "9002", "9003")})
    r = client.post("/v1/messages/count_tokens", json={
        "model": "GLM-5.3",
        "system": "你是助手",
        "messages": [{"role": "user",
                      "content": [{"type": "text", "text": "你好"}]}],
    })
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    assert list(body) == ["input_tokens"]
    assert isinstance(body["input_tokens"], int) and body["input_tokens"] > 0
    assert HITS == []                              # 绝不为了计数打上游

    bad = client.post("/v1/messages/count_tokens", json={"model": "GLM-5.3"})
    assert bad.status_code == 400
    assert bad.get_json()["error"]["type"] == "invalid_request_error"
    assert HITS == []


def test_anthropic_endpoints_reject_oversized_body_with_413(monkeypatch):
    """Anthropic 两个入口的请求体上限：超限按协议口径拒，不许退化成"缺参数"400。

    count_tokens 的估算成本与体积成正比，超限请求又必然被上游拒；先把体积挡住，
    才不至于让匿名客户端用一条大请求占住核心。
    """
    assert RP.ANTHROPIC_MAX_BODY_BYTES == 32 * 1024 * 1024   # 对齐官方 Messages 上限
    monkeypatch.setattr(RP, "ANTHROPIC_MAX_BODY_BYTES", 4096)
    _reset({f"key-{u}": "200" for u in ("9001", "9002", "9003")})

    big = {"model": "GLM-5.3", "max_tokens": 8,
           "messages": [{"role": "user", "content": [{"type": "text",
                                                      "text": "x" * 9000}]}]}
    for path in ("/v1/messages/count_tokens", "/v1/messages"):
        r = client.post(path, json=big)
        assert r.status_code == 413, (path, r.status_code, r.get_data(as_text=True)[:200])
        err = r.get_json()["error"]
        assert err["type"] == "invalid_request_error"
        # 上限文案按 MB 报，不足 1MB 也要说 1MB——整除会把亚 MB 上限打印成 "0MB"
        assert "exceeds the 1MB limit" in err["message"], err["message"]
    assert HITS == []                                       # 拒在打上游之前

    ok = client.post("/v1/messages/count_tokens", json={
        "model": "GLM-5.3",
        "messages": [{"role": "user", "content": "你好"}]})
    assert ok.status_code == 200, ok.get_data(as_text=True)
    assert ok.get_json()["input_tokens"] > 0


def _chunked_post(path: str, body: bytes):
    """按真·chunked 口径发请求：有 wsgi.input_terminated、没有可用 Content-Length。

    测试客户端默认把整个 body 折算成 Content-Length，而 max_content_length 的预检
    只看那一个头——chunked 走的是完全不同的分支（LimitedStream 静默截到上限后
    就停，不抛 413），必须照 werkzeug.serving 的做法手工搭 environ 才覆盖得到。
    返回 (响应, 源流)：源流的 tell() 就是应用真正读走的字节数。
    """
    from io import BytesIO

    src = BytesIO(body)
    return client.open(
        path, method="POST",
        headers={"Content-Type": "application/json"},
        environ_overrides={
            "wsgi.input": src,
            "wsgi.input_terminated": True,
            "HTTP_TRANSFER_ENCODING": "chunked",
            "CONTENT_LENGTH": "",
        }), src


class _JsonSpy:
    """记录请求体到底有没有被解析过。

    "超限就拒在解析之前"是这条上限的意义所在（解析成本与体积成正比），
    而状态码、报文、读取字节数在全绿的前提下都挡得住"先解析再判体积"的写法。
    只在测试期间替换 routes.proxy 模块里的 json 名字，monkeypatch 会还原。
    """

    def __init__(self):
        self.parsed = []

    def loads(self, s, *a, **kw):
        self.parsed.append(len(s) if hasattr(s, "__len__") else -1)
        return json.loads(s, *a, **kw)

    def dumps(self, *a, **kw):
        return json.dumps(*a, **kw)


def test_anthropic_chunked_body_over_limit_gets_413_not_misleading_400(monkeypatch):
    """chunked 超限必须是 413：werkzeug 只静默截断，截残的 JSON 会被当成"缺参数"。

    同时盯住成本上限本身：413 报得对不代表读得少。超限的意义上界是"只读到
    cap+1 就收手、且一个字节都不解析"，所以必须量应用真正从流里取走了多少字节
    ——把上限设掉、整个 body 吸进内存后再报 413 的实现，光看状态码和报文是全绿的。
    """
    monkeypatch.setattr(RP, "ANTHROPIC_MAX_BODY_BYTES", 4096)
    _reset({f"key-{u}": "200" for u in ("9001", "9002", "9003")})
    parsed = _JsonSpy()
    monkeypatch.setattr(RP, "json", parsed)

    seen = []
    real = RP._anthropic_too_large

    def spy(actual):
        seen.append((RP.request.content_length, actual))
        return real(actual)

    monkeypatch.setattr(RP, "_anthropic_too_large", spy)

    big = json.dumps({"model": "GLM-5.3", "max_tokens": 8,
                      "messages": [{"role": "user",
                                    "content": [{"type": "text",
                                                 "text": "x" * 200_000}]}]}
                     ).encode()
    assert len(big) > 4096
    r, src = _chunked_post("/v1/messages/count_tokens", big)
    assert r.status_code == 413, r.get_data(as_text=True)[:200]
    assert r.get_json()["error"]["type"] == "invalid_request_error"
    # (None, 4097)：长度未知且读到了 cap+1（多给的那 1 字节就是判据本身）——
    # 证明走的是体积判定分支，而不是被测试客户端偷偷加上 Content-Length 后命中预检
    assert seen == [(None, 4097)], seen
    # 读走 cap+1 就收手：一条超限请求的内存与解析成本被封在上限里
    assert 4096 < src.tell() <= 4097, src.tell()
    assert parsed.parsed == [], parsed.parsed          # 一个字节都没解析

    # 恰好等于上限的请求必须放行：读满 cap 不等于越界，
    # 用 >= 会把这条合法的边界请求误拒成 413
    exact = {"model": "GLM-5.3", "messages": [{"role": "user", "content": ""}]}
    exact["messages"][0]["content"] = "y" * (4096 - len(json.dumps(exact).encode()))
    body = json.dumps(exact).encode()
    assert len(body) == 4096, len(body)
    monkeypatch.setattr(RP, "_proxy_chat", _stub_chat({}))   # /v1/messages 不打上游
    for path in ("/v1/messages/count_tokens", "/v1/messages"):
        r, src = _chunked_post(path, body)
        assert r.status_code == 200, (path, r.status_code,
                                      r.get_data(as_text=True)[:120])
        assert src.tell() == 4096, (path, src.tell())
        r = client.post(path, data=body, content_type="application/json")
        assert r.status_code == 200, (path, r.status_code,
                                      r.get_data(as_text=True)[:120])
    assert seen == [(None, 4097)], seen                  # 边界请求没有再触发 413

    # 没超限的 chunked 请求照常放行（否则等于把 chunked 一刀切拒了）
    small = json.dumps({"model": "GLM-5.3",
                        "messages": [{"role": "user", "content": "你好"}]}).encode()
    ok, src = _chunked_post("/v1/messages/count_tokens", small)
    assert ok.status_code == 200, ok.get_data(as_text=True)
    assert ok.get_json()["input_tokens"] > 0
    assert src.tell() == len(small)
    assert HITS == []
    # 探针自检：放行过的 5 条请求（4 条恰好等于上限 + 1 条小的）确实各解析一次。
    # 少了这条，"超限那条没解析"可能只是因为探针挂错了对象。
    assert parsed.parsed == [4096, 4096, 4096, 4096, len(small)], parsed.parsed


def test_anthropic_real_32mib_cap_enforces_read_ceiling_not_just_wording(monkeypatch):
    """真实帽值（32MiB）必须亲自跑一次：缩小帽只验过"1MB"文案与 4097 这个数。

    全套体积用例都把 ANTHROPIC_MAX_BODY_BYTES 改成 4096，于是"读上限跟着帽值走"
    这件事从没在真实数量级上被执行过——把预检额度悄悄换成 1GiB+1 的实现，超限照样
    报 413、文案照样写 32MB，全绿，但一条 8GiB 的 chunked 请求会被整个吸进内存。
    这里用真帽值，且断言的是"读走多少字节"而不是状态码。
    """
    cap = RP.ANTHROPIC_MAX_BODY_BYTES
    assert cap == 32 * 1024 * 1024, cap
    _reset({f"key-{u}": "200" for u in ("9001", "9002", "9003")})
    parsed = _JsonSpy()
    monkeypatch.setattr(RP, "json", parsed)
    seen = []
    real = RP._anthropic_too_large

    def spy(actual):
        seen.append((RP.request.content_length, actual))
        return real(actual)

    monkeypatch.setattr(RP, "_anthropic_too_large", spy)

    big = json.dumps({"model": "GLM-5.3",
                      "messages": [{"role": "user",
                                    "content": "x" * (cap + 64 * 1024)}]}).encode()
    assert len(big) > cap + 4096, len(big)

    # chunked：长度未知，只能靠"读到 cap+1 就收手"来封顶成本
    r, src = _chunked_post("/v1/messages/count_tokens", big)
    assert r.status_code == 413, r.get_data(as_text=True)[:200]
    err = r.get_json()["error"]
    assert err["type"] == "invalid_request_error"
    assert "exceeds the 32MB limit" in err["message"], err["message"]
    assert seen == [(None, cap + 1)], seen
    assert src.tell() == cap + 1, src.tell()        # 一个字节都没多读
    assert parsed.parsed == [], parsed.parsed       # 也没解析过

    # Content-Length：声明长度在预检就够判，正文一个字节都不该读
    seen.clear()
    r = client.post("/v1/messages/count_tokens", data=big, content_type="application/json")
    assert r.status_code == 413, r.get_data(as_text=True)[:200]
    assert seen == [(len(big), len(big))], seen     # 报的是声明长度，非实际读取长度
    assert parsed.parsed == [], parsed.parsed
    assert HITS == []


def test_anthropic_chunked_over_limit_on_a_real_http_server(monkeypatch):
    """真 HTTP 服务器上再来一次：手注 environ 绕过了 werkzeug 自己的分帧解码。

    上面的 chunked 用例把 BytesIO 直接塞进 wsgi.input，等于是"我替服务器造了一个
    它恰好会造出来的流"。真正的路径是 serving 层把 Transfer-Encoding: chunked 解成
    DechunkedInput 再交给 LimitedStream，只有过一遍真 socket 才碰到那层代码。
    """
    import http.client
    from werkzeug.serving import make_server

    monkeypatch.setattr(RP, "ANTHROPIC_MAX_BODY_BYTES", 4096)
    _reset({f"key-{u}": "200" for u in ("9001", "9002", "9003")})
    srv = make_server("127.0.0.1", 0, _app, threaded=True)
    port = srv.server_address[1]
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()

    def send(body: bytes):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
        try:
            c.putrequest("POST", "/v1/messages/count_tokens")
            c.putheader("Transfer-Encoding", "chunked")
            c.putheader("Content-Type", "application/json")
            c.endheaders()
            for i in range(0, len(body), 512):           # 小帧，逼它真的循环解帧
                c.send(b"%x\r\n" % len(body[i:i + 512]) + body[i:i + 512] + b"\r\n")
            c.send(b"0\r\n\r\n")
            r = c.getresponse()
            return r.status, r.getheader("Content-Type"), r.read()
        finally:
            c.close()

    try:
        big = json.dumps({"model": "GLM-5.3",
                          "messages": [{"role": "user", "content": "z" * 20_000}]}).encode()
        assert len(big) > 4096
        status, ctype, payload = send(big)
        assert status == 413, (status, payload[:160])
        assert "json" in (ctype or ""), ctype
        assert json.loads(payload)["error"]["type"] == "invalid_request_error"

        small = json.dumps({"model": "GLM-5.3",
                            "messages": [{"role": "user", "content": "你好"}]}).encode()
        status, ctype, payload = send(small)
        assert status == 200, (status, payload[:160])
        assert json.loads(payload)["input_tokens"] > 0
        assert HITS == []
    finally:
        srv.shutdown()
        srv.server_close()
        th.join(timeout=5)


class _DyingStream:
    """读到 after 字节就 EOF 的流：客户端声明了 Content-Length 却提前断开。

    werkzeug 的 LimitedStream 在没读满声明长度时收到 0 字节会抛 ClientDisconnected
    （一个 400 的 HTTPException）。读取兜底一旦被删或缩窄，这个异常就会逃到
    Flask 的错误处理，客户端拿到的是通用 text/html 400 页，不再是协议形状的
    错误体——两条路径的差别只在响应内容上，状态码都是 400。
    """

    def __init__(self, data: bytes, after: int):
        self._data, self._after, self._pos = data, after, 0

    def readable(self) -> bool:
        return True

    def read(self, size=-1) -> bytes:
        if size is None or size < 0:
            size = len(self._data) - self._pos
        end = min(self._pos + size, self._after)
        chunk = self._data[self._pos:end]
        self._pos = end
        return chunk

    def readinto(self, buf) -> int:
        chunk = self.read(len(buf))
        buf[:len(chunk)] = chunk
        return len(chunk)


def test_anthropic_client_disconnect_mid_body_stays_json_400():
    """半路断开的请求体：仍是协议口径的 400 JSON，不能逃成 Flask 的 HTML 错误页。"""
    _reset({f"key-{u}": "200" for u in ("9001", "9002", "9003")})
    first_required = {"/v1/messages": "model", "/v1/messages/count_tokens": "messages"}
    body = json.dumps({"model": "GLM-5.3",
                       "messages": [{"role": "user", "content": "你好"}]}).encode()
    for path, param in first_required.items():
        r = client.open(
            path, method="POST",
            headers={"Content-Type": "application/json"},
            environ_overrides={
                "wsgi.input": _DyingStream(body, len(body) // 2),
                "CONTENT_LENGTH": str(len(body)),
            })
        assert r.status_code == 400, (path, r.status_code,
                                      r.get_data(as_text=True)[:200])
        assert "text/html" not in (r.content_type or ""), r.get_data(as_text=True)[:200]
        payload = r.get_json(silent=True)
        assert payload, (path, r.content_type, r.get_data(as_text=True)[:200])
        assert payload["error"]["type"] == "invalid_request_error"
        assert f"Missing required parameter: {param}" in payload["error"]["message"]
    assert HITS == []


def test_anthropic_endpoints_reject_non_object_json_bodies():
    """请求体是合法 JSON 但不是对象（null/[]/标量）：按缺参数拒，不许炸成 HTML 500。

    get_json(force=True) 对 "null" 是正常返回 None，不是异常——没有 isinstance
    兜底时视图里的 data.get(...) 会直接 AttributeError，客户端拿到 500 HTML 页。

    这里必须同时盯住"拒绝的理由"和"合法对象照常放行"：只断言 400 的话，
    任何把请求体一律归一成空对象的变异都是绿的。
    """
    _reset({f"key-{u}": "200" for u in ("9001", "9002", "9003")})
    # 两个入口先查的参数不同（/v1/messages 先 model，count_tokens 先 messages）
    first_required = {"/v1/messages": "model", "/v1/messages/count_tokens": "messages"}
    # 合法但不是对象（null/[]/标量）+ 根本无法解析（空体/纯空白/截断的 JSON）：
    # 两类都必须归成 400 JSON。少了后一类，把 json.loads 的兜底从
    # except Exception 缩成任何一个具体异常类型都是绿的，而线上会回 500 HTML 页。
    bodies = ("null", "[]", '"just-a-string"', "5",
              "", "   ", "[1,", '{"a":[1,', "\xff\xfe not json at all")
    for body in bodies:
        for path, param in first_required.items():
            r = client.post(path, data=body.encode("latin-1"),
                            content_type="application/json")
            assert r.status_code == 400, (body, path, r.status_code,
                                          r.get_data(as_text=True)[:200])
            err = r.get_json()["error"]
            assert err["type"] == "invalid_request_error"
            assert f"Missing required parameter: {param}" in err["message"], err["message"]
            assert "text/html" not in (r.content_type or "")
    assert HITS == []

    # 对照组：合法对象请求体照常解析（否则上面的全 400 毫无信息量）
    ok = client.post("/v1/messages/count_tokens", json={
        "model": "GLM-5.3",
        "messages": [{"role": "user", "content": "你好"}]})
    assert ok.status_code == 200, ok.get_data(as_text=True)
    assert ok.get_json()["input_tokens"] > 0


def _stub_chat(seen):
    """替换 _proxy_chat：只记录转发体与模型名，不打上游。"""
    from flask import jsonify

    def fake_proxy(data):
        seen.update(data)
        return jsonify({
            "id": "chatcmpl-alias", "model": data["model"],
            "choices": [{"finish_reason": "stop",
                         "message": {"role": "assistant", "content": "已完成"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        })

    return fake_proxy


def test_anthropic_messages_model_alias_is_config_driven(monkeypatch):
    """Anthropic 端点型号名别名：config 收编才改写（精确项优先），响应回显原名。"""
    seen = {}
    monkeypatch.setattr(RP, "_proxy_chat", _stub_chat(seen))
    monkeypatch.setattr(RP, "load_config", lambda: {"anthropic_model_aliases": {
        "claude-3-5-haiku-20241022": "cline-free/mimo-v2.6-flash",
        "claude-*": "cline-free/deepseek-v4.1-flash",
    }})
    req = {"max_tokens": 64, "messages": [{"role": "user", "content": "hi"}]}

    r = client.post("/v1/messages", json=dict(req, model="claude-sonnet-4-5-20250929"))
    assert r.status_code == 200, r.get_data(as_text=True)
    assert seen["model"] == "cline-free/deepseek-v4.1-flash"
    assert r.get_json()["model"] == "claude-sonnet-4-5-20250929"    # 回显客户端请求名

    r = client.post("/v1/messages", json=dict(req, model="claude-3-5-haiku-20241022"))
    assert r.status_code == 200
    assert seen["model"] == "cline-free/mimo-v2.6-flash"            # 精确项先于通配


def test_anthropic_messages_without_alias_config_keeps_client_model(monkeypatch):
    """没配别名时不猜模型名：原样透传，未知模型仍交给模型名守卫 404（不静默假活）。"""
    seen = {}
    monkeypatch.setattr(RP, "_proxy_chat", _stub_chat(seen))
    monkeypatch.setattr(RP, "load_config", lambda: {})
    r = client.post("/v1/messages", json={
        "model": "claude-sonnet-4-5-20250929", "max_tokens": 64,
        "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert seen["model"] == "claude-sonnet-4-5-20250929"
    assert r.get_json()["model"] == "claude-sonnet-4-5-20250929"


def test_anthropic_messages_unknown_model_returns_404_not_500():
    """模型名守卫的 404 必须原样变成 Anthropic 错误体（旧实现 500 + HTML 页）。"""
    _reset({f"key-{u}": "200" for u in ("9001", "9002", "9003")})
    for stream in (False, True):
        r = client.post("/v1/messages", json={
            "model": "gpt-9-turbo-20990101", "max_tokens": 16, "stream": stream,
            "messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 404, (stream, r.status_code,
                                      r.get_data(as_text=True)[:200])
        assert r.get_json()["error"]["type"] == "invalid_request_error"
        assert "does not exist" in r.get_data(as_text=True)
    assert HITS == []                       # 未知模型绝不打上游


def test_responses_unknown_model_returns_404_not_500():
    """/v1/responses 的守卫分支同样要 404 JSON：元组被当 Response 用会 500/断链。"""
    _reset({f"key-{u}": "200" for u in ("9001", "9002", "9003")})
    for stream in (False, True):
        r = client.post("/v1/responses", json={
            "model": "gpt-9-turbo-20990101", "input": "hi", "stream": stream})
        assert r.status_code == 404, (stream, r.status_code,
                                      r.get_data(as_text=True)[:200])
        assert r.get_json()["error"]["code"] == "model_not_found"
    assert HITS == []


# ─── 0929 修复：混合权限换号 / 池模型大小写规范化 / provider 配额冷却 ─────────


def test_access_denied_fails_over_to_authorized_account():
    """混合权限池：部分账号无该模型权限时应换号，命中可用账号后整单成功。

    0929 前旧行为：access_denied 判 failover=False，单次尝试直接 401——而实况
    是上游 team 权限分化（<账号ID> 无 DeepSeek-V4.1-Flash 权限、<账号ID> 有），
    旧行为导致 DeepSeek-V4.1-Flash 时好时坏、约一半请求整单失败。
    """
    _reset({"key-9001": "401#access", "key-9002": "200", "key-9003": "200"})
    r = _post()
    assert r.status_code == 200, r.status_code
    assert HITS[0] == "key-9001" and len(HITS) == 2 and "key-9001" not in HITS[1:], HITS
    # 换号精确避开刚失败账号：恰好两次命中且不重复撞 9001（9002/9003 任一均可）
    for a in am.accounts:
        assert a["budget_exceeded"] is False and a["enabled"] is True


def test_pool_model_case_variant_canonicalized_not_rewritten():
    """池模型大小写变体（kimi-k3）应规范化为 KIMI-K3 进池，不得改写 codely-core。

    0929 前的旧行为：白名单大小写不敏感放行、池判断却精确匹配，小写变体落进
    codely-core 改写分支——客户端拿到 200 但由 GLM 代答（静默假活，deepseek-v4.1-flash
    被调包事故即由此而来）。
    """
    _reset({f"key-{u}": "200" for u in ("9001", "9002", "9003")})
    MODELS.clear()
    r = client.post("/v1/chat/completions", json={
        "model": "kimi-k3", "stream": False,
        "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200, r.status_code
    assert MODELS and MODELS[-1] == "KIMI-K3", MODELS


def test_external_provider_quota_cooldown():
    """外部 provider 全 key 429 quota：置 provider 冷却，窗口内快速失败不打上游。

    背景：intern 通道 10 key 配额被打穿后客户端每 ~100s 重试，旧行为每笔都
    串行打满全部 key（整夜 3.7 万次无效上游请求 + 50MB 日志）。
    """
    cfg = json.loads(S.CONFIG_FILE.read_text(encoding="utf-8"))
    old_provs = cfg.get("providers", {})
    try:
        cfg["providers"] = {"mockprov": {
            "base_url": MOCK, "models": ["mp-model"],
            "api_keys": [{"key": "kA", "label": "kA"},
                         {"key": "kB", "label": "kB"}]}}
        S.CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")

        def _post_mp():
            return client.post("/v1/chat/completions", json={
                "model": "mp-model", "stream": False,
                "messages": [{"role": "user", "content": "hi"}]})

        _reset({"kA": "429#quota", "kB": "429#quota"})
        r1 = _post_mp()
        assert r1.status_code == 429, r1.status_code
        assert HITS == ["kA", "kB"], HITS        # 两把 key 各打一次后如实 429
        r2 = _post_mp()
        assert r2.status_code == 503, r2.status_code
        assert r2.headers.get("Retry-After")
        assert len(HITS) == 2                    # 冷却窗内零上游请求
        RP._prov_quota_cooldown["mockprov"] = time.time() - 1   # 模拟窗口过期
        BEHAVIOR.update({"kA": "200", "kB": "200"})
        assert _post_mp().status_code == 200
        assert "mockprov" not in RP._prov_quota_cooldown   # 成功即解除冷却
    finally:
        cfg["providers"] = old_provs
        S.CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        RP._prov_quota_cooldown.pop("mockprov", None)
