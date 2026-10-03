# wb — WorkBuddy(腾讯 CodeBuddy 桌面端) 接入

本目录让「本地局域网中转-v3」可以复用 **WorkBuddy 桌面端**已登录的额度：
一个把 Anthropic Messages 协议翻译成 OpenAI 协议的反代，以及一个每日自动签到脚本。

上游业务 API：`https://copilot.tencent.com`（桌面端自带登录态，非官方公开 API）。

目录内容：

| 文件 | 作用 |
| --- | --- |
| `workbuddy2api.py` | OpenAI → Anthropic 协议适配器，监听 `127.0.0.1:8768` |
| `wb_checkin.py` | 每日签到脚本（纯标准库，无依赖） |
| `run_checkin.vbs` | 计划任务无窗口包装（wscript 拉起 python） |
| `checkin.log` | 签到日志（运行时生成） |

---

## 一、签到自动化

手动跑一次：

```
python G:\程序开发\本地局域网中转-v3\wb\wb_checkin.py
```

注册每天 09:30 的计划任务（无窗口）：

```
schtasks /create /f /tn "wb-checkin" /tr "wscript.exe G:\程序开发\本地局域网中转-v3\wb\run_checkin.vbs" /sc daily /st 09:30
```

结果追加写入同目录 `checkin.log`，每行形如：

```
2026-09-17 09:30:01  ALREADY_CHECKED_IN  credits_total=2808
```

退出码：`0` 成功或今天已签 / `1` 签到失败 / `2` 状态检查失败。

---

## 二、反代服务

启动：

```
python G:\程序开发\本地局域网中转-v3\wb\workbuddy2api.py
```

监听 `http://127.0.0.1:8768/v1`（与 vibex2api 的 8787 不冲突）：

* `GET /healthz` — 健康检查，返回 `{"status":"ok","token_configured":true,"uid_prefix":"..."}`
* `GET /v1/models` — 30 个可用模型，附 `wb_credits` 倍率字段
* `POST /v1/chat/completions` — OpenAI Chat Completions（流式 / 非流式均可）

在中转 `config.json` 的 `providers` 里加一段（模型名加 `wb-` 前缀做映射，避免与其它上游撞名）：

```json
"workbuddy": {
  "base_url": "http://127.0.0.1:8768/v1",
  "models": ["wb-hy3", "wb-glm-5.3-flash", "wb-deep-model"],
  "api_keys": [{"key": "local-workbuddy2api", "label": "本机workbuddy2api"}]
}
```

适配器内部对上游使用模型原名（不加 `wb-` 前缀），前缀映射由中转侧负责。

**Token 免维护**：适配器的凭据直接读取桌面端的
`%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\workbuddy-desktop.info`，
且**每次上游请求都重读**。桌面端会自动刷新该文件（有效期 60 天），所以只要桌面端保持登录，
本适配器就自动续期，无需手工同步 token。读不到文件时会返回 502 并提示先登录桌面端。

---

## 三、风险声明

* **非官方用法**：上游是桌面端内部接口，腾讯可随时变更协议或收紧风控；接口失效时本目录需重新逆向。
* **credits 倍率即计费倍率**：`GET /v1/models` 的 `wb_credits` 是相对计费倍率（如 `x0.79`），实际消耗要乘以所调模型的倍率。
* **免费模型**：`hy3` 与 `hy4-preview` 为 `x0.00`（不扣 credits），适合高频/测试调用；免费额度随时可能调整。
* **仅供个人在本机复用自己已登录的额度**，请勿对外分发或用于商业转售。

---

## 四、余额查询（2026-09-18 破解 10085）

`wb_checkin.py` 每次签到后顺带查真实余额，日志示例：

```
2026-09-18 01:10:56  ALREADY_CHECKED_IN  credits_total=2907.97/2908
```

原理：billing 资源接口有腾讯 TuringShield 设备指纹校验，裸 Bearer 一律
403/10085（"请求不合法"）。解法：

| 文件 | 作用 |
| --- | --- |
| `wb_turing_token.js` | 用桌面端同款 N-API binding（native/turing-sdk，channelId=109144）取设备风控 Token（约 5 分钟有效，现取现用） |
| `wb_checkin.py` | `_fetch_device_token()` 调上面的 js；`fetch_credits()` 带全套头请求余额接口 |

必带请求头（缺一即 10085）：

- `X-Device-Token`: TuringShield 设备 Token
- `User-Agent: WorkBuddy/5.5.6`（国内版版本号）
- `X-IDE-Type / X-IDE-Name / X-IDE-Version / X-Product: WorkBuddy`

接口：主用 `/billing/meter/get-user-resource-summary`（#97550，无 /v2 前缀，
按资源包汇总剩余/总量），回退 `/v2/billing/meter/get-user-resource` 的 TotalDosage。

依赖：系统 PATH 里有 node（任意 v18+，N-API ABI 稳定）；桌面端保持登录
（binding 与桌面同源，token 文件自动跟随刷新）。
