# Qoder 渠道接入（2026-10-20）

## 架构

```
客户端 → 中转:8765 → qoder 渠道（qoder/ 前缀）→ 本地桥 8790
         （qoder2api-hub v1.5.4，COSY 签名真协议）→ gateway.qoder.com.cn
```

- **桥**：`G:\程序开发\qoder2api-audit`（MIT，零依赖，Python 3.9+）
  - 启动：计划任务 `qoder2api`（开机自启，`run-qoder-proxy.vbs` wscript  detached）
  - 端口 8790（`/v1` OpenAI 兼容 + `/` 面板，管理端点需面板密码（首次登录后务必修改，勿用默认值））
  - 账号：桌面 App `auth.v1.dat` 导入（两步确认：扫描→导入），CN 区主账号
    （昵称/uid 已脱敏；17 天有效，refresh token 自动续期）
- **中转侧**：`providers.qoder`（base_url `http://127.0.0.1:8790/v1`）
  - 模型统一 `qoder/` 前缀（go/ zynk/ 先例，防与主池/竞对渠道大小写撞名）
  - `model_aliases` 还原桥侧显示名（大小写敏感，如 `qoder/GLM-5.3`→`GLM-5.3`）

## 模型目录（14 个，以桥 /v1/models 实时为准）

Auto、Qwen3.8-Max/Flash、Qwen3.7-Max/Plus/Flash、DeepSeek-V4-Pro、
DeepSeek-Flash、**GLM-5.3 / GLM-5.3-Flash / GLM-5.2**、Kimi-K3、
Kimi-K2.8-Preview、MiniMax-M3.1-Flash

## 为什么接它

Codely 主池的 GLM-5.3 权限集中在 2 个账号（39640/58863），配额周期性
耗尽时 GLM 请求全 401（1019/1020 审计实证）。Qoder CN 区自带
GLM-5.3/Flash/5.2 + Kimi-K3 + DeepSeek-V4 系，作为**第二供应线**削峰。
注：CN 区无 glm-5.3-flashx（国际区有，待国际区账号登录后可扩）。

## 安全审计结论（接入前已审）

端点仅 Qoder 官方 + 本地；全库仅 1 个 RSA 服务端公钥（COSY 协议设计）；
凭证只读本地 auth.v1.dat（DPAPI）且两步确认、不出本机；零第三方依赖；
面板 233KB 全内联无 CDN。MIT 许可。

## 运维

- 桥挂了：`schtasks /run /tn qoder2api`（或重启计划任务）
- 加账号：面板 `[+ 添加账号]`（OAuth/PAT/桌面导入三条路）
- 每日签到：桥内置 scheduler **已启用并实证**——每天 09:00/21:00 两次
  自动签到（每次 +100 积分），22:00 Token 集中保活；启动巡检带
  mark-before-act 防重放（崩溃重启不会重复领取）。2026-10-20 启动
  巡检已自动领到当日 +100。手动触发：面板或 `POST /accounts/checkin`
  （需 X-Panel-Token）。
- 保活看门狗：计划任务 `qoder2api-keepalive`（08:50）+
  `qoder2api-keepalive-2`（20:50）——签到窗口前检查 8790，桥挂了
  自动拉起，不错过签到窗口
- 撤渠道：删 `providers.qoder` 配置节即热失效
