#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""wb_snapshot_backup.py — 冷备号快照抓取 (纯标准库, 无第三方依赖)。

用途:
  用户有两个 WorkBuddy (腾讯 CodeBuddy 国内版) 账号: 主号日常用, 2 号做冷备
  (只签到攒额度, 不进推理池)。冷备号凭据需在桌面端登录 2 号时抓一次快照保存,
  之后 wb_checkin.py 用快照 token 独立签到, 不需要桌面端保持 2 号登录。

切号三步 (桌面端切到 2 号后运行本脚本):
  1. 登主号:   桌面端正常登录主号 (日常使用);
  2. 登 2 号:  在同一桌面端切换到 2 号登录;
  3. 跑脚本:   python wb_snapshot_backup.py 抓取 2 号快照 (保存后即可);
  4. 切回主号: 桌面端重新切回主号登录。
  (顺序: 先跑脚本抓快照, 再切回主号; 快照保存后桌面端不再需要保持 2 号)

流程:
  1. 读取桌面端 auth 文件 (workbuddy-desktop.info) 取 token/uid/到期时间;
  2. POST /v2/billing/meter/checkin-status 验证凭据可用 (code==0 即可用;
     若今天已签过, 该接口仍返回 code=0, 不是 10001);
  3. 保存快照到 accounts/backup-<uid前8位>.json (同 uid 覆盖);
  4. 打印保存路径 + 昵称 + uid 前 8 位 + accessToken 到期日。

退出码: 0=成功 / 1=失败
"""

import json
import os
import ssl
import sys
import urllib.error
import urllib.request
from datetime import datetime

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
ACCOUNTS_DIR = os.path.join(HERE, "accounts")   # 即 G:\程序开发\本地局域网中转-v3\wb\accounts

AUTH_FILE = r"C:\Users\?\AppData\Local\CodeBuddyExtension\Data\Public\auth\workbuddy-desktop.info"
UPSTREAM = "https://copilot.tencent.com"
TIMEOUT = 20


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def ms_to_date(ms):
    """毫秒时间戳 -> YYYY-MM-DD。"""
    return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d")


def load_auth():
    """读取桌面端 auth 文件, 取冷备号所需字段。"""
    with open(AUTH_FILE, encoding="utf-8") as f:
        d = json.load(f)
    auth = d["auth"]
    account = d["account"]
    return {
        "accessToken": auth["accessToken"],
        "refreshToken": auth["refreshToken"],
        "expiresAt": auth["expiresAt"],
        "refreshExpiresAt": auth["refreshExpiresAt"],
        "domain": auth["domain"],
        "uid": account["uid"],
        "nickname": account["nickname"],
    }


def verify_credentials(token, uid):
    """POST checkin-status 验证凭据。可用返回 (True, "")，否则 (False, 详情)。"""
    url = UPSTREAM + "/v2/billing/meter/checkin-status"
    data = json.dumps({}).encode("utf-8")
    headers = {
        "Authorization": "Bearer " + token,
        "X-User-Id": uid,
        "Content-Type": "application/json",
    }
    req = urllib.request.Request(url, data=data, method="POST", headers=headers)
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as r:
        body = r.read().decode("utf-8", "replace")
    j = json.loads(body)
    if isinstance(j, dict) and j.get("code") == 0:
        return True, ""
    return False, "checkin-status code != 0: " + body[:200]


def main():
    # 1. 读取凭据
    try:
        info = load_auth()
    except Exception as e:
        print("auth_read_failed: %s" % e)
        return 1

    token = info["accessToken"]
    uid = info["uid"]

    # 2. 验证凭据可用
    try:
        ok, detail = verify_credentials(token, uid)
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            body = ""
        print("verify_failed: HTTP %s %s" % (e.code, body[:200]))
        return 1
    except Exception as e:
        print("verify_failed: %s" % e)
        return 1
    if not ok:
        print("verify_failed: %s" % detail)
        return 1

    # 3. 保存快照
    snapshot = {
        "uid": uid,
        "nickname": info["nickname"],
        "captured_at": now_str(),
        "accessToken": token,
        "refreshToken": info["refreshToken"],
        "expiresAt": info["expiresAt"],
        "refreshExpiresAt": info["refreshExpiresAt"],
        "domain": info["domain"],
    }
    try:
        os.makedirs(ACCOUNTS_DIR, exist_ok=True)
        path = os.path.join(ACCOUNTS_DIR, "backup-%s.json" % uid[:8])
        with open(path, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=2)
            f.write("\n")
    except Exception as e:
        print("snapshot_save_failed: %s" % e)
        return 1

    # 4. 输出
    print("snapshot_saved: %s" % path)
    print("nickname: %s" % info["nickname"])
    print("uid8: %s" % uid[:8])
    print("accessToken_expires: %s" % ms_to_date(info["expiresAt"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
