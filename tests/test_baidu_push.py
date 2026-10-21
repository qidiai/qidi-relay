# -*- coding: utf-8 -*-
"""baidu_push.py 单元测试（零网络）。"""

import json
import pathlib
import sys

import pytest

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from scripts import baidu_push as bp  # noqa: E402


SITEMAP = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://www.qidiai.ltd/</loc></url>
  <url><loc>https://www.qidiai.ltd/tech/</loc><lastmod>2026-10-21</lastmod></url>
  <url>
    <loc>https://www.qidiai.ltd/tech/free-token-resources/</loc>
  </url>
</urlset>
"""


def test_sitemap_urls_parses_and_trims():
    urls = bp.sitemap_urls(SITEMAP)
    assert urls == [
        "https://www.qidiai.ltd/",
        "https://www.qidiai.ltd/tech/",
        "https://www.qidiai.ltd/tech/free-token-resources/",
    ]


def test_pick_new_skips_pushed_and_caps():
    urls = ["a", "b", "c", "d"]
    assert bp.pick_new(urls, {"a"}, 2) == ["b", "c"]
    assert bp.pick_new(urls, set(), 10) == urls
    assert bp.pick_new(urls, set(urls), 10) == []


def test_pick_new_dedups_input():
    assert bp.pick_new(["a", "a", "b"], set(), 10) == ["a", "b"]


def test_state_roundtrip(tmp_path):
    p = tmp_path / "sub" / "pushed.json"
    bp.save_state(str(p), {"https://x/1", "https://x/2"})
    assert bp.load_state(str(p)) == {"https://x/1", "https://x/2"}
    # 缺失文件 → 空集，不抛
    assert bp.load_state(str(tmp_path / "nope.json")) == set()


def test_state_corrupt_returns_empty(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json", encoding="utf-8")
    assert bp.load_state(str(p)) == set()


@pytest.fixture
def env(tmp_path, monkeypatch):
    sm = tmp_path / "sitemap.xml"
    sm.write_text(SITEMAP, encoding="utf-8")
    monkeypatch.setenv("BAIDU_PUSH_TOKEN", "tok")
    monkeypatch.setenv("BAIDU_SITE", "https://www.qidiai.ltd")
    monkeypatch.setenv("BAIDU_SITEMAP", str(sm))
    monkeypatch.setenv("BAIDU_PUSH_STATE", str(tmp_path / "state.json"))
    monkeypatch.setenv("BAIDU_PUSH_MAX", "10")
    return tmp_path


def test_main_dry_run_sends_nothing(env, monkeypatch, capsys):
    monkeypatch.setattr(bp, "push", lambda *a, **k: pytest.fail("不应发请求"))
    assert bp.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "本轮待推=3" in out and "未发送" in out


def test_main_pushes_and_records_success(env, monkeypatch, capsys):
    calls = []

    def fake_push(urls, token, site, timeout=25):
        calls.append((list(urls), token, site))
        return 200, json.dumps({"remain": 7, "success": len(urls)})

    monkeypatch.setattr(bp, "push", fake_push)
    assert bp.main([]) == 0
    assert calls and calls[0][1] == "tok"
    state = bp.load_state(str(env / "state.json"))
    assert len(state) == 3
    # 第二次运行：无新增
    assert bp.main([]) == 0
    assert "无新增 URL" in capsys.readouterr().out


def test_main_partial_success_records_only_pushed(env, monkeypatch):
    monkeypatch.setattr(bp, "push", lambda urls, token, site, timeout=25:
                        (200, json.dumps({"remain": 9, "success": 1})))
    assert bp.main([]) == 0
    state = bp.load_state(str(env / "state.json"))
    assert len(state) == 1          # 只记成功条数，其余下轮重试


def test_main_error_keeps_state(env, monkeypatch):
    monkeypatch.setattr(bp, "push", lambda urls, token, site, timeout=25:
                        (401, json.dumps({"error": 401, "message": "token is not valid"})))
    assert bp.main([]) == 0
    assert bp.load_state(str(env / "state.json")) == set()


def test_main_missing_token_no_request(env, monkeypatch):
    monkeypatch.delenv("BAIDU_PUSH_TOKEN")
    monkeypatch.setattr(bp, "push", lambda *a, **k: pytest.fail("不应发请求"))
    assert bp.main([]) == 0


def test_main_sitemap_unreachable(env, monkeypatch, capsys):
    monkeypatch.setenv("BAIDU_SITEMAP", str(env / "missing.xml"))
    monkeypatch.setattr(bp, "push", lambda *a, **k: pytest.fail("不应发请求"))
    assert bp.main([]) == 0
    assert "拉 sitemap 失败" in capsys.readouterr().err
