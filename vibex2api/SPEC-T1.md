# SPEC-T1 — vibex2api.py 全量实现规格书

> 指挥官已逆向 vibex.runninghub.cn 全部协议。本规格书含完整协议情报 + 模块规格。
> 编码手铁律：只做规格内的事；规格有缺口停手报 `SPEC GAP`；自测跑真实命令贴真实输出；不 git commit。

## 一、背景

VibeX（RunningHub 旗下 vibe coding 产品）内部架构：

```
浏览器 SPA ──REST /api/*──> 网关 ──> 沙箱容器内 cc-server (Claude Code 无头服务端)
       └──WS wss://…/app-ws/{app_id}/ws──> (Cookie 鉴权)
cc-server 用管理员配置的 ANTHROPIC_BASE_URL + ANTHROPIC_AUTH_TOKEN 调上游 Claude(Haiku/Sonnet/Opus)
```

目标：写一个**单文件适配器** `vibex2api/vibex2api.py`，把 VibeX 的对话能力暴露为 **OpenAI 兼容 API**（/v1/models + /v1/chat/completions，支持 SSE 流式），用用户自己的账号额度。

## 二、改动范围

- 新增：`vibex2api/vibex2api.py`（唯一代码文件，目标 700~900 行）
- 不许动：`tmp/`、仓库其它一切
- 依赖：**仅 aiohttp**（已装 3.14.3，Python 3.12）。禁止引入其它第三方库

## 三、协议规格（逆向成果，照抄实现，不许"优化"）

### 3.1 配置

文件 `vibex2api/vibex2api.config.json`（运行时生成/更新，与脚本同目录）。默认值（常量 `DEFAULTS` dict）：

```python
DEFAULTS = {
    "base_url": "https://vibex.runninghub.cn",
    "listen_host": "127.0.0.1", "listen_port": 8787,
    "api_key": "",                # 适配器自身鉴权；空=不校验
    "token": "",                  # Rh-Accesstoken；环境变量 RH_ACCESSTOKEN 优先
    "tenant_id": "",              # X-Tenant-Id；空则自动 uuid4 生成并持久化
    "app_name": "vibex2api",      # 复用的沙箱项目名
    "app_type": "web",
    "model": "",                  # 默认 provider id；空=自动选(优先 billing_tier=="lite")
    "fresh_session": True,        # 每请求先发 new_session（对齐 OpenAI 无状态）
    "turn_timeout": 900,          # 单轮秒
    "ready_timeout": 90,          # WS init→ready 秒
    "queue_limit": 4,             # 排队并发上限，超出 429
    "heartbeat_s": 15,            # SSE 心跳注释间隔
    "include_thinking": False,    # thinking_delta → delta.reasoning_content
    "expose_tools": False,       # tool_use → 文本 "[tool: 名字]" 注入流
    "chat_instruction": "",       # prompt 尾部自定义指令；空=用内置默认指令
    "connect_mode": "auto",      # auto=首次连接用 init_root，之后用 init
}
```

加载规则：DEFAULTS ← config.json 覆盖 ← env `RH_ACCESSTOKEN` 覆盖 token；tenant_id 为空 → 生成后立即写回 config.json 持久化（写回时仅含 DEFAULTS 键）。

### 3.2 JWT 解码（函数 `jwt_decode(token) -> dict|None`）

token 是 JWT。`payload = b64url(token.split(".")[1])`，补 `=` padding，JSON 解析。失败返回 None。取 `sub`（userId）、`nickName`/`user_name`/`username`/`mobile`（username）、`exp`。

### 3.3 鉴权头（HTTP）

```python
headers = {
    "Content-Type": "application/json",
    "Authorization": f"Bearer {token}",
    "RH-TOKEN": token,
    "X-Tenant-Id": tenant_id,
    "X-Vibex-Locale": "zh-CN",
    "Origin": base_url, "Referer": base_url + "/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
}
if JWT 解析成功且有 sub:
    headers["userId"] = str(sub)
    headers["username"] = str(nick 或 sub)
```

### 3.4 REST 端点（base_url + 路径）

未授权响应统一是 `{"code":403,"msg":"TOKEN_MISSION"}`（或 TOKEN_INVALID/401/412）→ 适配器抛 401 级错误，提示"重新从浏览器提取 Rh-Accesstoken"。

