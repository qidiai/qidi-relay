# apigoto 渠道接入指南

[apigoto](https://www.apigoto.com/) 是一个 OpenAI 兼容的第三方 API 聚合网关：一个 Key 即可调用多家厂商模型（Claude / GPT / Gemini / Grok / DeepSeek / Kimi / Qwen / MiniMax / GLM / Doubao 等旗舰与国产系列），按 token 池计费，适合把多个模型源合并成一个统一入口。

> 🔑 **注册 / 获取 API Key**：https://www.apigoto.com/wechat_oX2xV2dt6vu8jpIdGvpvPHzk_qY8
> 登录后在控制台创建 API Key（`sk-` 开头）。

## 一、网页面板接入（推荐）

1. 打开管理面板 → **Provider / 渠道管理** → **添加**
2. 填写：
   - **名称**：`apigoto`
   - **Base URL**：`https://www.apigoto.com/v1`
   - **API Key**：粘贴你的 `sk-...` Key
3. **模型列表**：填入要接入的模型 ID——
   - 可用的模型 ID 以网关实时目录为准：`GET https://www.apigoto.com/v1/models`
   - 面板自带"**探测模型**"按钮可一键从上游拉取全量模型 ID，勾选即可
4. 保存即生效（`config.json` 热加载，无需重启）

## 二、直接写 config.json

在 `providers` 段添加（Key 请替换为你自己的）：

```json
"apigoto": {
  "base_url": "https://www.apigoto.com/v1",
  "api_keys": [
    "sk-你的apigotoKey"
  ],
  "models": [
    "claude-sonnet-5-5",
    "claude-haiku-4-5",
    "gpt-5.5",
    "gpt-6-sol",
    "gemini-3.8-flash",
    "grok-4.7",
    "deepseek-v4-pro",
    "deepseek-v4-flash",
    "kimi-k2.7-code-highspeed",
    "qwen3.8-max",
    "minimax-m2.7-highspeed",
    "doubao-seed-2-1-pro-260915"
  ],
  "enabled": true
}
```

> 上面列出的模型 ID 仅供参考（均为实测可用的主流型号），网关上游会动态调整——接入前建议先用 `/v1/models` 或面板探针确认当前可用清单。

## 三、进阶用法

- **多 Key 轮换**：`api_keys` 数组可放多个 Key，配额耗尽（402/额度类错误）自动切换下一个
- **Key 停用**：额度暂时用尽的 Key 可在面板上**停用**（保留在池中、不参与选号），额度重置后再一键启用——避免删除后重新登录网关控制台
- **渠道开关**：`enabled: false` 可整渠道下线（面板一键切换），上游故障时快速止损
- **客户端调用**：标准 OpenAI Chat Completions 协议，把客户端的 `base_url` 指向本中转即可，无需关心上游差异

## 四、注意事项

- ⚠️ **Key 即额度**：不要把真实 Key 提交进任何公开仓库（本仓库所有示例配置均已脱敏）
- apigoto 为第三方聚合网关，其服务可用性、模型真实性与计费口径以官方为准
- 建议公网部署时配合 `api_keys`（客户端鉴权）与 `allowed_ips`（管理面板白名单）使用
