# -*- coding: utf-8 -*-
"""训练语料云端→本地 拉取器（每12小时由计划任务调起）

流程：增量导出(since_id) → 追加本地月度JSONL → fsync → ack删除云端已同步行。
安全原则：本地落盘成功才 ack，任何失败不删云端（下轮重试，宁可重复不可丢失）。
"""
import json
import os
import sys
from datetime import datetime

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOCAL_DIR = os.path.join(BASE_DIR, "corpus_local")
STATE = os.path.join(BASE_DIR, "corpus_state.json")
LOG = os.path.join(BASE_DIR, "corpus_pull.log")
URL = "https://cloud-relay.example/v1"
KEY = "REDACTED-PULL-KEY"


def log(msg):
    line = "%s %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def main():
    os.makedirs(LOCAL_DIR, exist_ok=True)
    last = 0
    if os.path.exists(STATE):
        try:
            last = int(json.load(open(STATE, encoding="utf-8")).get("last_max_id", 0))
        except Exception:
            last = 0

    r = requests.get(URL + "/training/export",
                    params={"since_id": last, "limit": 5000},
                    headers={"Authorization": "Bearer " + KEY},
                    timeout=120)
    if r.status_code != 200:
        log("导出失败 HTTP %d: %s" % (r.status_code, r.text[:200]))
        return 1
    d = r.json()
    lines, max_id = d.get("lines") or [], int(d.get("max_id") or 0)
    if not lines:
        log("无新数据（last=%d）" % last)
        return 0

    # 追加到本地月度文件
    month = datetime.now().strftime("%Y%m")
    path = os.path.join(LOCAL_DIR, "corpus_%s.jsonl" % month)
    with open(path, "a", encoding="utf-8") as f:
        for ln in lines:
            f.write(ln + "\n")
        f.flush()
        os.fsync(f.fileno())
    got_max = 0
    for ln in lines:
        try:
            got_max = max(got_max, int(json.loads(ln).get("id", 0)))
        except Exception:
            pass
    if got_max <= 0:
        log("本地已写 %d 行但未解析出 max id，跳过 ack" % len(lines))
        return 1

    # 落盘成功 → ack 删云端
    r2 = requests.get(URL + "/training/ack",
                      params={"max_id": got_max},
                      headers={"Authorization": "Bearer " + KEY},
                      timeout=60)
    ok = r2.status_code == 200
    log("拉取 %d 行 -> %s（%d bytes）| ack HTTP %d %s" % (
        len(lines), path, os.path.getsize(path), r2.status_code, r2.text[:100]))
    if ok:
        json.dump({"last_max_id": got_max}, open(STATE, "w", encoding="utf-8"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