| 调用 | 方法/路径/body | 返回（容错解包） |
|---|---|---|
| providers | GET `/api/llm-providers` | dict，列表在 `.providers`（兜底 items/data） |
| apps_list | GET `/api/apps` | 顶层数组，或 dict 的 `.apps`/`.items`/`.data`(可为 dict 再套 apps/items) |
| app_get | GET `/api/apps/{id}` | app 对象 |
| app_create | POST `/api/apps` body `{"name":..,"pinned_host_ip":None,"llm_provider_id":..,"app_type":"web","flow":None,"enabled_capabilities":None}` | app 对象（顶层的 `app_id`，兜底 `.data`/`.app` 包裹）；响应 code!=0 抛错，含 `FREE_PROJECT_LIMIT` 时抛 409"免费项目已达上限(约10个)" |
| app_start | POST `/api/apps/{id}/start` body `{}` | 然后轮询 app_get：`live_status(app)=="running" and ports_ok(app)` 每 2s，最长 240s；超时时若 status=="running" 也放行 |
| set_llm_provider | PATCH `/api/apps/{id}/llm-provider` body `{"provider_id":..,"llm_test":False}` | |
| ensure_llm_settings | POST `/api/apps/{id}/ensure-llm-settings` body `{}` | 失败仅 log 警告不抛 |

判定函数：
- `live_status(app)`: `app.live.status`（live 是 dict、status 是 str、且 live 无 "error" 键）否则 `app.status_cached`
- `ports_ok(app)`: `app.host_ports["9000"]>0 且 ["7000"]>0`（对齐前端 Lh()；host_ports 非 dict → False）

### 3.5 WS 协议（核心）

URL：`wss://<base host>/app-ws/{app_id}/ws`（把 base_url 的 https→wss）。**鉴权只带 Cookie**：`Cookie: Rh-Accesstoken={token}`（另加 Origin/UA）。

客户端→服务端（全部 JSON）：

```json
{"type":"init",      "cwd":"/workspace/app", "replay":false}                    // 二次及以后连接
{"type":"init_root", "cwd":"/workspace/app", "meta":{...}, "replay":false}       // 首次连接(对齐前端)
{"type":"new_session"}
{"type":"prompt", "text":"...", "permission_mode":"dontAsk"}
{"type":"ping"}                                                                  // 每20s
{"type":"cancel"}                                                               // 超时中断用
```

init_root 的 meta（从 app 对象提取）：`{app_id, name, app_type, image, host_ip, host_ports, created_at, pocketbase_url}`，若 `app.flow` 是字符串则加 `flow`，若 `enabled_capabilities` 是 list 则拷贝。

服务端→客户端（消息顶层 `type` 字段分支）：

| type | 载荷 | 处理 |
|---|---|---|
| ready | {cwd} | 标记就绪；只有收到 ready 才能发后续消息 |
| pong | | 忽略 |
| claude_event | {event: CC} | **核心事件泵**，见 3.6 |
| done | {context_overflow, session_poison, balance_insufficient, rate_limit_failover, failover_model, primary_model} | **本轮结束**（result 事件通常先到） |
| error | {message, code} | ready 前收到 → 连接失败抛错；ready 后 → 记为轮内错误 |
| context_compacting / context_compacted | | 忽略或注入提示 |
| 其它（sessions/session_info/run_status/cancel_ack/history_*/source_*/ws_send_dropped/ws_close/retract_last_assistant/dev_server_restarted） | | 忽略 |

### 3.6 claude_event.event 分支（Claude Code stream-json 格式）

```python
ev = msg["event"]; et = ev.get("type")
if et == "stream_event":
    inner = ev["event"]                      # Anthropic SSE 事件
    if inner.get("type") == "content_block_delta":
        d = inner["delta"]
        if d.get("type") == "text_delta":     产出 ("text", d["text"])
        if d.get("type") == "thinking_delta": 产出 ("thinking", d["thinking"])
elif et == "assistant":                        # 一条完整 assistant 消息（多块）
    for blk in ev["message"]["content"]:
        blk.type=="text"     → 产出 ("assistant_text", blk.text)
            若 ev.isApiErrorMessage 或 ev.balance_insufficient → 产出 ("error_text", blk.text)
        blk.type=="tool_use" → expose_tools 时产出 ("tool", f"[tool: {blk.name}]")
elif et == "result":                           # Claude Code 最终结果
    产出 ("result", {"subtype": ev.get("subtype"),
                     "result": ev.get("result") if str else None,
                     "usage": ev.get("usage")})
# et 为 "system"/"user" → 忽略
```

