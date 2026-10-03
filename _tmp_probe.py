# -*- coding: utf-8 -*-
"""0903 重新探测全部账号的最新模型权限。"""
import json
import sys
import uuid

import httpx

sys.path.insert(0, r"G:\程序开发\本地局域网中转-v3")
from proxy_core.keystore import build_litellm_headers  # noqa: E402
from proxy_core.settings import CLI_API_KEY_FETCH_URL  # noqa: E402

out = open(r"G:\程序开发\本地局域网中转-v3\_tmp_probe.txt", "w", encoding="utf-8")
d = json.load(open(r"G:\程序开发\本地局域网中转-v3\accounts.json", encoding="utf-8-sig"))
for acc in d["accounts"]:
    if not acc.get("enabled", True):
        continue
    uid = acc["user_id"]
    out.write(f"\n===== 账号 {uid} (group={acc.get('group') or '(Lite订阅)'}) =====\n")
    try:
        r = httpx.get(CLI_API_KEY_FETCH_URL, params={"teamId": acc["org_id"]},
                      headers={"Authorization": f"Bearer {acc['access_token']}"}, timeout=15)
        key = r.json()["cli_api_key"]
        h = build_litellm_headers(key, "/v1/models")
        r2 = httpx.get("https://codely-litellm.tuanjie.cn/v1/models", headers=h, timeout=20)
        ids = [m.get("id", "?") for m in r2.json().get("data", [])]
        out.write(f"  可用模型({len(ids)}): {', '.join(ids)}\n")
        out.write(f"  GLM-5.3: {'✅' if 'GLM-5.3' in ids else '❌'} | "
                  f"GLM-5.3-FLASH: {'✅' if 'GLM-5.3-FLASH' in ids else '❌'} | "
                  f"KIMI-K3: {'✅' if 'KIMI-K3' in ids else '❌'}\n")
    except Exception as e:
        out.write(f"  异常: {e}\n")
out.close()
print("done")
