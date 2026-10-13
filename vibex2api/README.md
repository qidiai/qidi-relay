# vibex2api

把 **VibeX（RunningHub 旗下 vibe coding 产品）** 的对话能力封装成 **OpenAI 兼容 API** 的**单文件适配器**，直接使用你自己 VibeX 账号的额度。

```text
OpenAI client ───> vibex2api (127.0.0.1:8787)
                      ├─ REST /api/*                ───> VibeX 网关
                      └─ WS   /app-ws/{app_id}/ws   ───> 沙箱 cc-server ───> 上游 Claude
```

---

## ⚠️ 风险与合规声明

- 这是**非官方用法**：通过逆向 VibeX 的私有接口 / WebSocket 协议，把它的对话能力转发成 OpenAI 接口，官方不提供、也不认可这种接入方式。
- 可能**违反 RunningHub 的用户协议**，存在**账号被风控 / 封禁**的风险，请自行评估并承担后果。
- 官方宣传的“每日 100 亿 Token”属于**营销话术**：实际免费额度通常按**结算周的调用次数**计（部分档位按 ×2 计扣），并不是真的按 Token 总量发放。
- 仅建议**个人本机自用**，请勿转售、二次分发或大规模滥用。

---

## 两种安装形态

- **源码版**：本文档流程（需要 Python 3.10+ 与 aiohttp）
- **EXE 免安装版**：单文件 `vibex2api.exe`（约 14MB，PyInstaller 打包，无需 Python），
  双击运行、首次交互式填 token。见 [使用说明-EXE版.md](使用说明-EXE版.md)

## 快速开始

1. **前置**：Python 3.10+（本适配器在 3.12 验证）、`pip install aiohttp`；注册并登录 https://vibex.runninghub.cn 。
2. **提取 token**：浏览器按 `F12` → **Application** → **Local Storage** → `https://vibex.runninghub.cn` → 找到键 **`Rh-Accesstoken`** → 复制它的值（是一个 JWT，实测约 30 天有效期，过期后重新提取）。
3. **配置**：把 `config.example.json` 复制为 `vibex2api.config.json`，填入 `token`；或设置环境变量：

   ```bat
   set RH_ACCESSTOKEN=<粘贴 Rh-Accesstoken 的值>
   ```

   ```powershell
   $env:RH_ACCESSTOKEN="<粘贴 Rh-Accesstoken 的值>"
   ```

   > 环境变量 `RH_ACCESSTOKEN` 的优先级高于配置文件里的 `token`。

4. **校验**：`python vibex2api.py --check`
   （应打印 JWT payload、providers 列表、现有 apps；未配置 token 时打印指引并退出码 1。）
5. **启动**：`python vibex2api.py`（默认监听 `127.0.0.1:8787`）。
6. **接入**：要点如下——
   - `base_url` 用 `http://127.0.0.1:8787/v1`
   - `model` 填第 4 步 `--check` 里看到的 provider id
   - 适配器未设置 `api_key` 时，客户端 `api_key` 随便填一个非空值即可

**A) Python（openai 库，流式打字机）**

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8787/v1", api_key="sk-anything")
stream = client.chat.completions.create(
    model="<provider-id>",          # 用 `python vibex2api.py --check` 打印的 provider id
    messages=[{"role": "user", "content": "你好，用一句话介绍你自己"}],
    stream=True,
)
for chunk in stream:
    print(chunk.choices[0].delta.content or "", end="", flush=True)
