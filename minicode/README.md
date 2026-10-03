# MiniCode（mcode / MiniMax Code CLI）账号每日签到

`minicode_checkin.py` 自动为 config.json 里所有 `type: minicode` 通道的账号执行
MiniMax 每日福利签到（token plan 每日送积分，7 天一轮，断签从第 1 天重来）。

- 登录 token 复用反代通道模块 `proxy_core/minicode.py`：mtime 感知 + 过期自动
  OAuth 刷新（refresh_token 轮换立即写回、generation 不动），官方 mcode 客户端
  在跑则零维护；不在跑也能自持。
- realUserID 通过开放平台 `www.minimaxi.com/v1/api/user/info` 实时解析，无本地缓存依赖。
- 签到接口走公共网关 `agent.minimaxi.com/minimax-cloud/api/v1/signin/*`
  （status 查面板 → claim 领取），客户端归属头（yy/x-timestamp/x-signature）与
  官方客户端字面一致（源码 wire-protocol constants）。
- 日志追加在本目录 `checkin.log`，只记录结果摘要，token 一律脱敏。
- 账号失效（401 / 业务码 1000048）会提示重新 `mcode login`，不影响其他账号。

## 使用

先 dry-run 看面板（不实际领取）：

```powershell
python G:\程序开发\本地局域网中转-v3\minicode\minicode_checkin.py --dry-run
```

确认后手动签到：

```powershell
python G:\程序开发\本地局域网中转-v3\minicode\minicode_checkin.py
```

多账号间隔默认 2 秒（`--delay`），超时默认 30 秒（`--timeout`）。
退出码 0 = 全部账号达成（已领/新领/无可领日），非 0 = 至少一个账号失败。

## 每天自动执行

无窗口 VBS 包装，注册 Windows 计划任务，设为每天 09:40（避开 codely 09:30 高峰）：

```powershell
schtasks /create /f /tn "minicode-checkin" /tr "wscript.exe G:\程序开发\本地局域网中转-v3\minicode\run_checkin.vbs" /sc daily /st 09:40
```

停止/删除任务：

```powershell
schtasks /delete /f /tn "minicode-checkin"
```

## 重要说明

- 签到送的是 **MiniMax 开放平台积分/token plan 福利**，直接进账号余额，与反代
  通道共享同一账号额度体系；脚本只做"领取"这一个动作，不改变路由行为。
- 上游改版时优先怀疑三处：路径（STATUS_PATH/CLAIM_PATH）、查询串风味、
  yy/x-signature 字面常量（`ooui` / `I*7Cf%WZ#S&%1RlZJ&C2`），全部来自 mcode
  开源仓库 `tui/src/checkin/http-gateway.ts` + `tui/src/runtime/public-gateway.ts`，
  对照最新源码即可修正。
- 中转程序本身不依赖本脚本：即使签到失败，反代通道照常工作。
