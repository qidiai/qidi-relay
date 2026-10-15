# QiDi Relay 管理面板前端重组方案（v1，2026-10-15）

> 现状痛点（用户原话"整个界面全部在一个网页上面，太乱太杂"）：
> index.html 一页平铺 **14 个 panel**（主网格 13 + providers 动态注入 1）、app.js 单文件 1701 行 119 个函数/变量、
> 用量滚动页面极长；运营台与观测台与运维工具混在一层，无导航。

## 一、现状盘点

| 面板 | id | 内容性质 | 刷新要求 |
|---|---|---|---|
| KPI 指标带 | — | 全局概览 | 5s |
| 接入信息 | panel-connect | 新用户 onboarding | 静态 |
| 用量趋势 | panel-trend | 观测图表 | 30s |
| 账号矩阵 | panel-accounts | 运营管理 | 5s |
| 外部账号（JS 注入） | panel-providers | 运营管理 | 120s |
| 用量分布 | panel-dist | 观测图表 | 30s |
| 账号质量 | panel-quality | 观测 | 30s |
| 语料采集 | panel-corpus | 观测 | 30s |
| 会话粘性 | panel-affinity | 观测 | 30s |
| 排队车道 | panel-gate | 观测 | 5s |
| 实时动态 | panel-activity | 运维直播流 | 持续 |
| API 测试台 | panel-playground | 开发工具 | 按需 |
| 系统日志 | panel-logs | 运维排障 | 滚动 |
| 安全设置 | panel-settings | 低频配置 | 静态 |

刷新节奏分四档（5s 实时 / 30s 观测 / 120s 管理 / 静态）——这正是天然的分组线。

## 二、方案：标签页四视图 + 顶栏常驻带

### 结构
```
顶栏（不动：brand/时钟/状态灯/退出）
KPI 带（不动：全局概览常驻）
[主视图] ▸ 总览 ▸ 渠道 ▸ 运维 ▸ 设置        ← 新增主导航（四个 tab）
  ├─ 概览视图: 接入信息 + 接口测试台 + 用量趋势 + 用量分布
  ├─ 渠道视图: 账号矩阵 + 外部账号 + 账号质量
  ├─ 运维视图: 实时动态 + 系统日志 + 会话粘性 + 排队车道 + 语料采集
  └─ 设置视图: 安全设置 + 运维全局操作（清统计等危险按钮收敛至此）
```

### 关键决策与理由
1. **单 HTML 内切 tab（display:none 切换），不做多页面路由**——现在所有 JS 用 `$("panel-x")` 全局 getElementById，hash 路由/多页会破坏 119 个函数的 DOM 假设并成倍改后端；tab 方案零 JS 接口改动
2. **不砍任何功能面板**——只重排分组；账号矩阵与外部账号面板保持现有 JS 渲染逻辑不动（attach 面板节点只是被 display 隐藏，刷新照常）
3. **默认视图=概览**；切换持久化到 localStorage（记住上次视图）
4. **tab 切换时刷新对应视图数据**（视图级懒刷新，避免被隐藏的数据无谓刷新频率不变——简单做法：切到该视图时强制刷一次）
5. **危险操作收敛**："清空全部统计"从用量分布面板挪到安全设置（与清密码同级），用量分布保留纯展示

### 反面方案记录（为什么不做）
- 折叠面板（accordion）：一次仍只能看一块，观测对照弱于 tab
- dashboard.json 驱动的动态面板：过度工程，静态分组已够
- 多页面（stats.html/channels.html）：破坏单文件部署的"复制即用"运维模型（当前云端/桌面只拷 web/ 目录 + config）

## 三、实施拆解（编码手任务书级别）

| # | 文件 | 改动 | 预计 |
|---|---|---|---|
| 1 | web/index.html | ① 主网格外包 nav：`<nav class="view-tabs">概览/渠道/运维/设置</nav>` + `<div id="view-overview/channels/ops/settings">` 四容器 ② 14 个 panel 各归其位（DOM 全部保留，只挪父级） ③ 页脚升级提示 | 40 行级 |
| 2 | web/static/app.css | `.view-tabs`（sticky 顶栏下）、`.view.active`、各视图 grid 重排（概览 3 列→运维 2 列）；`?v=55` 缓存戳 | ~60 行 |
| 3 | web/static/app.js | ① `setView(name)` + localStorage 恢复 + 启动默认 ② dashboard()` 刷新链按视图分组：overview→trend/dist、channels→accounts/providers/quality、ops→activity/logs/gate/affinity/corpus ③ tab 切换触发对应组强刷一次 ④ 清统计按钮调用点迁移提示 | ~80 行 |
| 4 | 后端 | **零改动**（index.html 静态页，面板数据 API 不变） | 0 |
| 5 | 测试 | ① `tests/test_web_assets.py` 扩展：四个视图容器存在、每 panel 恰好归属一个视图、v=55 戳更新 ② 手测清单：登录闸→四视图切换→各面板数据活着→localStorage 记忆 | 3 用例 |

## 四、明确冻结/不动

- 全部后端 API、数据形态、刷新间隔逻辑（仅重排调用分组）
- 现有配色/背景 canvas/scanlines 科技风视觉（view-tabs 用现有 tag/btn 风格语言）
- F1-F15c 的全部面板 JS 函数与 API 契约
- 移动端布局（现无专门适配，本次不加，后续单独立项）

## 五、验收
1. pytest 全绿（含 web asset 用例 3 条新）
2. node --check app.js
3. 云端+桌面部署冒烟：四视图切换、默认概览、渠道视图含账号矩阵+外部账号、运维视图直播正常、设置视图含密码与清统计
4. 手测：手机浏览器窄屏不破版（现有 media query 兼容）

## 六、后续可选（不在本批）
- 图表 canvas 抽组件、暗色主题切换、视图内二级折叠、移动端 drawer 导航、i18n
