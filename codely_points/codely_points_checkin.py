#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Codely/Tuanjie 多账号每日积分签到（仅标准库）。"""
import argparse, json, re, sys, time, urllib.error, urllib.request
from datetime import datetime
from pathlib import Path
BASE = Path(__file__).resolve().parent.parent
DEFAULT_ACCOUNTS = BASE / "accounts.json"
LOG = Path(__file__).resolve().parent / "checkin.log"
UPSTREAM = "https://codely.tuanjie.cn"
# 页面 /dashboard/points-tasks 的签到接口；服务端改版时只需改这里。
CHECKIN_PATH = "/api/points/check-in"
# 日志只记录排障所需摘要；如果上游错误页/错误体回显凭证，先统一脱敏。
SECRET = re.compile(r"(?i)bearer\s+\S+|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*")
try: sys.stdout.reconfigure(encoding="utf-8")
except Exception: pass

def log(msg):
    line = datetime.now().strftime("%Y-%m-%d %H:%M:%S") + "  " + msg
    with LOG.open("a", encoding="utf-8") as f: f.write(line + "\n")
    print(line)

def load_accounts(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return data.get("accounts", []) if isinstance(data, dict) else data

def name(account, index):
    value = str(account.get("username") or account.get("email") or account.get("user_id") or index + 1)
    return value if len(value) <= 8 else value[:4] + "***" + value[-4:]

def post(account, timeout):
    token = str(account.get("access_token") or "").strip()
    if not token: raise RuntimeError("缺少 access_token")
    req = urllib.request.Request(UPSTREAM + CHECKIN_PATH, data=b"{}", method="POST", headers={
        "Authorization": "Bearer " + token, "Content-Type": "application/json",
        "Accept": "application/json", "User-Agent": "Tuanjie-AI-Desktop"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")

def classify(status, body):
    text = (body or "").lower()
    if status in (401, 403): return "AUTH_ERROR"
    if status in (404, 405): return "NOT_IMPLEMENTED"
    if status == 429: return "RATE_LIMIT"
    if status >= 500: return "SERVER_ERROR"
    if any(x in text for x in ("已签到", "already", "重复签到", "duplicate")): return "ALREADY"
    return "OK" if 200 <= status < 300 else "FAILED"

def detail_text(body):
    return SECRET.sub("<redacted>", " ".join((body or "").split()))[:240]

def main():
    ap = argparse.ArgumentParser(description="Codely/Tuanjie 多账号积分签到")
    ap.add_argument("--accounts", default=str(DEFAULT_ACCOUNTS))
    ap.add_argument("--timeout", type=int, default=30)
    ap.add_argument("--delay", type=float, default=1.0)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    try: items = load_accounts(args.accounts)
    except Exception as e:
        log("FATAL accounts_load_error=%s" % e); return 2
    log("START total=%d dry_run=%s endpoint=%s" % (len(items), args.dry_run, CHECKIN_PATH))
    if args.dry_run:
        for i, account in enumerate(items, 1):
            print("%02d %s token=%s enabled=%s" % (
                i, name(account, i - 1), "ok" if account.get("access_token") else "missing",
                "yes" if account.get("enabled", True) else "no"))
        return 0
    stats = {}
    for i, account in enumerate(items, 1):
        label = name(account, i - 1)
        if not account.get("enabled", True):
            state = "SKIPPED"
            log("RESULT %02d/%02d %s state=SKIPPED detail=account_disabled" % (i, len(items), label))
            stats[state] = stats.get(state, 0) + 1
            continue
        try:
            status, body = post(account, args.timeout)
            state = classify(status, body)
            detail = detail_text(body)
            log("RESULT %02d/%02d %s state=%s http=%s detail=%s" % (i, len(items), label, state, status, detail))
        except Exception as e:
            state = "FAILED"
            log("RESULT %02d/%02d %s state=FAILED error=%s" % (i, len(items), label, e))
        stats[state] = stats.get(state, 0) + 1
        if i < len(items) and args.delay > 0: time.sleep(args.delay)
    log("DONE " + " ".join("%s=%d" % x for x in sorted(stats.items())))
    bad = sum(stats.get(k, 0) for k in ("FAILED", "AUTH_ERROR", "RATE_LIMIT", "SERVER_ERROR", "NOT_IMPLEMENTED"))
    return 0 if not bad else 1

if __name__ == "__main__": raise SystemExit(main())
