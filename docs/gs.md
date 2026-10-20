# Google AI Studio 渠道接入（2026-10-20）

## 架构

```
客户端 → 中转:8765 → gs 渠道（gs/ 前缀）→ generativelanguage.googleapis.com
         （OpenAI 兼容端点 /v1beta/openai）→ 出口固定首尔 socks5(1080)
```

- **凭证**：18 个月 Google AI Pro 订阅账号的 AI Studio key（`AQ.` 新版格式）
- **出口**：`providers.gs.proxy_url = socks5://127.0.0.1:1080`（Google 国内
  直连超时，实测出口 IP = 首尔 43.155.167.164； socksio 已入中转 Python）
- **计费**：AI Studio 免费档（订阅抬限额）；旗舰模型免费档额度为 0

## 可用模型（实测分级，2026-10-20）

**✅ 免费档可用（11 个，已上架）**
gemini-3.6-flash / 3.5-flash / 3.5-flash-lite / 3-flash-preview /
3.1-flash-lite(-preview) / flash-latest / flash-lite-latest /
gemma-4-31b-it / gemma-4-26b-a4b-it

**❌ 免费档额度 0（需 AI Studio 挂 billing，暂不列入）**
gemini-4-argon、gemini-3.1-pro-preview（+customtools）、gemini-pro-latest、
gemini-omni-flash(-preview)、gemini-3.1-flash-image、nano-banana-pro-preview

**⚠️ 上游瞬时抖动（重试即好，暂不列入）**
gemini-3.8-flash / 3.7-flash（503/disconnect，Google 侧容量）

**❌ 404（该 key 不可用）** gemini-2.5-flash / 2.5-pro / 2.5-flash-lite

## 运维

- 首尔隧道挂了 → gs 渠道全断（计划任务 QidiSeoulProxy 保活）；
  恢复后自动愈合（渠道冷却结束即重试）
- 想解锁旗舰：AI Studio 控制台挂 billing（按量付费，非免费），
  再把模型加进 `providers.gs.models` + `model_aliases`
- 查 key 实时限额：ai.dev/rate-limit

## 复盘备注

- httpx 的 SOCKS 支持要求 socksio；中转 Python 补装后**必须重启进程**
  （httpx 在模块加载时探测，运行中进程不认新装的包——本次实测踩坑）
- 中转曾多实例并存（15772/22524/22316 抢 8765）；清理后以 WMI 脱离启动
  单实例为准。建议后续给中转建 onlogon 计划任务固化
