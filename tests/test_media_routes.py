# -*- coding: utf-8 -*-
"""routes/media 媒体透传路由测试（零网络，monkeypatch httpx）。

覆盖 6 条：
1. 无 api_key → 401（与 chat 一致）
2. 正常转发：body 原样到达上游、响应原样回传（含 status 201/400 等）
3. 上游 429 → 换下一把 key 重试，第二把成功则返回 200
4. 所有 key 429 → 本端返回 429 + key_rate_limited + Retry-After
5. 未知模型 → 404/400（与 chat 的 model_not_found 风格一致）
6. /agnesapi 的 query 透传（video_id、model_name 原样带上）

跑法：py -3.12 -m pytest tests/test_media_routes.py -v
"""

import json
import pathlib
import sys
import tempfile
import threading

import pytest

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

# ─── 路径重定向（必须在 import routes 之前）─────────────────────
_TMP = pathlib.Path(tempfile.mkdtemp(prefix="qidi-media-test-"))
(_TMP / "config.json").write_text(json.dumps({
    "port": 0,
    "api_keys": ["test-client-key-12345"],
    "providers": {},
}), encoding="utf-8")
(_TMP / "accounts.json").write_text(json.dumps({"accounts": []}), encoding="utf-8")

import proxy_core.settings as _S  # noqa: E402

_S.BASE_DIR = _TMP
_S.CONFIG_FILE = _TMP / "config.json"
_S.ACCOUNTS_FILE = _TMP / "accounts.json"
_S.STATS_FILE = _TMP / "token_stats.json"
_S.WATER_FILE = _TMP / "water_check.json"
_S.LOG_FILE = _TMP / "proxy.log"

import proxy_core.providers as _PV  # noqa: E402
_PV.CONFIG_FILE = _S.CONFIG_FILE

import routes.proxy as RP  # noqa: E402
import routes.media as MEDIA  # noqa: E402
from proxy_core.key_scheduler import KeyScheduler  # noqa: E402

from flask import Flask  # noqa: E402


# ─── Fake httpx Client ─────────────────────────────────────────────

class _FakeResp:
    def __init__(self, status_code: int, body: dict = None,
                 content_type: str = "application/json",
                 upstream_text: str = None):
        self.status_code = status_code
        self._body = body if body is not None else {}
        self.headers = {"Content-Type": content_type}
        self._text = (upstream_text
                      if upstream_text is not None
                      else json.dumps(self._body, ensure_ascii=False))
        self._content = self._text.encode("utf-8")
        self._closed = False

    @property
    def content(self) -> bytes:
        return self._content

    @property
    def text(self) -> str:
        return self._text

    def json(self):
        return json.loads(self._text)

    def close(self):
        self._closed = True


class _FakeUpstream:
    """按 Authorization Bearer key 本体 返回预设响应；记录所有调用。

    键 = 完整 key 字符串（如 "sk-key-A"），与 routes/media._upstream_request
    的 Authorization 头一致。
    """

    def __init__(self):
        self.responses = {}   # key_str -> list[_FakeResp]（顺序消费）
        self.calls = []        # (method, url, key_str, body)
        self._lock = threading.Lock()

    def set_responses(self, key_str: str, responses: list):
        self.responses[key_str] = list(responses)

    def _label(self, headers) -> str:
        auth = (headers or {}).get("Authorization", "")
        return auth.removeprefix("Bearer ").strip()

    def call(self, method: str, url: str, key_str: str,
             body=None, **kwargs) -> _FakeResp:
        with self._lock:
            self.calls.append((method, url, key_str, body))
            queue = self.responses.get(key_str) or []
            if queue:
                return queue.pop(0)
            # 无预设 → 返回 500
            return _FakeResp(500, {"error": "no preset response"})

    def reset(self):
        self.responses.clear()
        self.calls.clear()


@pytest.fixture()
def fake_upstream():
    return _FakeUpstream()