### 3.7 一轮对话完整时序（Vibex.run_turn）

```
连接 WS（30s 连接超时）
→ 发 init_root(首次) 或 init（connect_mode=="auto" 时首次 init_root；显式 "init" 恒用 init）
→ 循环收消息等 ready，同时若先收到 error → 抛连接失败；90s 无 ready → 抛 504
→ POST ensure-llm-settings（失败忽略）
→ 若 fresh_session: 发 new_session
→ 发 prompt
→ 事件泵（deadline = now + turn_timeout）:
    收 claude_event → 按 3.6 转产出发到 out 队列
    收 done → 把 flags 发 ("done", flags)，结束泵
    收 error(ready 后) → ("error", message)
    超时 → 发 cancel + ("note", "…超时…") + ("done", {"timeout":True})
    WS 关闭 → ("note","连接关闭") + ("done",{"ws_closed":True})
→ finally: 队列放哨兵 (None,None)；ping 任务 cancel
```

out 队列元素约定：`(kind, value)`，kind ∈ text / thinking / assistant_text / error_text / tool / note / result / done / error / None(哨兵)。

### 3.8 模型（Provider）解析（Engine.resolve_model）

1. `providers()` 取列表（空 → 502）
2. requested 非空：精确 id 匹配 → display_name/label 匹配 → 不匹配继续
3. config["model"] 非空且能匹配 id → 用它
4. 兜底：第一个 `billing_tier=="lite"`，否则第一个

### 3.9 沙箱项目管理（Engine.ensure_app）

1. `apps_list()` 里找 `name==config.app_name` 或 `app_id==config.app_name` 的项目
2. 找不到 → `app_create(app_name, provider.id)`；错误含 FREE_PROJECT_LIMIT → 抛 409 带指引
3. `app_start(app_id)`（含轮询）
4. `set_llm_provider(app_id, provider.id)`（仅当与上次不同，缓存 Engine.current_provider；失败警告不抛）
5. 缓存 app 对象，返回

并发：`asyncio.Lock`（turn_lock）保证单 app 同时只跑一轮；`asyncio.Semaphore(queue_limit)` 控排队，满时返回 429。

### 3.10 messages → 单条 prompt（build_prompt）

- system/developer 角色内容 → 前缀块 `【系统设定】\n<拼接>`
- user/assistant/tool（tool 标"工具输出"）→ `【对话记录】\n【用户】\n<text>\n\n【助手】\n<text>…`
- 尾部 `【任务】\n<指令>`：指令 = config.chat_instruction 或内置默认：
  "请直接以「助手」的身份回答最后一条用户消息: 直接给出回答内容本身; 这是一次纯问答, 无需创建或修改任何项目文件。"
- **特例**：仅 1 条 user 且无 system → 直接返回该文本原文（不加包装，除非 chat_instruction 非空则附加）
- content 为 list 时：取每个块的 `.text`；`image_url` 块替换为 `[图片内容不支持, 已忽略]`；其余 str 直接用

### 3.11 OpenAI HTTP 层（aiohttp web）

**鉴权**：config.api_key 非空时，请求头必须 `Authorization: Bearer <api_key>`，否则 401。

**GET /v1/models** →
```json
{"object":"list","data":[{"id":"<provider.id>","object":"model","created":0,"owned_by":"vibex",
 "vibex":{"display_name":..,"billing_tier":..,"description":..}}]}
```

**POST /v1/chat/completions**：
- 入参校验：messages 非空 list；stream bool
- 错误响应统一 `{"error":{"message","type":"vibex_error","code"}}`
- 非流式：聚 text+error_text 全部 → content；空 content 且有 error → 502；done.balance_insufficient → 402；空 → 502 EMPTY_TURN。返回标准 completion JSON（usage 见下）
- 流式 SSE：
  - 头：`Content-Type: text/event-stream; charset=utf-8`、`Cache-Control: no-cache`、`X-Accel-Buffering: no`
  - 首块 `delta {"role":"assistant"}`；text → `delta {"content": ...}`；thinking（config 开启）→ `delta {"reasoning_content": ...}`；assistant_text 忽略（text_delta 已覆盖）；tool/note → content 文本；心跳：队列 get 超时 heartbeat_s 秒 → 写 `: hb\n\n`
  - 终止：done 到达 → 收尾块（finish_reason："stop"；若 flags.balance_insufficient 先注入 note）；error 且无任何内容 → 直接写 OpenAI 错误 JSON 事件再 [DONE]
  - `data: {...}\n\n`，最后 `data: [DONE]\n\n`
  - usage：result 事件里有 usage（input_tokens/output_tokens）→ 最后一块附 `"usage":{"prompt_tokens":input,"completion_tokens":output,"total":...}`，无则 0
