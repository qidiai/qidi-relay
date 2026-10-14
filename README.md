# QiDi Relay v4 — 局域网代理控制台（模块化重构版）
> ⚠️ **许可协议 / License**: 本项目采用 [CC BY-NC 4.0](./LICENSE)（署名-非商业性使用 4.0 国际）——个人学习、研究、非商业用途可自由使用与修改；**未经作者书面授权，禁止任何商业用途**。/ This project is licensed under **CC BY-NC 4.0** — free for personal and noncommercial use; **commercial use is prohibited** without written permission.


基于 v3 的完整重构：后端模块化拆分 + 全新 HUD 风格控制台前端。
**数据文件（accounts.json / config.json / token_stats.json）与 v3 完全兼容。**

## 目录结构

```
main.py              入口：创建 Flask 应用、注册蓝图、启动服务
proxy_core/
  settings.py        常量、路径、配置加载（端口可在 config.json 配 "port"）
  accounts.py        AccountManager  账号池：轮询/启停/GLM 标记/JWT 过期解析
  keystore.py        ApiKeyCache + LiteLLM 请求签名（X-Codely-Signature）
  providers.py       外部模型源：OpenAI 兼容静态 key / atomcode 签名 / Cline / MiniCode 官方通道
  cline.py           Cline 官方通道：客户端凭证池、token 自动刷新回写、免费通道风控
  minicode.py        MiniCode（mcode）官方通道：mcode 登录态凭证、OAuth 刷新轮换写回、
                     OpenAI ↔ Anthropic Messages 协议转换（请求/响应/SSE 桥）
  anthropic.py       Anthropic Messages ↔ OpenAI 协议适配（请求/响应/SSE 桥/token 估算）
  router.py          角色别名路由与进行中负载注册表
  stats.py           TokenStats（累计 + 小时级历史桶）/ ActivityLog（实时动态）
  auth.py            密码 SHA256 哈希、check_admin_auth 装饰器
  state.py           全局单例与服务启动时间
routes/
  proxy.py           /health、/v1/models、/v1/chat/completions、/v1/responses、
                     /v1/messages（+ /v1/messages/count_tokens）、/v1/embeddings、/codely/models（兼容旧路径）
  admin.py           /api/* 管理接口（账号/配置/登录/日志/统计/动态）
web/
  index.html         控制台页面
  static/app.css     HUD 主题样式
  static/app.js      前端逻辑（无外部依赖，离线可用）
```

## 相对 v3 的新增能力