@pytest.fixture()
def media_app(fake_upstream, monkeypatch, tmp_path):
    """Flask app + monkeypatch 隔离：providers 配置 + 假 httpx + 独立调度器。"""
    cfg = {
        "port": 0,
        "api_keys": ["test-client-key-12345"],
        "providers": {
            "agnes": {
                "base_url": "https://api.agnes-ai.cn/v1",
                "api_keys": [
                    {"key": "sk-key-A", "label": "kA"},
                    {"key": "sk-key-B", "label": "kB"},
                ],
                "models": ["agnes-img", "agnes-video"],
            },
        },
    }
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    monkeypatch.setattr(_S, "CONFIG_FILE", cfg_file)
    monkeypatch.setattr(_PV, "CONFIG_FILE", cfg_file)

    # 重置 providers 缓存
    _PV._cache = None

    # 独立调度器（不依赖模块级单例，避免跨测试污染）
    fake_sched = KeyScheduler()

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def get(self, url, headers=None, timeout=None, **kw):
            key_str = fake_upstream._label(headers)
            return fake_upstream.call("GET", url, key_str, None)

        def post(self, url, json=None, headers=None, timeout=None, **kw):
            key_str = fake_upstream._label(headers)
            return fake_upstream.call("POST", url, key_str, json)

    monkeypatch.setattr(_PV, "get_client", lambda *a, **kw: _FakeClient())
    monkeypatch.setattr(MEDIA, "scheduler", fake_sched)

    app = Flask(__name__)
    app.register_blueprint(MEDIA.bp)
    app.config["TESTING"] = True
    return app.test_client(), fake_sched, cfg_file


def _auth_header() -> dict:
    return {"Authorization": "Bearer test-client-key-12345"}


# ─── 用例 1：无 api_key → 401 ─────────────────────────────────────

