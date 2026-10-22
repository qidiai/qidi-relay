# -*- coding: utf-8 -*-
"""错误码归一化单元测试（纯函数，零网络）。"""
import json
import pathlib
import sys

import pytest

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

# 与其它 relay 测试一致：先把 settings 指到临时目录，避免读生产 config
import tempfile  # noqa: E402
_TMP = pathlib.Path(tempfile.mkdtemp(prefix="qidi-errmap-test-"))
(_TMP / "config.json").write_text(json.dumps({"providers": {}}), encoding="utf-8")
(_TMP / "accounts.json").write_text(json.dumps({"accounts": []}), encoding="utf-8")

import proxy_core.settings as _S  # noqa: E402

_S.BASE_DIR = _TMP
_S.CONFIG_FILE = _TMP / "config.json"
_S.ACCOUNTS_FILE = _TMP / "accounts.json"
_S.STATS_FILE = _TMP / "token_stats.json"
_S.WATER_FILE = _TMP / "water_check.json"
_S.LOG_FILE = _TMP / "proxy.log"

from routes.media import normalize_upstream_error  # noqa: E402


def _body(status, code, msg):
    return json.dumps({"error": {"code": code, "message": msg}}).encode()


def test_success_passthrough():
    raw = b'{"data":[{"url":"https://x/y.png"}]}'
    st, body, ct = normalize_upstream_error(200, raw)
    assert st == 200 and body == raw


def test_non_json_passthrough():
    raw = b"<html>502 Bad Gateway</html>"
    st, body, _ = normalize_upstream_error(500, raw)
    assert st == 500 and body == raw


def test_401_becomes_provider_error():
    st, body, _ = normalize_upstream_error(401, _body(401, "invalid_api_key", "无效的令牌"))
    d = json.loads(body)
    assert st == 502
    assert d["error"]["code"] == "provider_auth_failed"
    assert d["error"]["type"] == "provider_error"
    assert d["error"]["upstream"] == {"provider": "agnes", "status": 401,
                                      "code": "invalid_api_key", "message": "无效的令牌"}


@pytest.mark.parametrize("status,code", [(402, "provider_quota_exceeded"),
                                         (403, "provider_access_denied")])
def test_payment_and_forbidden_map_to_502(status, code):
    st, body, _ = normalize_upstream_error(status, _body(status, "x", "y"))
    d = json.loads(body)
    assert st == 502 and d["error"]["code"] == code


def test_404_task_not_found():
    st, body, _ = normalize_upstream_error(404, _body(404, 404, "任务不存在 (request id: 123)"))
    d = json.loads(body)
    assert st == 404 and d["error"]["code"] == "task_not_found"


def test_404_model_not_found():
    st, body, _ = normalize_upstream_error(404, _body(404, "model_not_found", "当前分组不支持模型 x"))
    d = json.loads(body)
    assert st == 404 and d["error"]["code"] == "model_or_task_not_found"


def test_429_keeps_status():
    st, body, _ = normalize_upstream_error(429, _body(429, "rate_limit", "RPM exceeded"))
    d = json.loads(body)
    assert st == 429 and d["error"]["code"] == "key_rate_limited"


def test_5xx_maps_to_502():
    st, body, _ = normalize_upstream_error(503, _body(503, "unavailable", "模型不可用"))
    d = json.loads(body)
    assert st == 502 and d["error"]["code"] == "provider_unavailable"
