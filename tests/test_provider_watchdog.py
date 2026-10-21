# -*- coding: utf-8 -*-
"""渠道级自愈看守（scripts/provider_watchdog.py）单元测试。

覆盖口径（全程零网络：http_post_json 由 monkeypatch 替换）：
1. 渠道已启用 / 无 enabled 成员 → 零网络请求、退出码 0；
2. 渠道停用 + 首把 key 探测 2xx → 调一次启用接口，URL/参数体/Cookie 正确；
3. 探测 429 额度文案 → 不调启用接口；
4. 多把 key（第 1 把耗尽、第 2 把恢复）→ 逐把探测并启用；
5. 全部 key 401 → 不启用；
6. --dry-run 探测成功也不启用；
7. 渠道不存在 / 缺 base_url / 无可用 key → 退出码 0 且不崩、不探测；
8. disabled 的 key 不参与探测；
9. 启用接口失败 / 探测抛异常 / 缺 admin_password → 不崩、退出码 0。
"""

import json
import pathlib
import sys

import pytest

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from scripts import provider_watchdog as pw  # noqa: E402

_ENABLE_PATH = "/api/providers/intern/enabled"


def _cfg(**prov):
    return {"admin_password": "secret", "providers": {"intern": prov}}


def _write(path, cfg):
    path.write_text(json.dumps(cfg), encoding="utf-8")


@pytest.fixture
def fake_config(tmp_path):
    return tmp_path / "config.json"


def _run(monkeypatch, fake_config, cfg, calls, probe_seq=None, *,
         enable_status=200, dry_run=False):
    """装配一次可控运行；calls 收集 http_post_json 的 (url, payload, headers)。

    probe_seq 按序分配给探测请求（用尽后复用最后一个结果）。
    """
    _write(fake_config, cfg)
    seq = list(probe_seq or [(429, '{"error":{"message":"insufficient balance"}}')])
    state = {"i": 0}

    def fake_post(url, payload, headers=None, timeout=None, insecure=False):
        calls.append((url, payload, dict(headers or {})))
        if url.endswith(_ENABLE_PATH):
            return enable_status, '{"provider": "intern", "enabled": true}'
        idx = min(state["i"], len(seq) - 1)
        state["i"] += 1
        return seq[idx]

    monkeypatch.setattr(pw, "http_post_json", fake_post)
    argv = ["--config", str(fake_config), "--provider", "intern",
            "--probe-model", "intern-s1", "--admin-url", "https://relay.example"]
    if dry_run:
        argv.append("--dry-run")
    return pw.main(argv)


def _enable_calls(calls):
    return [c for c in calls if c[0].endswith(_ENABLE_PATH)]


# ── 1. 渠道已启用 → 零网络 ────────────────────────────────────────────
def test_enabled_channel_makes_no_request(monkeypatch, fake_config):
    calls = []
    rc = _run(monkeypatch, fake_config,
              _cfg(enabled=True, base_url="https://x/v1",
                   api_keys=[{"key": "k1", "label": "主key"}]),
              calls, [(200, "")])
    assert rc == 0
    assert calls == []


def test_missing_enabled_member_treated_as_enabled(monkeypatch, fake_config):
    calls = []
    rc = _run(monkeypatch, fake_config,
              _cfg(base_url="https://x/v1", api_keys=[{"key": "k1"}]),
              calls, [(200, "")])
    assert rc == 0
    assert calls == []


# ── 2. 首把 key 恢复 → 启用接口（路径/参数体/Cookie）─────────────────
def test_recovered_first_key_enables_channel(monkeypatch, fake_config):
    calls = []
    rc = _run(monkeypatch, fake_config,
              _cfg(enabled=False, base_url="https://intern.example/v1",
                   api_keys=[{"key": "k1", "label": "主key"},
                             {"key": "k2", "label": "key2"}]),
              calls, [(200, '{"choices":[]}')])
    assert rc == 0
    assert len(calls) == 2                      # 1 探测 + 1 启用
    probe_url, probe_payload, probe_hdrs = calls[0]
    assert probe_url == "https://intern.example/v1/chat/completions"
    assert probe_hdrs["Authorization"] == "Bearer k1"
    assert probe_payload["model"] == "intern-s1"
    assert probe_payload["max_tokens"] == 1
    enable_url, enable_payload, enable_hdrs = calls[1]
    assert enable_url == "https://relay.example" + _ENABLE_PATH
    assert enable_payload == {"enabled": True}
    assert enable_hdrs["Cookie"] == "admin_pwd=secret"