| 能力 | 说明 |
|---|---|
| 用量趋势图 | `TokenStats` 新增小时级历史桶（保留 7 天），`GET /api/stats/history?hours=24` |
| 实时请求动态 | 内存环形缓冲记录每次转发的模型/账号/耗时/token，`GET /api/activity` |
| API 测试台 | 页面内直接发聊天请求（支持流式），验证链路无需打开客户端 |
| 一键接入 | Base URL / cURL / Python / JS 片段一键复制 |
| 账号健康卡 | Token 剩余时间进度条、配额/GLM 徽章、启用开关 |
| 日志终端 | 级别过滤、暂停/继续、自动滚底 |
| 端口可配置 | config.json 增加 `"port": 8766` 即可改端口 |
| Anthropic 兼容 | `POST /v1/messages`（文本/图片/工具调用/流式 SSE 桥）+ `POST /v1/messages/count_tokens`（本地估算、不打上游），Claude Code / Anthropic SDK 可直接接入 |
| Cline 官方通道 | provider `"type": "cline"`：凭证跟随 Cline 客户端登录态（providers.json mtime 感知）、token 过期自动刷新并原子回写、客户端指纹头、免费模型串行门 + 429 冷却解析 |
| MiniCode 官方通道 | provider `"type": "minicode"`（2026-10-01）：MiniMax mcode 登录态反代（`~/.minimax/auth/prod/cn/mcode-public/auth.json`，mtime 感知），上游 Anthropic Messages 协议自动转 OpenAI；token 短时效自动 OAuth 刷新（refresh_token 轮换立即落盘，generation 不动与官方客户端共存）；模型 `mcode-m3`（512K/图像输入/thinking 可关）/ `mcode-m3.1-flash`（effort 档位）/ `mcode-m2.7-highspeed` / `mcode-m2.7`；429 冷却解析、瞬时 400 限次重试、管理面板通道状态 |
| Anthropic 型号别名 | config.json 的 `anthropic_model_aliases` 把客户端内置型号名（`claude-*`）收编到中转真实模型，仅 `/v1/messages` 生效 |
| 每渠道代理出口 | `providers.<name>.proxy_url`（吸收-1/2 §R-B）：http/https/socks5 三种代理，空=回退全局 `requests.proxy_url`，`"direct"`=显式直连覆盖全局。转发、面板「探测模型」、注水探针三处出口一致；所有按 provider 构造的连接 `trust_env=False`（不吃 HTTP(S)_PROXY 环境变量，`direct` 语义才成立）。用例：某上游按家庭宽带出口 IP 403 时只给该渠道挂代理，其余渠道不动。socks5 需 `socksio`（安装器已带 `httpx[socks]`） |
| SSE 心跳保活 | `requests.keepalive_seconds`（吸收-1/2 §R-C，默认 15 秒开启）：上游思考型模型 prefill 静默期不吐字节时，每 N 秒下发一条 SSE 注释行 `: keepalive` 防中间层/客户端掐连接；`0` = 关闭（回滚开关）。实现为「后台读线程 + Queue」，同一上游连接不中断；心跳不混入数据帧、不重置流内嗅探门死限；`/v1/messages` 与 `/v1/responses` 两桥对 `:` 注释行原样透传（Claude Code 等主力客户端可见）；每流心跳次数聚合进该请求的动态条目（`keepalives` 字段），不逐条灌 `ActivityLog` |
| 会话粘性失败分级 | `routing.rebind_on`（吸收-1/2 §R-D）：`"default"`（缺省）= 现状零改动（成功者覆写绑定、失败不动绑定）；`"quota"` = CPA 语义——transient 失败（读超时/连接重置/5xx/空流/截断）本次换号但**保持绑定**（保 prompt cache），hard 失败（401/402/403/429/404/budget_exhausted/OAuth invalid_grant）**持久清绑定**，且仅「命中绑定账号且成功」才覆写绑定。失败面×归类映射见 `proxy_core/classify_failure.py`；观测见 `GET /api/affinity/stats` 的 `rebind_distribution` |
| 渠道接入示例 | apigoto 聚合网关完整接入步骤（面板添加 / config 手写 / 多 Key 轮换 / 停用启用）见 [docs/apigoto.md](docs/apigoto.md) |

## 启动

```
双击 启动代理-v2.bat
或: py -3 main.py
```

管理界面：`http://<本机IP>:8765`（已正式替换旧版；如需改端口，编辑 config.json 的 `"port"`）。

## 客户端接入

### OpenAI 兼容客户端（Cline / Cursor / zcode / qidicode …）

- Base URL: `http://<本机IP>:<端口>/v1`
- API Key: 任意值（如 `sk-anything`），代理自动轮换上游账号
- 模型名：见 `GET /v1/models`（池模型 + 各 provider 模型 + 角色别名）

### Anthropic 协议客户端（Claude Code / Cline 的 Anthropic Provider 模式）

- `ANTHROPIC_BASE_URL=http://<本机IP>:<端口>`（官方 SDK 自己拼 `/v1/messages`，不要再带 `/v1`）
- `ANTHROPIC_AUTH_TOKEN`：config.json 配了 `api_keys` 时填其中一个 key；未开启鉴权时填任意值
- 模型名：客户端内置的官方型号名（`claude-sonnet-4-5-20250929` 等）由 config.json 的
  `anthropic_model_aliases` 收编（默认 `claude-*` → `cline-free/mimo-v2.6-flash`，走 Cline 免费通道）；
  也可以直接把模型名填成本中转的真实模型名。**未被收编的名字仍按模型名守卫返回 404**，
  不会静默改写假活（键支持精确名与尾部 `*` 前缀，精确项写在通配项之前优先命中）
- 支持文本 / 图片 / 工具调用 / 流式；`/v1/messages/count_tokens` 由本地估算回答（不打上游、不记账）

### 并行开发实例（不碰生产 8765）

    $env:RELAY_PORT=8899 ; py -3 main.py

同目录起第二实例、只改端口不改配置；生产重启/切换仍由用户手动执行 `start_relay.ps1 -Force`。

## 回归测试

    py -3.12 -m pytest tests -q          # 42 用例：换号/冷却/截断/Anthropic 适配，全程打本地 mock 上游
