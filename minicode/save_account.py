# -*- coding: utf-8 -*-
"""把当前 mcode 登录态账号快照保存到中转签到名册（幂等：同账号不重复存）"""
import json, shutil, sys, time
from pathlib import Path

BASE = Path(r"G:\程序开发\本地局域网中转-v3")
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "minicode"))

LIVE = Path.home() / ".minimax" / "auth" / "prod" / "cn" / "mcode-public" / "auth.json"
ACC_DIR = BASE / "minicode" / "accounts"
ACC_FILE = BASE / "minicode" / "accounts.json"

# 0) 当前登录态
st = LIVE.stat()
print("live auth.json mtime:", time.strftime("%m-%d %H:%M:%S", time.localtime(st.st_mtime)))

import minicode_checkin as MC
cred = MC.minicode.parse_auth_json(str(LIVE))
print("token 前12:", cred.access_token[:12],
      " 过期:", "是" if cred.expired else "否",
      " 至:", time.strftime("%m-%d %H:%M", time.localtime(cred.expires_at_ms / 1000))
      if cred.expires_at_ms else "未知")

# 1) 已存名册读取 + 查重（按身份）
doc = {"accounts": []}
if ACC_FILE.exists():
    try:
        d = json.load(ACC_FILE.open(encoding="utf-8"))
        doc["accounts"] = d.get("accounts") if isinstance(d, dict) else d
    except Exception:
        pass
existing = doc["accounts"]

# 2) 识别当前账号身份
uid = None
try:
    uid = MC._fetch_real_user_id(cred.access_token, 30)
    print("realUserID:", uid)
except Exception as e:
    print("realUserID 获取失败(仍会保存，label 用时间戳):", str(e)[:100])

if uid:
    for a in existing:
        if str(a.get("uid") or "") == str(uid):
            print("该账号已有快照（label=%s）——用新登录态覆盖"
                  "（旧快照 refresh 可能已被吊销/轮换）" % a.get("label"))
            label = a["label"]
            break

label = ("小号-" + str(uid)) if uid else ("小号-" + time.strftime("%m%d%H%M%S"))

# 3) 快照 + 登记
ACC_DIR.mkdir(exist_ok=True)
dest = ACC_DIR / (label + ".json")
shutil.copyfile(LIVE, dest)
existing = [a for a in existing if a.get("label") != label]
existing.append({"label": label, "uid": uid or "",
                 "auth_path": str(dest).replace("\\", "/")})
ACC_FILE.write_text(json.dumps({"accounts": existing},
                               ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")

# 4) 从保存的副本回验（token 可读 + 身份一致）
cred2 = MC.minicode.parse_auth_json(str(dest))
ch = MC.minicode.MiniCodeChannel("minicode",
                                 {"auth_path": str(dest), "region": "cn"},
                                 label=label)
tok2 = ch.get_token()
uid2 = MC._fetch_real_user_id(tok2, 30)
print("saved ->", dest)
print("registered:", label, " uid:", uid or uid2,
      " 回验一致:", (uid2 == uid) if uid else "n/a(uid未取到)")