# ── 3. 仍耗尽 → 不启用 ───────────────────────────────────────────────
def test_still_exhausted_keeps_channel_disabled(monkeypatch, fake_config):
    calls = []
    rc = _run(monkeypatch, fake_config,
              _cfg(enabled=False, base_url="https://x/v1",
                   api_keys=[{"key": "k1", "label": "主key"}]),
              calls, [(429, '{"error":{"message":"insufficient balance"}}')])
    assert rc == 0
    assert len(calls) == 1
    assert _enable_calls(calls) == []


# ── 4. 多把 key 逐把探测：第 1 把耗尽、第 2 把恢复 → 启用 ─────────────
def test_second_key_recovery_enables_channel(monkeypatch, fake_config):
    calls = []
    rc = _run(monkeypatch, fake_config,
              _cfg(enabled=False, base_url="https://x/v1",
                   api_keys=[{"key": "k1", "label": "主key"},
                             {"key": "k2", "label": "key2"}]),
              calls, [(429, "quota exceeded"), (200, '{"choices":[]}')])
    assert rc == 0
    assert len(calls) == 3
    assert calls[0][2]["Authorization"] == "Bearer k1"
    assert calls[1][2]["Authorization"] == "Bearer k2"
    assert len(_enable_calls(calls)) == 1


def test_all_keys_exhausted_not_enabled(monkeypatch, fake_config):
    calls = []
    rc = _run(monkeypatch, fake_config,
              _cfg(enabled=False, base_url="https://x/v1",
                   api_keys=[{"key": "k1"}, {"key": "k2"}, {"key": "k3"}]),
              calls, [(429, "insufficient balance")])
    assert rc == 0
    assert len(calls) == 3
    assert _enable_calls(calls) == []


# ── 5. 全部 401 → 不启用 ─────────────────────────────────────────────
def test_all_keys_unauthorized_not_enabled(monkeypatch, fake_config):
    calls = []
    rc = _run(monkeypatch, fake_config,
              _cfg(enabled=False, base_url="https://x/v1",
                   api_keys=[{"key": "k1", "label": "主key"},
                             {"key": "k2", "label": "key2"}]),
              calls, [(401, '{"msg":"user token expired"}'),
                      (403, "forbidden")])
    assert rc == 0
    assert len(calls) == 2
    assert _enable_calls(calls) == []


# ── 6. dry-run 不启用 ────────────────────────────────────────────────
def test_dry_run_does_not_enable(monkeypatch, fake_config):
    calls = []
    rc = _run(monkeypatch, fake_config,
              _cfg(enabled=False, base_url="https://x/v1",
                   api_keys=[{"key": "k1", "label": "主key"}]),
              calls, [(200, "")], dry_run=True)
    assert rc == 0
    assert len(calls) == 1
    assert _enable_calls(calls) == []


# ── 7. 配置异常路径：退出码 0 且不探测 ────────────────────────────────
def test_unknown_provider_exits_zero(monkeypatch, fake_config):
    calls = []
    monkeypatch.setattr(pw, "http_post_json",
                        lambda *a, **k: pytest.fail("不应发起网络请求"))
    _write(fake_config, {"admin_password": "secret", "providers": {}})
    rc = pw.main(["--config", str(fake_config), "--provider", "intern",
                  "--probe-model", "intern-s1", "--admin-url", "https://relay.example"])
    assert rc == 0
    assert calls == []


def test_missing_base_url_exits_zero(monkeypatch, fake_config):
    calls = []
    monkeypatch.setattr(pw, "http_post_json",
                        lambda *a, **k: pytest.fail("不应发起网络请求"))
    _write(fake_config, _cfg(enabled=False, api_keys=[{"key": "k1"}]))
    rc = pw.main(["--config", str(fake_config), "--provider", "intern",
                  "--probe-model", "intern-s1", "--admin-url", "https://relay.example"])
    assert rc == 0
    assert calls == []


def test_no_usable_key_exits_zero(monkeypatch, fake_config):
    calls = []
    monkeypatch.setattr(pw, "http_post_json",
                        lambda *a, **k: pytest.fail("不应发起网络请求"))
    _write(fake_config, _cfg(enabled=False, base_url="https://x/v1", api_keys=[]))
    rc = pw.main(["--config", str(fake_config), "--provider", "intern",
                  "--probe-model", "intern-s1", "--admin-url", "https://relay.example"])
    assert rc == 0
    assert calls == []


