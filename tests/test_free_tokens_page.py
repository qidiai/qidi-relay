# -*- coding: utf-8 -*-
"""公开栏目「免费 Token 资源分享」(/free-tokens) 回归。

断言四件事：
  1. 公开可访问：无 cookie / 无 Authorization 也 200（不被鉴权重定向），
     且路由挂在 app 上（路径 /free-tokens，不在 /api 前缀下）、无
     @check_admin_auth 装饰；
  2. 首篇内容 APIGOTO 关键事实齐备：推广链接、1 亿 / 4 亿额度、实测表
     关键行（DeepSeek / Opus / Haiku / Grok），价格与避雷提示在页；
  3. 后续资源预留骨架在页（3 张 "即将上新"：ModelScope / OpenRouter / Qoder）；
  4. 入口可达：index.html 顶栏与登录闸门下都有 /free-tokens 链接。

起真实 Flask app（main.py 的 app），但 config/accounts/日志路径全部
重定向到 tmp —— 不碰仓库真实数据文件，也不在仓库根写 proxy.log。
main 在 fixture 内延迟 import：模块级 import 会打开日志文件并装配蓝图。
"""

import pathlib
import re
import sys

import pytest
from pytest import MonkeyPatch

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

_PROMO_SLUG = "wechat_oX2xV2dt6vu8jpIdGvpvPHzk_qY8"
_PROMO_URL = "https://www.apigoto.com/" + _PROMO_SLUG


@pytest.fixture(scope="module")
def app_client(tmp_path_factory):
    """真实 app + 隔离数据目录；客户端默认无任何 cookie。"""
    tmp = tmp_path_factory.mktemp("free-tokens-page")
    (tmp / "config.json").write_text('{"providers": {}}', encoding="utf-8")
    (tmp / "accounts.json").write_text('{"accounts": []}', encoding="utf-8")

    import proxy_core.settings as S

    mp = MonkeyPatch()
    mp.setattr(S, "BASE_DIR", tmp)
    mp.setattr(S, "CONFIG_FILE", tmp / "config.json")
    mp.setattr(S, "ACCOUNTS_FILE", tmp / "accounts.json")
    mp.setattr(S, "STATS_FILE", tmp / "token_stats.json")
    mp.setattr(S, "WATER_FILE", tmp / "water_check.json")
    mp.setattr(S, "LOG_FILE", tmp / "proxy.log")
    mp.setattr(S, "CONVERSATIONS_FILE", tmp / "conversations.json")
    mp.setattr(S, "COOLDOWNS_FILE", tmp / "cooldowns.json")
    mp.setattr(S, "CORPUS_DIR", tmp / "corpus_local")

    import main  # noqa: WPS433 —— 路径重定向之后再装配真实 app

    app = main.app
    app.config.update(TESTING=True)
    try:
        with app.test_client() as client:
            yield client, app
    finally:
        mp.undo()


def _body(client) -> str:
    r = client.get("/free-tokens")
    assert r.status_code == 200, f"/free-tokens 应公开返回 200，实得 {r.status_code}"
    return r.get_data(as_text=True)


# ─── 1. 公开访问 ──────────────────────────────────────────────


def test_free_tokens_page_is_public(app_client):
    """无 cookie / 无 Authorization 直接 200，且不被重定向到登录。"""
    client, app = app_client

    rules = {str(r.rule): r.endpoint for r in app.url_map.iter_rules()}
    assert "/free-tokens" in rules, "应注册 /free-tokens 路由"
    assert rules["/free-tokens"] == "free_tokens"
    assert "/api/free-tokens" not in rules, "公开页不应落在 /api 前缀下"

    r = client.get("/free-tokens")  # 无 cookie、无鉴权头
    assert r.status_code == 200
    assert r.headers.get("Location") is None, "公开页不应 302 到登录"
    assert "text/html" in r.headers["Content-Type"]

    body = r.get_data(as_text=True)
    assert "QiDi Relay" in body
    assert "免费 Token 资源分享" in body


def test_free_tokens_route_has_no_admin_auth_decorator():
    """源码级护栏：@app.route("/free-tokens") 与 def 之间不得有 check_admin_auth。"""
    src = (BASE / "main.py").read_text(encoding="utf-8")
    m = re.search(r'@app\.route\("/free-tokens"\)(.*?)def free_tokens', src, re.S)
    assert m, "main.py 应存在 /free-tokens 路由"
    assert "check_admin_auth" not in m.group(1), "公开页不得挂 @check_admin_auth"


# ─── 2. 首篇内容：APIGOTO ────────────────────────────────────


def test_apigoto_card_carries_promo_and_quota_facts(app_client):
    """推广链接、额度、价格与避雷提示齐备。"""
    body = _body(app_client[0])

    assert _PROMO_SLUG in body, "页面必须含推广 slug"
    assert _PROMO_URL in body, "页面必须含完整注册链接"
    assert 'href="' + _PROMO_URL + '"' in body, "注册链接应可点击"

    assert "1 亿 token" in body, "注册送 1 亿 token"
    assert "4 亿 token" in body, "实名再送 4 亿 token"
    assert "网易易盾" in body
    assert "0.8 折起" in body
    assert "注水避雷" in body


def test_apigoto_card_carries_model_test_table(app_client):
    """实测表关键行（DeepSeek / Opus / Haiku / Grok）与判定不在页面丢失。"""
    body = _body(app_client[0])

    for model in (
        "DeepSeek V4 Pro",
        "Claude Opus 5.5",
        "Claude Haiku 5.5",
        "Grok 4.7",
        "GLM-5.3",
        "Doubao-Seed",
    ):
        assert model in body, f"实测表缺行：{model}"

    # 证据列的可核查细节（防"只剩模型名"的降级）
    assert "prompt_tokens 恒差 53" in body
    assert "难题组 6/6" in body
    assert body.count("✅ 真身") >= 5
    assert "❌ 注水避雷" in body
    assert "⚠️ 存疑" in body


# ─── 3. 预留资源位 ───────────────────────────────────────────


def test_upcoming_resource_placeholders(app_client):
    """"即将上新"骨架 3 张：ModelScope / OpenRouter / Qoder。"""
    body = _body(app_client[0])

    assert body.count('class="panel soon"') == 3, "应恰好 3 张预留骨架卡"
    assert body.count('<span class="tag dim">即将上新</span>') == 3, "每张骨架卡各带一个「即将上新」标"
    for kw in ("ModelScope", "OpenRouter", "Qoder"):
        assert kw in body, f"预留卡缺关键词：{kw}"


# ─── 4. 控制台入口 ───────────────────────────────────────────


def test_index_links_to_free_tokens():
    """index.html 顶栏 + 登录闸门都有公开栏目入口。"""
    html = (BASE / "web" / "index.html").read_text(encoding="utf-8")
    assert html.count('href="/free-tokens"') == 2, "顶栏与登录闸门各一处入口"
    assert "免费 Token 资源分享" in html
