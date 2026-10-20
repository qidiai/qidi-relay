# -*- coding: utf-8 -*-
"""静态 key 自愈看守（scripts/key_watchdog.py）单元测试。

覆盖口径：
1. key_entries：裸字符串 / dict / 单 api_key 与数组混用 / 重复去重；
2. disabled_entries：只挑 disabled=True 的条目；
3. classify：200 恢复、429 与额度文案 → exhausted、401/403 → unauthorized、
   5xx/网络层 → unknown（宁可不启用，不可误启用）；
4. main 行为：无停用 key → 零探测；探测恢复 → 调 toggle(disabled=false)；
   探测仍耗尽 → 不调 toggle；--dry-run 只探测不启用；缺 admin_password
   /渠道不存在 → 不崩、退出码 0（cron 友好）。

全程零网络：http_post_json 由 monkeypatch 替换。
"""

import pathlib
import sys

import pytest

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from scripts import key_watchdog as kw  # noqa: E402


def _cfg(**prov):
    return {"admin_password": "secret", "providers": {"modelscope": prov}}


# ── 1. key 归一化 ──────────────────────────────────────────────────────
def test_key_entries_single_api_key():
    entries = kw.key_entries({"api_key": "ms-a"})
    assert entries == [{"key": "ms-a", "label": "主key", "disabled": False}]


def test_key_entries_array_dict_and_str():
    entries = kw.key_entries({"api_keys": [
        {"key": "ms-a", "label": "主key", "disabled": True},
        "ms-b",
        {"key": "ms-c", "disabled": False},
    ]})
    assert [e["key"] for e in entries] == ["ms-a", "ms-b", "ms-c"]
    assert [e["label"] for e in entries] == ["主key", "key2", "key3"]
    assert [e["disabled"] for e in entries] == [True, False, False]


def test_key_entries_dedup_and_blank():
    entries = kw.key_entries({"api_keys": [{"key": "ms-a"}, {"key": "ms-a"}, {"key": "  "}]})
    assert [e["key"] for e in entries] == ["ms-a"]


def test_disabled_entries_filters():
    prov = {"api_keys": [{"key": "a", "label": "主key", "disabled": True},
                         {"key": "b", "label": "modelscope·2"}]}
    assert [e["label"] for e in kw.disabled_entries(prov)] == ["主key"]


# ── 2. 结果分类 ────────────────────────────────────────────────────────
@pytest.mark.parametrize("status,body,expected", [
    (200, '{"choices":[]}', "recovered"),
    (429, '{"error":{"message":"insufficient balance"}}', "exhausted"),
    (429, "额度耗尽", "exhausted"),
    (400, "quota exceeded", "exhausted"),
    (401, '{"msgCode":"A0211","msg":"user token expired"}', "unauthorized"),
    (403, "forbidden", "unauthorized"),
    (500, "internal", "unknown"),
    (0, "URLError: timed out", "unknown"),
])
def test_classify(status, body, expected):
    assert kw.classify(status, body) == expected


# ── 3. main 行为（零网络） ─────────────────────────────────────────────
@pytest.fixture
def fake_config(tmp_path):
    p = tmp_path / "config.json"
    return p


def _write(path, cfg):
    import json
    path.write_text(json.dumps(cfg), encoding="utf-8")


def _run(monkeypatch, fake_config, cfg, calls, *, probe_result=(429, "insufficient balance"),
         toggle_status=200, dry_run=False):
    """装配一个可控的 main 运行；calls 收集 http_post_json 的 URL。"""
    _write(fake_config, cfg)

    def fake_post(url, payload, headers=None, timeout=kw.TIMEOUT, insecure=False):
        calls.append((url, payload, dict(headers or {})))
        if url.endswith("/api/provider/key/toggle"):
            return toggle_status, '{"ok":true}'
        return probe_result

    monkeypatch.setattr(kw, "http_post_json", fake_post)
    argv = ["--config", str(fake_config), "--provider", "modelscope",
            "--probe-model", "Qwen/Qwen3.8-27B",
            "--admin-url", "https://relay.example"]
    if dry_run:
        argv.append("--dry-run")
    return kw.main(argv)


def test_main_no_disabled_key_makes_no_probe(monkeypatch, fake_config):
    calls = []
    rc = _run(monkeypatch, fake_config, _cfg(api_keys=[{"key": "a", "label": "k1"}]),
              calls, probe_result=(200, ""))
    assert rc == 0
    assert calls == []          # 正常态零探测成本


def test_main_reenables_recovered_key(monkeypatch, fake_config):
    calls = []
    rc = _run(monkeypatch, fake_config,
              _cfg(base_url="https://api-inference.modelscope.cn/v1",
                   api_keys=[{"key": "a", "label": "主key", "disabled": True}]),
              calls, probe_result=(200, '{"choices":[]}'))
    assert rc == 0
    assert len(calls) == 2
    probe_url, probe_payload, probe_hdrs = calls[0]
    assert probe_url.endswith("/chat/completions")
    assert probe_hdrs["Authorization"] == "Bearer a"
    assert probe_payload["model"] == "Qwen/Qwen3.8-27B"
    toggle_url, toggle_payload, toggle_hdrs = calls[1]
    assert toggle_url.endswith("/api/provider/key/toggle")
    assert toggle_payload == {"provider": "modelscope", "label": "主key", "disabled": False}
    assert toggle_hdrs["Cookie"] == "admin_pwd=secret"


def test_main_keeps_disabled_when_still_exhausted(monkeypatch, fake_config):
    calls = []
    _run(monkeypatch, fake_config,
         _cfg(base_url="https://x/v1",
              api_keys=[{"key": "a", "label": "主key", "disabled": True}]),
         calls, probe_result=(429, '{"error":{"message":"insufficient balance"}}'))
    assert len(calls) == 1      # 只探测，不启用


def test_main_dry_run_does_not_enable(monkeypatch, fake_config):
    calls = []
    _run(monkeypatch, fake_config,
         _cfg(base_url="https://x/v1",
              api_keys=[{"key": "a", "label": "主key", "disabled": True}]),
         calls, probe_result=(200, ""), dry_run=True)
    assert len(calls) == 1
    assert not any(u.endswith("/api/provider/key/toggle") for u, _, _ in calls)


def test_main_toggle_failure_does_not_crash(monkeypatch, fake_config):
    calls = []
    rc = _run(monkeypatch, fake_config,
              _cfg(base_url="https://x/v1",
                   api_keys=[{"key": "a", "label": "主key", "disabled": True}]),
              calls, probe_result=(200, ""), toggle_status=500)
    assert rc == 0
    assert len(calls) == 2


def test_main_missing_admin_password_no_write(monkeypatch, fake_config):
    calls = []
    cfg = {"providers": {"modelscope": {"base_url": "https://x/v1",
                                        "api_keys": [{"key": "a", "disabled": True}]}}}
    rc = _run(monkeypatch, fake_config, cfg, calls, probe_result=(200, ""))
    assert rc == 0
    assert calls == []


def test_main_unknown_provider_no_crash(monkeypatch, fake_config):
    calls = []
    _write(fake_config, {"admin_password": "s", "providers": {}})
    monkeypatch.setattr(kw, "http_post_json",
                        lambda *a, **k: pytest.fail("不应发起网络请求"))
    rc = kw.main(["--config", str(fake_config), "--provider", "modelscope",
                  "--probe-model", "m", "--admin-url", "https://relay.example"])
    assert rc == 0
    assert calls == []