def test_all_keys_disabled_exits_zero(monkeypatch, fake_config):
    calls = []
    monkeypatch.setattr(pw, "http_post_json",
                        lambda *a, **k: pytest.fail("不应发起网络请求"))
    _write(fake_config, _cfg(enabled=False, base_url="https://x/v1",
                             api_keys=[{"key": "k1", "label": "主key",
                                        "disabled": True}]))
    rc = pw.main(["--config", str(fake_config), "--provider", "intern",
                  "--probe-model", "intern-s1", "--admin-url", "https://relay.example"])
    assert rc == 0
    assert calls == []


def test_unreadable_config_exits_zero(monkeypatch, tmp_path):
    monkeypatch.setattr(pw, "http_post_json",
                        lambda *a, **k: pytest.fail("不应发起网络请求"))
    rc = pw.main(["--config", str(tmp_path / "nope.json"), "--provider", "intern",
                  "--probe-model", "intern-s1", "--admin-url", "https://relay.example"])
    assert rc == 0


# ── 8. disabled 的 key 不参与探测 ────────────────────────────────────
def test_disabled_key_is_skipped(monkeypatch, fake_config):
    calls = []
    rc = _run(monkeypatch, fake_config,
              _cfg(enabled=False, base_url="https://x/v1",
                   api_keys=[{"key": "dead", "label": "主key", "disabled": True},
                             {"key": "alive", "label": "key2"}]),
              calls, [(200, '{"choices":[]}')])
    assert rc == 0
    assert len(calls) == 2
    assert calls[0][2]["Authorization"] == "Bearer alive"   # dead 未被探测
    assert len(_enable_calls(calls)) == 1


# ── 9. 失败面：启用接口 500 / 探测抛异常 / 缺 admin_password ───────────
def test_enable_failure_does_not_crash(monkeypatch, fake_config):
    calls = []
    rc = _run(monkeypatch, fake_config,
              _cfg(enabled=False, base_url="https://x/v1",
                   api_keys=[{"key": "k1", "label": "主key"}]),
              calls, [(200, "")], enable_status=500)
    assert rc == 0
    assert len(_enable_calls(calls)) == 1


def test_probe_exception_keeps_disabled(monkeypatch, fake_config):
    calls = []

    def boom(url, payload, headers=None, timeout=None, insecure=False):
        calls.append(url)
        raise TimeoutError("probe timed out")

    _write(fake_config, _cfg(enabled=False, base_url="https://x/v1",
                             api_keys=[{"key": "k1", "label": "主key"}]))
    monkeypatch.setattr(pw, "http_post_json", boom)
    rc = pw.main(["--config", str(fake_config), "--provider", "intern",
                  "--probe-model", "intern-s1", "--admin-url", "https://relay.example"])
    assert rc == 0
    assert len(calls) == 1
    assert not any(u.endswith(_ENABLE_PATH) for u in calls)


def test_missing_admin_password_no_enable_call(monkeypatch, fake_config):
    calls = []

    def fake_post(url, payload, headers=None, timeout=None, insecure=False):
        calls.append(url)
        return 200, '{"choices":[]}'

    _write(fake_config, {"providers": {"intern": {
        "enabled": False, "base_url": "https://x/v1",
        "api_keys": [{"key": "k1", "label": "主key"}]}}})
    monkeypatch.setattr(pw, "http_post_json", fake_post)
    rc = pw.main(["--config", str(fake_config), "--provider", "intern",
                  "--probe-model", "intern-s1", "--admin-url", "https://relay.example"])
    assert rc == 0
    assert calls == ["https://x/v1/chat/completions"]     # 只探测，无启用调用


# ── 10. 纯函数：active_keys / _channel_disabled ──────────────────────
def test_active_keys_filters_disabled():
    prov = {"api_keys": [{"key": "a", "label": "主key", "disabled": True},
                         {"key": "b", "label": "key2"},
                         "c"]}
    assert [e["key"] for e in pw.active_keys(prov)] == ["b", "c"]


def test_channel_disabled_semantics():
    assert pw._channel_disabled({"enabled": False}) is True
    assert pw._channel_disabled({"enabled": True}) is False
    assert pw._channel_disabled({}) is False             # 缺省 = 启用


def test_enable_provider_quotes_provider_name(monkeypatch, fake_config):
    seen = {}

    def fake_post(url, payload, headers=None, timeout=None, insecure=False):
        seen["url"] = url
        seen["payload"] = payload
        seen["headers"] = dict(headers or {})
        return 200, "{}"

    monkeypatch.setattr(pw, "http_post_json", fake_post)
    pw.enable_provider("https://relay.example/", "pwd", "a b")
    assert seen["url"] == "https://relay.example/api/providers/a%20b/enabled"
    assert seen["payload"] == {"enabled": True}
    assert seen["headers"]["Cookie"] == "admin_pwd=pwd"