```

**B) curl**

```bash
curl -s http://127.0.0.1:8787/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"<provider-id>","messages":[{"role":"user","content":"你好"}],"stream":false}'
```

> 若配置里设置了 `api_key`，请追加请求头 `-H "Authorization: Bearer <api_key>"`。

---

## 配置项

配置文件为与脚本同目录的 `vibex2api.config.json`；`config.example.json` 是全部键的模板（`token` / `api_key` / `tenant_id` / `model` / `chat_instruction` 默认留空）。

| 名称 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `base_url` | string | `"https://vibex.runninghub.cn/vc"` | VibeX 网关地址（REST 需要 `/vc` 前缀；WebSocket 由适配器自动去掉前缀直连 `/app-ws/...`） |
| `listen_host` | string | `"127.0.0.1"` | 适配器监听地址，建议保持本机回环 |
| `listen_port` | number | `8787` | 适配器监听端口 |
| `api_key` | string | `""` | 适配器自身的鉴权；**留空 = 不校验**。设置后客户端须带 `Authorization: Bearer <api_key>` |
| `token` | string | `""` | `Rh-Accesstoken`；环境变量 `RH_ACCESSTOKEN` 优先 |
| `tenant_id` | string | `""` | `X-Tenant-Id`；留空时自动生成 `uuid4` 并写回配置文件 |
| `app_name` | string | `"vibex2api"` | 复用的沙箱项目名（见下方说明） |
| `app_type` | string | `"web"` | 新建沙箱项目时的类型 |
| `model` | string | `""` | 默认 provider id；留空 = 自动选（优先 `billing_tier=="lite"` 免费档） |
| `fresh_session` | boolean | `true` | 每请求先发 `new_session`（对齐 OpenAI 无状态语义） |
| `turn_timeout` | number | `900` | 单轮对话超时（秒） |
| `ready_timeout` | number | `90` | WS `init` → `ready` 的等待超时（秒） |
| `queue_limit` | number | `4` | 排队并发上限，超出返回 429 |
| `heartbeat_s` | number | `15` | SSE 心跳注释间隔（秒） |
| `include_thinking` | boolean | `false` | 把 `thinking_delta` 映射到 `delta.reasoning_content` |
| `expose_tools` | boolean | `false` | 把 `tool_use` 以文本 `[tool: 名字]` 注入输出流 |
| `chat_instruction` | string | `""` | prompt 尾部自定义指令；留空 = 用内置默认问答指令 |
| `connect_mode` | string | `"auto"` | `auto` = 首次连接用 `init_root`、之后用 `init` |

**重点说明**

- **`app_name`**：适配器会按这个名字**复用**沙箱项目（找不到才新建）。免费账号项目数上限约 **10 个**，所以不要频繁改这个值；如果触发免费项目上限，请在网页端删掉旧项目，或把 `app_name` 改成某个已存在项目的名字以复用。
- **`model`**：留空时自动挑选 `billing_tier=="lite"` 的免费档 provider；想指定模型就填 `--check` 打印出来的 provider id。
- **`fresh_session`**：保持 `true` 即为“每轮新会话”（OpenAI 客户端无状态），模型不会跨请求记忆，**必须由客户端带全量 `messages`**。
- **`turn_timeout`**：背后是 Claude Code agent 完整跑一轮，正常 30s ~ 5min，按需调大。
- **`api_key`**：只保护**本适配器**的入口，与 VibeX 账号无关；留空则任何能访问该端口的程序都能消耗你的额度。
- **`include_thinking`**：开启后思考内容会以 `delta.reasoning_content` 随流下发。
- **`expose_tools`**：开启后 agent 的工具调用会以文本形式出现在回复里，便于调试。

---

## 端点

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/v1/models` | 列出可用 provider，格式为 OpenAI `{"object":"list","data":[...]}` |
| POST | `/v1/chat/completions` | 对话补全；`"stream": true` 走 SSE，否则一次性返回 |
| GET | `/healthz` | 健康检查，返回 `token_configured` 等状态 |

---

## 错误对照

统一返回 OpenAI 风格 `{"error":{"message":...,"type":"vibex_error","code":...}}`。

| HTTP | code | 含义与处理 |
| --- | --- | --- |
| 401 | `TOKEN_NOT_CONFIGURED` | token 缺失/过期 → 重新从浏览器提取 `Rh-Accesstoken` |
| 402 | `BALANCE_INSUFFICIENT` | 上游余额不足，无法完成本轮 |
| 409 | 免费项目已达上限 | 网页端删除旧项目，或改 `app_name` 复用现有项目 |
| 429 | `QUEUE_FULL` | 并发排队已满，稍后重试（或调大 `queue_limit`） |
| 502 | `UPSTREAM_ERROR` / `EMPTY_TURN` | 上游错误 / 本轮无内容 → 查看服务端日志 |
| 504 | — | 沙箱启动或 WS 就绪超时（`ready_timeout` / 启动轮询超时） |

---

## 已知限制

- **单条回复慢**：背后是 Claude Code agent 完整跑一轮，正常 **30s ~ 5min**。
- **无跨请求记忆**：对话是“每轮新 session”，模型不记得上一轮；客户端必须每次都带全量 `messages`。
- **图片输入不支持**：`image_url` 等图片块会被替换为占位符 `[圖片內容不支持, 已忽略]`。
- **同一时刻只处理一轮**：适配器对单个沙箱项目串行化，其余请求排队（超过 `queue_limit` 直接 429）。

---

## 故障排查

- **PowerShell 里中文乱码**：先执行 `$env:PYTHONIOENCODING="utf-8"`，或 `chcp 65001` 切到 UTF-8 代码页。
- **token 约 30 天过期**：`Rh-Accesstoken` 实测 30 天失效（JWT exp 字段），出现 401 时重新从浏览器复制。
- **离线自检**：`python vibex2api.py --selftest`（不需要 token、不联网），全部通过会打印 `SELFTEST PASS`。
- **查看帮助**：`python vibex2api.py --help`。
