#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""token_sentinel.py — 每天由计划任务调用一次，检查 VibeX JWT 的过期倒计时。

行为:
  - 读取本脚本同目录下 vibex2api.config.json 的 token 字段;
  - 解码 JWT payload 的 exp, 计算剩余天数, 追加一行到 sentinel.log;
  - 剩余天数 <= 7 天: 写 TOKEN_ALERT.md (红色警报), 退出码 1;
  - 剩余天数  > 7 天: 若 TOKEN_ALERT.md 存在则删除, 退出码 0;
  - token 为空 / 解码失败: 记一行 token_missing_or_invalid, 退出码 2。

仅使用 Python 3.12 标准库 (base64 / json / sys / time / datetime / pathlib)。
"""

import base64
import json
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "vibex2api.config.json"
LOG_PATH = HERE / "sentinel.log"
ALERT_PATH = HERE / "TOKEN_ALERT.md"
ALERT_THRESHOLD_DAYS = 7

REFRESH_STEPS = (
    "1. 让 qidi 弹出可见浏览器重新登录 vibex.runninghub.cn；"
    "2. 把新 Rh-Accesstoken 更新进 vibex2api.config.json 的 token 字段；"
    "3. 重启 vibex2api（杀进程后 keepalive 任务 2 分钟内自动拉起，"
    "或手动 python vibex2api.py）"
)


def local_now_str():
    """当前本地时区的可读时间戳。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def tz_offset_str(dt):
    """返回形如 +08:00 的本地时区偏移。

    datetime.fromtimestamp() 得到的是 naive 本地时间, 其 utcoffset() 为 None,
    因此先用 astimezone() 附上本地时区再取偏移。
    """
    if dt.tzinfo is None:
        dt = dt.astimezone()
    off = dt.utcoffset()
    if off is None:
        return ""
    total = int(off.total_seconds())
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    return f"{sign}{total // 3600:02d}:{(total % 3600) // 60:02d}"


def append_log(line):
    """追加一行到 sentinel.log (UTF-8, 不轮转)。"""
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def read_token():
    """从 config.json 读取 token 字段; 任何异常都返回空串。"""
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return ""
    if isinstance(data, dict) and isinstance(data.get("token"), str):
        return data["token"].strip()
    return ""


def jwt_decode(token):
    """只解码 JWT 的 payload 段; 失败返回 None。"""
    if not token or not isinstance(token, str):
        return None
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return None
        seg = parts[1]
        seg += "=" * (-len(seg) % 4)              # 补 = padding
        payload = json.loads(base64.urlsafe_b64decode(seg).decode("utf-8"))
        return payload if isinstance(payload, dict) else None
    except Exception:
        return None


def write_alert(exp_dt, days_left):
    """写 TOKEN_ALERT.md (UTF-8 中文红色警报)。"""
    exp_str = exp_dt.strftime("%Y-%m-%d %H:%M:%S")
    content = (
        "# 🔴 红色警报：vibex2api TOKEN 即将过期\n\n"
        f"- **过期时间**：{exp_str}（本地时区 {tz_offset_str(exp_dt)}）\n"
        f"- **剩余天数**：{days_left:.1f} 天\n\n"
        "## 刷新步骤\n\n"
        f"{REFRESH_STEPS}\n"
    )
    ALERT_PATH.write_text(content, encoding="utf-8")


def main():
    token = read_token()
    payload = jwt_decode(token)
    exp = payload.get("exp") if isinstance(payload, dict) else None
    if not isinstance(exp, (int, float)):
        append_log(f"{local_now_str()}  token_missing_or_invalid")
        return 2

    now = time.time()
    days_left = (float(exp) - now) / 86400.0
    exp_dt = datetime.fromtimestamp(float(exp))   # 本地时区可读格式
    append_log(
        f"{local_now_str()}  days_left={days_left:.1f}  "
        f"exp={exp_dt.strftime('%Y-%m-%d %H:%M:%S')}({tz_offset_str(exp_dt)})"
    )

    if days_left <= ALERT_THRESHOLD_DAYS:
        write_alert(exp_dt, days_left)
        return 1
    if ALERT_PATH.exists():
        ALERT_PATH.unlink()
    return 0


if __name__ == "__main__":
    sys.exit(main())