- 响应 model 字段回显请求的 model（无则 provider id）
- id：`chatcmpl-` + uuid4 hex 24 位

### 3.12 CLI

- `python vibex2api.py` → 启动服务，打印地址/token 状态/风险提示
- `--check` → 解码打印 JWT payload；打印 providers 列表（id/tier/display_name）；打印现有 apps（app_id/name/status/ports）；token 未配置打印指引并退出码 1
- `--selftest` → **不需要 token**：
  - jwt_decode：手工构造 `base64url(header).base64url(payload).sig` 断言解析出 sub
  - build_prompt：4 条 messages 断言含"【系统设定】【用户】1+1=？"；单条 user 特例断言输出 == 原文
  - extract_text：多模态 list 断言文本拼接
  - 错误格式：oai_error 是合法 JSON
  - 全过打印 `SELFTEST PASS`
- `--help`：打印文件 docstring

Windows 控制台：入口处 `sys.stdout.reconfigure(encoding="utf-8", errors="replace")`（包 try）。

## 四、模块结构（钉死，按序实现）

```
1. imports + utf-8 stdout + 常量 (CONFIG_PATH/DEFAULTS)
2. log()/load_config()/persist()/jwt_decode()
3. class VibexError(Exception): message/status/code
4. oai_error(status,message,code,typ) -> web.json_response
5. class Vibex: __init__/start/close/_headers/api/providers/apps_list/app_get/
              app_create/app_start/set_llm_provider/ensure_llm_settings/
              run_turn(app,prompt,first_connect,fresh_session,out,turn_timeout)/_pinger
6. 辅助: _loads/_unwrap_obj/_live_status/_ports_ok/extract_text/build_prompt/_oai_usage
7. class Engine: ensure_started/resolve_model/ensure_app
8. handlers: handle_models/handle_chat/_do_turn(流式+非流式)
9. make_app() + cmd_check/cmd_selftest/cmd_serve/main()
```

**已知陷阱（必须避开）**：
- aiohttp WS：`session.ws_connect(url, headers=..., autoping=True, heartbeat=None, max_msg_size=0)`；`ws.receive(timeout=秒)` 会抛 `asyncio.TimeoutError`
- `ws.close_code` 在 CLOSED 后才有值
- aiohttp web.StreamResponse：`await resp.prepare(request)` 后 `await resp.write(bytes)`，结束 `await resp.write_eof()`
- Semaphore 满判断：不要碰私有 `_value`；用 `queue_sem.locked()`（表示满）即可
- 所有 f-string JSON 输出用 `ensure_ascii=False`
- 服务器 handler 签名 `async def h(request)`；StreamResponse 的 prepare 需要 request 对象（把 request 传进 _do_turn）

## 五、验收标准（编码手自测 + 指挥官复跑）

- [ ] `python vibex2api.py --selftest` 输出 `SELFTEST PASS`，退出码 0
- [ ] `python vibex2api.py --help` 正常打印
- [ ] `python vibex2api.py --check`（无 token）打印指引且退出码 1，不崩溃
- [ ] `python -c "import ast,sys; ast.parse(open('vibex2api/vibex2api.py',encoding='utf-8').read()); print('SYNTAX OK')"` 通过
- [ ] 启动服务（无 token）：`/healthz` 200 且 `token_configured:false`；`/v1/models` 返回 401 错误 JSON（message 含"Rh-Accesstoken"字样）；`POST /v1/chat/completions` 空 body → 400
- [ ] 仅 import aiohttp/标准库，无其它第三方 import
- [ ] 文件 UTF-8 保存，中文注释

自测方法提示（编码手要真的跑）：用后台启动 `python vibex2api.py`（改随机端口避免冲突，如 --check 后用 config 临时端口或直接 Start-Job），curl/Invoke-WebRequest 打 /healthz、/v1/models，贴真实输出；测完杀进程。
