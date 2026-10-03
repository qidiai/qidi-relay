# Codely/Tuanjie 积分签到

`codely_points_checkin.py` 会读取上一级目录的 `accounts.json`，逐个使用账号的 `access_token` 请求 Codely/Tuanjie 每日积分签到接口。

## 使用

先只检查账号，不发送请求：

```powershell
python G:\程序开发\本地局域网中转-v3\codely_points\codely_points_checkin.py --dry-run
```

确认后手动签到：

```powershell
python G:\程序开发\本地局域网中转-v3\codely_points\codely_points_checkin.py
```

日志追加到本目录 `checkin.log`。Token 不会写入日志；账号名会脱敏显示；`accounts.json` 中 `enabled=false` 的账号会跳过。

## 每天自动执行

用无窗口 VBS 包装器注册 Windows 计划任务（示例为每天 09:30）：

```powershell
schtasks /create /f /tn "codely-points-checkin" /tr "wscript.exe G:\程序开发\本地局域网中转-v3\codely_points\run_checkin.vbs" /sc daily /st 09:30
```

停止/删除任务：

```powershell
schtasks /delete /f /tn "codely-points-checkin"
```

## 重要说明

- 这是一个非公开后台接口，路径当前按站点页面约定配置为 `/api/points/check-in`；如果官网改版，可能需要根据浏览器 Network 中的真实请求更新 `CHECKIN_PATH`。
- 签到成功通常返回积分增加；重复执行应由服务端返回“已签到”，脚本会记为 `ALREADY`。
- 仅用于你本人拥有并已授权的账号，不要把 Token、脚本或日志上传到公开仓库。
- 本地中转只需保证 `accounts.json` 中账号的 `access_token` 有效；建议先用 `--dry-run` 检查账号数量。