def test_no_api_key_returns_401(media_app, fake_upstream):
    client, _, _ = media_app
    # 不带 Authorization 头
    r = client.post("/v1/images/generations",
                    json={"model": "agnes-img", "prompt": "test"},
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 401, f"期望 401，实际 {r.status_code}: {r.get_data(as_text=True)[:200]}"
    body = r.get_json()
    assert body["error"]["code"] == "invalid_api_key"


def test_wrong_api_key_returns_401(media_app, fake_upstream):
    client, _, _ = media_app
    r = client.post("/v1/images/generations",
                    json={"model": "agnes-img", "prompt": "test"},
                    headers={"Authorization": "Bearer wrong-key",
                             "Content-Type": "application/json"})
    assert r.status_code == 401


# ─── 用例 2：正常转发，body 原样、响应原样回传 ─────────────────────

def test_normal_forward_images_200(media_app, fake_upstream):
    client, sched, _ = media_app
    sched.reset()

    upstream_body = {
        "model": "agnes-img",
        "prompt": "一只猫",
        "size": "1024x1024",
        "n": 1,
        "extra_body": {"foo": "bar"},
    }
    fake_upstream.set_responses("sk-key-A", [
        _FakeResp(201, {"created": True, "data": [{"url": "http://cos/cat.png"}]})
    ])

    r = client.post("/v1/images/generations",
                    json=upstream_body,
                    headers=_auth_header())
    assert r.status_code == 201, f"期望 201，实际 {r.status_code}: {r.get_data(as_text=True)[:300]}"
    resp_body = r.get_json()
    assert resp_body["created"] is True
    assert resp_body["data"][0]["url"] == "http://cos/cat.png"

    # 上游收到的 body 应原样（extra_body 保留）
    calls = [c for c in fake_upstream.calls if c[0] == "POST"]
    assert len(calls) == 1
    method, url, key_str, body = calls[0]
    assert key_str == "sk-key-A"
    assert body == upstream_body, f"上游收到的 body 被改写：{body}"
    assert url == "https://api.agnes-ai.cn/v1/images/generations"


def test_normal_forward_videos_400_passthrough(media_app, fake_upstream):
    client, sched, _ = media_app
    sched.reset()

    fake_upstream.set_responses("sk-key-A", [
        _FakeResp(400, {"error": "invalid seconds parameter"})
    ])

    r = client.post("/v1/videos",
                    json={"model": "agnes-video", "prompt": "test", "seconds": "4"},
                    headers=_auth_header())
    assert r.status_code == 400, f"期望 400 透传，实际 {r.status_code}: {r.get_data(as_text=True)[:300]}"
    assert r.get_json()["error"] == "invalid seconds parameter"


# ─── 用例 3：上游 429 → 换下一把 key 重试 ─────────────────────────

def test_upstream_429_switches_key(media_app, fake_upstream):
    client, sched, _ = media_app
    sched.reset()

    fake_upstream.set_responses("sk-key-A", [
        _FakeResp(429, {"error": "rate limited"})
    ])
    fake_upstream.set_responses("sk-key-B", [
        _FakeResp(200, {"created": True})
    ])

    r = client.post("/v1/images/generations",
                    json={"model": "agnes-img", "prompt": "test"},
                    headers=_auth_header())
    assert r.status_code == 200, f"期望 200（第二把 key），实际 {r.status_code}: {r.get_data(as_text=True)[:300]}"

    # 两次调用：kA 429，kB 200
    calls = [c for c in fake_upstream.calls if c[0] == "POST"]
    assert len(calls) == 2
    assert calls[0][2] == "sk-key-A"
    assert calls[1][2] == "sk-key-B"


# ─── 用例 4：所有 key 429 → 本端 429 + key_rate_limited + Retry-After ──

def test_all_keys_429_returns_429_rate_limited(media_app, fake_upstream):
    client, sched, _ = media_app
    sched.reset()

    fake_upstream.set_responses("sk-key-A", [
        _FakeResp(429, {"error": "rate limited"})
    ])
    fake_upstream.set_responses("sk-key-B", [
        _FakeResp(429, {"error": "rate limited"})
    ])

    r = client.post("/v1/images/generations",
                    json={"model": "agnes-img", "prompt": "test"},
                    headers=_auth_header())
    assert r.status_code == 429, f"期望 429，实际 {r.status_code}: {r.get_data(as_text=True)[:300]}"
    body = r.get_json()
    assert body["error"]["code"] == "key_rate_limited"
    assert body["error"]["message"] == "all keys rate-limited"
    assert r.headers.get("Retry-After") == "5"


# ─── 用例 5：未知模型 → 404（model_not_found 风格）──────────────

def test_unknown_model_returns_404(media_app, fake_upstream):
    client, sched, _ = media_app
    sched.reset()

    r = client.post("/v1/images/generations",
                    json={"model": "unknown-model", "prompt": "test"},
                    headers=_auth_header())
    assert r.status_code == 404, f"期望 404，实际 {r.status_code}: {r.get_data(as_text=True)[:300]}"
    body = r.get_json()
    assert body["error"]["code"] == "model_not_found"


# ─── 用例 6：/agnesapi query 透传 ─────────────────────────────────

def test_agnesapi_query_passthrough(media_app, fake_upstream):
    client, sched, _ = media_app
    sched.reset()

    fake_upstream.set_responses("sk-key-A", [
        _FakeResp(200, {"video_id": "vid-123", "status": "completed",
                        "url": "http://cos/video.mp4"})
    ])

    r = client.get("/agnesapi?video_id=vid-123&model_name=agnes-video",
                   headers=_auth_header())
    assert r.status_code == 200, f"期望 200，实际 {r.status_code}: {r.get_data(as_text=True)[:300]}"
    body = r.get_json()
    assert body["video_id"] == "vid-123"

    # 验证上游 URL 带了 query 参数
    calls = [c for c in fake_upstream.calls if c[0] == "GET"]
    assert len(calls) == 1
    method, url, key_str, _ = calls[0]
    assert "video_id=vid-123" in url, f"URL 缺少 video_id: {url}"
    assert "model_name=agnes-video" in url, f"URL 缺少 model_name: {url}"
    assert key_str == "sk-key-A"
