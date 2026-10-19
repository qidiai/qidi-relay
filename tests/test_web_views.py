"""前端四视图重组（v1）静态资产断言。

纯读文件校验，不起 Flask、不执行 JS：
  1. index.html 存在四个视图容器 id；
  2. 12 个静态 panel 各恰好归属一个视图容器，且 panel-providers 已静态化进渠道视图；
  3. 缓存戳 app.css?v=56 / app.js?v=58，且「清空统计」.danger-zone 已从用量分布迁到安全设置。

方案：docs/frontend-regroup-plan.md；
任务书：一页 14 panel 拆「总览 / 渠道 / 运维 / 设置」四视图，单 HTML display 切换。
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HTML = ROOT / "web" / "index.html"
CSS = ROOT / "web" / "static" / "app.css"
JS = ROOT / "web" / "static" / "app.js"

# 现有 12 个静态 panel（providers 原先由 JS 注入，本次静态化，单列断言）
STATIC_PANELS = [
    "panel-connect",
    "panel-trend",
    "panel-accounts",
    "panel-dist",
    "panel-quality",
    "panel-corpus",
    "panel-affinity",
    "panel-gate",
    "panel-activity",
    "panel-playground",
    "panel-logs",
    "panel-settings",
]

VIEW_IDS = ["view-overview", "view-channels", "view-ops", "view-settings"]


def _html() -> str:
    return HTML.read_text(encoding="utf-8")


def _view_segments():
    """把 <main> 内四个视图容器切成 {view_id: 段落文本}。

    不依赖任何注释：以每个视图容器开标签为分段点，取到下一个视图开标签
    （或 </main>）为止。
    """
    html = _html()
    main = html[html.index('<main class="grid">'): html.index("</main>")]
    marks = list(re.finditer(r'<div class="view[^"]*" id="(view-\w+)">', main))
    assert len(marks) == 4, f"应恰好有 4 个视图容器，实得 {len(marks)}"
    segs = {}
    for i, m in enumerate(marks):
        start = m.end()
        end = marks[i + 1].start() if i + 1 < len(marks) else len(main)
        segs[m.group(1)] = main[start:end]
    return segs


def _panel_segment(html: str, panel_id: str) -> str:
    """截取单个 <section ... id="panel-x"> ... </section> 的正文（panel 无嵌套 section）。"""
    m = re.search(
        r'<section[^>]*id="' + re.escape(panel_id) + r'"[^>]*>(.*?)</section>',
        html,
        re.S,
    )
    assert m, f"未找到 panel 节点 {panel_id}"
    return m.group(1)


def test_four_view_containers_exist():
    """用例 1：index.html 存在四个视图容器 id。"""
    html = _html()
    for vid in VIEW_IDS:
        assert f'id="{vid}"' in html, f"缺少视图容器 {vid}"
    # 默认激活视图 = 总览
    assert 'class="view active" id="view-overview"' in html


def test_each_panel_belongs_to_exactly_one_view():
    """用例 2：12 个静态 panel 各恰好归属一个视图容器 + panel-providers 静态在渠道视图。"""
    segs = _view_segments()

    # 每个视图段落内的 panel id 集合（去重）
    per_view = {v: set(re.findall(r'id="(panel-[\w-]+)"', seg)) for v, seg in segs.items()}

    for pid in STATIC_PANELS:
        owners = [v for v, ids in per_view.items() if pid in ids]
        assert len(owners) == 1, f"{pid} 应恰好属于一个视图，实得 {owners}"

    # 静态化后的大盘：13 个静态 panel 节点全部在 HTML 里
    assert "panel-providers" in per_view["view-channels"], "panel-providers 应静态存在于渠道视图"
    assert re.search(r'<section[^>]*id="panel-providers"', _html()), "panel-providers 应为静态节点"

    # 视图归属收口（与 VIEW_PANELS 约定一致）
    assert per_view["view-overview"] == {
        "panel-connect", "panel-playground", "panel-trend", "panel-dist",
    }
    assert per_view["view-channels"] == {
        "panel-accounts", "panel-providers", "panel-quality",
    }
    assert per_view["view-ops"] == {
        "panel-activity", "panel-logs", "panel-gate", "panel-affinity", "panel-corpus",
    }
    assert per_view["view-settings"] == {"panel-settings"}

    # HTML 内静态 panel 节点总数 = 12 + providers = 13
    assert len(re.findall(r'<section[^>]*class="panel[^"]*"[^>]*id="panel-[\w-]+"', _html())) == 13


def test_asset_version_and_danger_zone_migration():
    """用例 3：静态资产缓存戳 + 清空统计按钮已从用量分布迁到安全设置。"""
    html = _html()
    assert "app.css?v=56" in html, "app.css 缓存戳应为 v=56"
    assert "app.js?v=58" in html, "app.js 缓存戳应为 v=58"

    dist = _panel_segment(html, "panel-dist")
    settings = _panel_segment(html, "panel-settings")
    assert "danger-zone" not in dist, "danger-zone 不应再出现在 panel-dist"
    assert "resetTokenStats()" not in dist, "清空统计按钮不应再挂在 panel-dist"
    assert "danger-zone" in settings, "danger-zone 应已迁入 panel-settings"
    assert "resetTokenStats()" in settings, "清空统计按钮应迁到 panel-settings"

    # 静态资产确实存在（防止路径漂移）
    assert CSS.is_file() and JS.is_file()
