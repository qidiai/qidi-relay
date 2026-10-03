# -*- coding: utf-8 -*-
"""回退：把 zcode 的 local-codely-proxy 指回 v2 生产 (8765)。
用法: py -3 切回v2.py   （v3 出问题时：先 taskkill 杀 v3，再跑这个）"""
import io
import json
import shutil
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

P = r"C:\Users\?\.zcode\v2\config.json"

cfg = json.load(open(P, encoding="utf-8-sig"))
p = cfg["provider"]["local-codely-proxy"]
old = p["options"]["baseURL"]
if ":8765" in old:
    print(f"无需回退：zcode 已指向 v2 ({old})")
    sys.exit(0)
shutil.copy2(P, P + f".bak-{time.strftime('%Y%m%d-%H%M%S')}")
p["options"]["baseURL"] = "http://192.168.1.<IP>:8765/v1"
with open(P, "w", encoding="utf-8", newline="\n") as f:
    json.dump(cfg, f, ensure_ascii=False, indent=2)
print(f"已回退: {old} → {p['options']['baseURL']}")
print("v2 生产一直在 8765 运行，立即可用。")
