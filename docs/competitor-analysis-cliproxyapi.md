# CLIProxyAPI 竞品功能吸收分析

> 分析对象：[router-for-me/CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI)（Go，MIT，55k★，2025-07 起）
> 分析方法：config.example.yaml 全量（72KB/1234 行）+ internal/ 模块树 + docs/management-api-v8.md + README 三语对照
> 结论用途：qidi-relay 功能路线图输入（2026-10-14）

## 一、能力对比矩阵

| 能力域 | CLIProxyAPI | qidi-relay | 判定 |
|---|---|---|---|
| 协议端点 | OpenAI chat+responses / Gemini generateContent+interactions / Claude messages+count_tokens / Codex / Grok / WebSocket | OpenAI chat+responses / Claude messages+count_tokens / embeddings | 端点面它更宽；messages 桥双方同构 |
| OAuth 订阅转 API | Claude Code/Codex/Antigravity/Grok Build/Kimi/Meta/Devin/Vertex 全家（PKCE+自动刷新回写+utls TLS 指纹） | Codely 账号池 + Cline/MiniCode/atomcode 登录态 + opencode-serve 桥 | 它覆盖面大；我们走的是自有渠道生态 |
| 凭证伪装/指纹 | cloak（sensitive-words 零宽混淆、cache-user-id、mcp 工具别名、CCH 签名、fingerprint-profile="claude-code-cli"） | 无 | **它独有**，属风控对抗 |
| 轮换策略 | round-robin / weighted（凭证权重）/ fill-first | 轮换 + GLM 资格路由 + 媒体改路由 | 缺权重 |
| 会话粘性 | session-affinity + TTL + **subagent 继承父凭证（KV cache 最大化）** + **rebind-on 分级（瞬态不重绑/配额认证才重绑）** | affinity：资格过滤直选+成功才记忆 | 它更精细，可吸收 |
| 失败重试 | **轮次制**（request-retry N 轮 + 每轮凭证上限 + 轮间等待） | 一轮遍历 + peek 判负不冷却（A组） | 可吸收轮次制 |
| 冷却 | 408/5xx 与 429 分开计时 + disable 开关 + **.cds 文件持久化（重启不丢）** | 固定 60s、内存态、重启全清零 | **可吸收（痛点）** |
| 保活 | 非流式空行 keepalive / SSE 心跳 / **首字节前 bootstrap-retry** | opencode 桥伪流式（局部） | 可吸收通用化 |
| 请求改写 | **payload 规则引擎**（default/override/filter × 模型/协议/头/payload 条件 × gjson 路径） | 手工散点（vision 改路由、thinking 注入） | **可吸收（架构级）** |
| 每凭证代理 | proxy-url（socks5/http，可 direct/none 覆盖全局） | 无 | **可吸收（解决 IP 封禁痛点）** |
| 服务发现 | mDNS/DNS-SD（_ai-gateway._tcp + 协议子类型） | 无 | 可吸收（多机体验） |
| 管理面 | 管理 API v0/v8 双版 + 面板 GitHub 热更 + secret-key 哈希 + allow-remote | /api/* + SHA256 cookie + allowed_ips | 我们已有对应物，对齐即可 |
| 插件系统 | C ABI 动态库插件 + 插件商店 registry + 多种 store-auth | 无 | 大工程，暂缓 |
| 集群 | redis 队列协议 + Home 并发契约 + postgres/gitstore | 无（单机） | 暂缓 |
| 多媒体 | images gen/edit、视频 ID 凭证缓存 TTL | 媒体改路由（转发层） | 方向不同 |
| 配额降级 | quota-exceeded: switch-project / switch-preview-model | failover（跨凭证）无同渠道模型降级 | 可吸收 |
| 注水检测 | **无** | 指纹+主动探针（v4.3.0） | **我们独有** |
| 语料采集/脱敏 | **无** | redact 16 族 + corpus + seedguard | **我们独有** |
| 客户端预算 | 无 | budget（C组） | **我们独有** |
| TTFT 质量统计 | 无内置（v6.10 起拆给生态） | /api/stats/quality（D组） | **我们独有** |
| 注水/质量面板 | 无 | 5 张运维面板 | **我们独有** |

## 二、第一梯队吸收建议（高价值 × 低/中难度）

### 1. 冷却状态持久化（直击痛点）
- **现状**：冷却是内存态，relay 重启后所有号/Key 立即回池，把已知的 429/402 墙再撞一遍才重新冷却。
- **CPA 做法**：`save-cooldown-status: true` 时把每凭证冷却态写成 .cds 文件，重启加载。
- **吸收设计**：`cooldowns.json`（`{provider/账号: 到期时间戳}`），写盘走既有原子替换纪律；启动时加载并丢弃已过期条目。挂在现有 stats/token_stats 持久化同款机制上。
- 难度：低。文件：proxy_core/accounts.py + providers.py 冷却路径。

### 2. 每 Provider 代理出口（解决 IP 封禁的通用解）
- **现状**：openai-next 按家庭宽带 IP 封禁（403/1010），当时唯一解法是整组改走云端 relay。
- **CPA 做法**：全局 `requests.proxy-url` + 每凭证覆盖（支持 direct/none 显式绕过）。
- **吸收设计**：config `providers.<name>.proxy_url`（socks5/http）；转发时按 provider 实例化带代理的 httpx.Client；`"direct"` 显式不走代理。被 IP 封的渠道接台式机/云出口即可，不必整组搬家。
- 难度：低。文件：routes/proxy.py（Client 构造处）+ providers.py 解析。

### 3. 保活三件套（通用化）
- **现状**：伪流式只在 opencode 桥；非流式慢响应（长思考模型）会超客户端超时；真流式上游静默时 SSE 连接易被中间层掐。
- **CPA 做法**：① `nonstream-keepalive-interval` 非流式响应期间周期发空行防空闲；② 流式 `keepalive-seconds` SSE 心跳；③ `bootstrap-retries` 首字节前安全换号重试。
- **吸收设计**：② SSE 注释行心跳（`: keepalive\n\n`，规范内合法，客户端自动忽略）优先做——10 行级改动；① 非流式上游慢时由转发层用 SSE 封装收尾（复用 opencode 桥的伪流式骨架，抽成通用工具）；③ 我们的 peek 判负不冷却（A组）已等价覆盖，确认即可。
- 难度：②低 ①中。文件：routes/proxy.py 流式路径 + proxy_core/opencode.py 骨架抽取。

### 4. 会话粘性 rebind 分级 + 子会话继承
- **现状**：affinity 成功即记忆，失败语义单一（换号即断绑定）。
- **CPA 做法**：`rebind-on: "quota"` 时瞬态错误（408/5xx/空流）只对本次 failover、**保持原绑定**（保住上游 prompt cache），配额/认证错误（429/401/402/403）才真正重绑；subagent 继承父会话绑定（KV cache 命中最大化、TTFT 最小化）。
- **吸收设计**：affinity 记录增加"最后失败类型"；错误分类复用 peek 嗅探门的判定代码（瞬态/配额两分法已有）；子会话继承可从"请求头带父会话 ID 的客户端"先支持（Claude Code/Codex/OpenCode 的 session 头），无头则维持现状。
- 难度：中。文件：proxy_core/affinity.py + routes/proxy.py 失败路径。

### 5. 凭证权重（加权轮换）
- **CPA 做法**：`strategy: weighted-round-robin`，凭证 JSON 顶层 `weight`（默认 1，非正数=临时排除）。
- **吸收设计**：api_keys dict 元素加 `weight`（F14 的 dict 结构现成）；Codely accounts.json 同步加；选号从纯轮换改加权抽样（大号权重高、试用号权重低、为 0 即软停用——顺带成为 F14 之外的第三种停用形态）。
- 难度：低。文件：proxy_core/providers.py pick_key + accounts.py。

## 三、第二梯队（架构/体验增强）

### 6. payload 规则引擎（运营灵活性的台阶）
- **CPA**：`requests.payload.{default,default-raw,override,override-raw,filter}`，匹配条件=模型通配+协议+请求头+payload 路径（gjson/sjson），动作=缺省注入/强制覆盖/参数删除。
- **吸收设计**：config 加 `payload_rules: [{models:["deepseek-*"], action:{set|default|remove}}]`，实现一个 60 行的 JSON-path 读写器即可起步（我们已有 messages 改写经验）；把现在散在代码里的 thinking 注入、max_tokens 兜底逐步迁成规则。
- 难度：中（先窄后宽：只做模型通配 + set/default/remove 三动作）。

### 7. 重试轮次制
- **CPA**：`request-retry: N` 轮 + `max-retry-interval` 轮间等待（等冷却号回池再打一轮，而不是直接 503）。
- **吸收设计**：failover 遍历完一轮后，若有号处于短冷却且请求可等，等待 min(cooldown) 再来一轮（上限 N 轮/总等待上限）；流式已发字节后不适用（保持现状）。
- 难度：中。

### 8. mDNS 局域网发现
- **CPA**：`server.discovery` 广播 `_ai-gateway._tcp` + 协议子类型 + auth-required 标志。
- **吸收设计**：python `zeroconf` 库（纯 pip），广播 `_qidi-relay._tcp`；面板"接入代码"页显示局域网内其它 relay 实例。多机（台式机/笔记本/云）互找体验质变。
- 难度：低-中。

### 9. 配额降级映射
- **CPA**：quota-exceeded 时 switch-project / switch-preview-model。
- **吸收设计**：providers 可选 `model_fallback: {"旗舰": "备用"}`；429/402 时先同渠道降级再跨渠道 failover。给"旗舰贵号打光了自动用便宜号顶住"一个配置化出口。
- 难度：低-中。

## 四、明确不吸收（含理由）

| 项 | 理由 |
|---|---|
| OAuth 订阅转 API 全家桶（Codex/Antigravity/Grok/Devin OAuth） | CPA 的核心壁垒，工程量巨大且属持续风控对抗；我们自有渠道生态（Codely/Cline/MiniCode/atomcode/opencode）已是同方向小规模实现，扩哪家按需求单点扩，不整体铺开 |
| cloak/指纹伪装 | 风控对抗性质（把非 CLI 客户端伪装成官方 CLI wire 指纹），账号安全风险自担；如某通道确需，单点借鉴 utls 思路即可 |
| 插件系统（C ABI + 商店） | 大工程；Python 侧等价物（动态 import）优先级低，等真实扩展需求出现再议 |
| 集群模式（redis/postgres/Home） | 我们单机 + 双部署（台式机/云）够用；真到多节点时再评估 |
| thinking replay cache / WebSocket 响应 | 依赖各上游 thinking 签名细节与客户端 WS 需求，性价比低 |
| 管理 API 版本化 v0/v8 双轨 | 单实现无需双版本；我们 /api/* + 面板一体已够 |

## 五、我们的护城河（对方没有，继续加固）

1. **注水检测**（tokenizer 指纹 + 主动探针）——渠道质量战的独有武器，可考虑输出成"渠道体检报告"形态运营化
2. **语料采集 + 16 族脱敏 + seedguard**——数据资产线
3. **客户端预算限额**（budget）
4. **TTFT 双口径质量端点 + 5 张运维面板**
5. **文本级 config 保序编辑**（pricing 重复键不丢）——CPA 是 yaml 重写，无此约束
6. **peek 流内嗅探门**（判负不冷却换号）——与 bootstrap-retry 同类但覆盖流中段

## 六、建议路线图（供排期）

| 批次 | 内容 | 预估 |
|---|---|---|
| 吸收-1（速赢） | 冷却持久化 + 每 Provider 代理出口 + SSE 心跳 | 各 0.5 天 |
| 吸收-2 | 凭证权重 + rebind 分级 + 配额降级映射 | 各 1 天 |
| 吸收-3 | 非流式保活通用化 + 重试轮次制 + mDNS 发现 | 1-2 天 |
| 吸收-4（架构） | payload 规则引擎（窄版） | 2-3 天 |

> 注：编号与 qidi-relay 既有 T/F/S 体系独立（用"吸收-N"批次前缀），避免与并行会话撞号；本文件为分析快照，实施规格由实施会话按 T 纪律另行编写。
