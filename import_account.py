# -*- coding: utf-8 -*-
"""从 Codely CLI 凭证文件导入当前登录账号到 accounts.json。

用法：
  1. 先在 Codely CLI 里登录目标账号（codely 登录后会写 oauth_creds.json）
  2. 运行：py -3 import_account.py
  3. 脚本自动读取 user_id / access_token / org_id 并导入

支持重复运行：已存在的账号会更新 token，不会重复添加。
"""
import base64
import json
import sys
import time
from pathlib import Path

OAUTH_FILE = Path.home() / ".codely-cli" / "oauth_creds.json"
ACCOUNTS_FILE = Path(__file__).parent / "accounts.json"


def parse_jwt(token: str) -> dict:
    """解析 JWT payload（不验签），提取 user_id / org_id / username。"""
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    payload_b64 = parts[1]
    payload_bytes = base64.urlsafe_b64decode(
        payload_b64 + "=" * (4 - len(payload_b64) % 4)
        if len(payload_b64) % 4 else payload_b64
    )
    return json.loads(payload_bytes)


def main():
    if not OAUTH_FILE.exists():
        print("错误：未找到 " + str(OAUTH_FILE))
        print("请先运行 codely 登录目标账号")
        sys.exit(1)

    creds = json.load(open(OAUTH_FILE, encoding="utf-8"))
    access_token = creds.get("access_token", "")
    if not access_token:
        print("错误：oauth_creds.json 中没有 access_token")
        sys.exit(1)

    payload = parse_jwt(access_token)
    user_id = str(payload.get("sub", ""))
    username = payload.get("username", "")
    org_id = payload.get("org_id", "")

    if not user_id:
        print("错误：无法从 token 中解析 user_id")
        sys.exit(1)

    print("检测到账号：")
    print("  user_id:  " + user_id)
    print("  username: " + username)
    print("  org_id:   " + org_id)
    print("  email:    " + payload.get("email", ""))
    print()

    # 读取现有 accounts.json
    if ACCOUNTS_FILE.exists():
        data = json.load(open(ACCOUNTS_FILE, encoding="utf-8"))
    else:
        data = {"accounts": []}

    accounts = data.get("accounts", [])

    # 查找是否已存在
    found = False
    for acc in accounts:
        if acc.get("user_id") == user_id:
            acc["access_token"] = access_token
            acc["username"] = username
            acc["org_id"] = org_id
            acc["enabled"] = True
            found = True
            print("已更新现有账号的 token（user_id=" + user_id + "）")
            break

    if not found:
        accounts.append({
            "user_id": user_id,
            "username": username,
            "org_id": org_id,
            "access_token": access_token,
            "last_used": "",
            "use_count": 0,
            "enabled": True,
            "has_glm53": False,
            "budget_exceeded": False,
        })
        print("已添加新账号（user_id=" + user_id + "）")

    data["accounts"] = accounts
    with open(ACCOUNTS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print()
    print("当前账号池（共 " + str(len(accounts)) + " 个）：")
    for acc in accounts:
        status = "启用" if acc.get("enabled") else "禁用"
        print("  " + acc["user_id"] + " (" + acc.get("username", "") + ") [" + status + "]")

    print()
    print("完成！重启代理后生效。")


if __name__ == "__main__":
    main()
