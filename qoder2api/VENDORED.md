# qoder2api（Vendored 依赖）

本目录是 [shuishuipingan/qoder2api-hub](https://github.com/shuishuipingan/qoder2api-hub)
的 **vendored 副本**（MIT 许可，原文 LICENSE 见本目录），让本仓库成为
「克隆即用」的 Qoder 反代完整方案。

- 上游版本：v1.5.4（2026-10-20 vendored，来自 origin/main）
- 同步方式：手动 vendoring；如需更新，重新拷贝上游源码并保留本目录
  `run-qoder-proxy.vbs` / `qoder2api-keepalive.ps1` 两个本仓新增文件
- 安全审计记录（接入前已审）：端点仅 Qoder 官方 + 本地；全库仅 1 个 RSA
  服务端公钥（COSY 协议设计使然）；凭证只读本机 auth.v1.dat 且两步确认，
  不出本机；零第三方依赖。详见 ../docs/qoder.md

## 本仓新增（上游没有）

| 文件 | 作用 |
|------|------|
| `run-qoder-proxy.vbs` | Windows 无窗 detached 启动（计划任务友好） |
| `qoder2api-keepalive.ps1` | 看门狗：8790 掉了自动重启（签到窗口前保命） |

## 快速开始（用你自己的 Qoder 账号）

1. 安装 Python 3.9+（上游零第三方依赖，纯标准库）
2. 在**自己的**电脑登录 Qoder 桌面 App 或 CLI（产生 auth.v1.dat）
3. 启动桥：`start-qoder-proxy.bat`（或 `run-qoder-proxy.vbs` 无窗）
4. 浏览器打开 `http://127.0.0.1:8790` → 面板 `[+ 添加账号]`
   （桌面 App 导入 / OAuth / PAT 三条路，任选）
5. 得到 OpenAI 兼容端点 `http://127.0.0.1:8790/v1`，
   模型清单见 `GET /v1/models`（GLM-5.3 系、Kimi-K3、Qwen3.8 系、
   DeepSeek-V4 系等 14 个）

接入本中转：见 ../docs/qoder.md（providers.qoder 配置节）。

## 注意

- `accounts/` 目录是凭证存储（uid.json / machine_identity.json /
  settings.json），**已被 .gitignore 排除**，切勿提交
- 合规边界：桥驱动官方协议 + 你自己的账号即正当使用；若要规模化，
  先读上游 README 的 ToS 章节（部分提供商标注了代理路由风险）
