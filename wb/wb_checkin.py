#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""wb_checkin.py — WorkBuddy 每日签到脚本 (纯标准库, 无第三方依赖)。

流程:
  1. 读取桌面端 auth 文件拿 token/uid;
  2. POST /v2/billing/meter/checkin-status  确认端点与鉴权, 失败退出 2;
  3. POST /v2/billing/meter/daily-checkin   签到;
       code==0                → CHECKIN_OK ...          (退出 0)
       code==10001 / 已签到    → ALREADY_CHECKED_IN       (退出 0)
       其它                   → CHECKIN_FAIL ...         (退出 1)
  4. POST /v2/billing/meter/get-user-resource 取 TotalDosage, 记 credits_total=<值>;

多账号: 账号列表 = 主号 + accounts/backup-*.json 冷备号快照; 所有日志行加 "[<label>] "
        前缀 (main / backup-<uid前8位>); 余额仅主号查询。
日志: 同目录 checkin.log 追加一行 "YYYY-MM-DD HH:MM:SS  [<label>] <结果> [credits_total=<值>]"
退出码: 由主号结果决定: 0=成功或已签 / 1=签到失败 / 2=状态检查失败
"""

import glob
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = Path(__file__).resolve().parent
LOG_PATH = HERE / "checkin.log"

AUTH_FILE = r"C:\Users\?\AppData\Local\CodeBuddyExtension\Data\Public\auth\workbuddy-desktop.info"
UPSTREAM = "https://copilot.tencent.com"
TIMEOUT = 30


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(line):
    """追加一行到 checkin.log 并回显到 stdout。"""
    full = now_str() + "  " + line
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(full + "\n")
    except Exception as e:
        print("log_write_failed: %s" % e)
    print(full)
    return full


# ---------------------------------------------------------------------------
# 3.2 Token 读取 (与适配器一致)
# ---------------------------------------------------------------------------
def load_auth():
    with open(AUTH_FILE, encoding="utf-8") as f:
        d = json.load(f)
    return d["auth"]["accessToken"], d["account"]["uid"]


def api_post(path, token, uid, payload=None, extra_headers=None):
    '''POST 到上游。返回 (status_code, body_text)。HTTP 错误也返回而不是抛。

    extra_headers: 可选附加头（余额接口需要 TuringShield 设备 Token +
    WorkBuddy 客户端身份头，见 fetch_credits）。
    '''
    url = UPSTREAM + path
    data = json.dumps(payload if payload is not None else {}).encode("utf-8")
    headers = {
        "Authorization": "Bearer " + token,
        "X-User-Id": uid,
        "Content-Type": "application/json",
    }
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, data=data, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")

def summarize(data):
    """从签到响应 data 中提取 credit/streak 类字段做摘要。"""
    if not isinstance(data, dict):
        return ""
    hits = []
    for k, v in data.items():
        kl = k.lower()
        if ("credit" in kl or "streak" in kl) and isinstance(v, (str, int, float, bool)):
            hits.append("%s=%s" % (k, v))
    if hits:
        return " ".join(hits)
    return json.dumps(data, ensure_ascii=False)[:120]


def _dig(obj, path):
    """按 path 逐层取值; 任一层缺失/类型不符返回 (False, None)。"""
    cur = obj
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return False, None
        cur = cur[key]
    return True, cur


def _extract_total_dosage(j):
    """多层兜底取 TotalDosage, 全败返回 None。"""
    for path in (
        ("data", "Response", "Data", "TotalDosage"),   # 规格书原始结构
        ("data", "TotalDosage"),                       # 拍平一层
        ("TotalDosage",),                              # 顶层直挂
    ):
        ok, val = _dig(j, path)
        if ok and val is not None:
            return val
    return None


_CLIENT_VERSION = "5.5.6"


def _fetch_device_token():
    '''调 wb_turing_token.js（TuringShield N-API binding）取设备风控 Token。

    与桌面端主进程同一 binding（resources/app.asar.unpacked/native/turing-sdk，
    channelId=109144 取自国内版 cli/product.json）。Token 短生命周期（约 5 分钟
    刷新），每次现取；任何失败返回 None，按软失败走 unknown。
    '''
    try:
        import os
        import subprocess
        js = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wb_turing_token.js")
        r = subprocess.run(["node", js], capture_output=True, text=True, timeout=45)
        out = json.loads(r.stdout)
        if out.get("ok") and out.get("result"):
            return str(out["result"])
    except Exception:
        pass
    return None


def fetch_credits(token, uid):
    '''余额查询（2026-09-18 破解 10085：X-Device-Token 风控头 + 客户端身份头）。

    billing 资源接口有 TuringShield 设备指纹校验，裸 Bearer 一律 403/10085
    （请求不合法）。解法：经 native binding 取设备 Token，再带 WorkBuddy UA
    + X-IDE-* 身份头请求。主接口用 #97550 get-user-resource-summary（网关路由
    无 /v2 前缀，按资源包汇总 剩余/总量），失败回退老接口
    /v2/billing/meter/get-user-resource 的 TotalDosage。全失败记 'unknown'。
    '''
    device_token = _fetch_device_token()
    if not device_token:
        return "unknown"
    extra = {
        "X-Device-Token": device_token,
        "User-Agent": "WorkBuddy/" + _CLIENT_VERSION,
        "X-IDE-Type": "WorkBuddy",
        "X-IDE-Name": "WorkBuddy",
        "X-IDE-Version": _CLIENT_VERSION,
        "X-Product": "WorkBuddy",
    }
    # 1) #97550 新接口：按资源包汇总（剩余/总量）
    try:
        status, text = api_post(
            "/billing/meter/get-user-resource-summary", token, uid, {}, extra_headers=extra)
        if status == 200:
            j = json.loads(text)
            pkgs = ((j.get("data") or {}).get("Packages")) or []
            if pkgs:
                remain = sum(float(p.get("CycleRemainCapacity") or 0) for p in pkgs)
                total = sum(float(p.get("CycleTotalCapacity") or 0) for p in pkgs)
                return "%g/%g" % (remain, total)
    except Exception:
        pass
    # 2) 回退老接口：TotalDosage
    try:
        status, text = api_post(
            "/v2/billing/meter/get-user-resource", token, uid, {}, extra_headers=extra)
        if status == 200:
            j = json.loads(text)
            credits = _extract_total_dosage(j)
            if credits is not None:
                return credits
    except Exception:
        pass
    return "unknown"


def load_backup_accounts():
    """扫描 <脚本目录>/accounts/backup-*.json, 返回冷备号列表。

    每项为 (label, token, uid, expiresAt); label = "backup-" + uid 前 8 位。
    目录不存在或无快照返回 []; 单个文件解析失败跳过并打印一行警告。
    """
    accounts = []
    pattern = str(HERE / "accounts" / "backup-*.json")
    for path in sorted(glob.glob(pattern)):
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
            token = d["accessToken"]
            uid = d["uid"]
            expires_at = d.get("expiresAt")
            label = "backup-" + str(uid)[:8]
            accounts.append((label, token, uid, expires_at))
        except Exception as e:
            print("backup_load_warn: %s: %s" % (path, e))
    return accounts


def run_account(label, token, uid, is_main, expires_at=None):
    """对单个账号执行 状态检查 -> 签到 -> (仅主号) 余额, 返回 rc。

    rc 语义与旧 main 一致: 0=成功或已签 / 1=签到失败 / 2=状态检查失败。
    所有 log 行加 "[<label>] " 前缀; 冷备号不查余额。
    """
    # 冷备号过期预警: 不阻断, 仍尝试签到 (服务器可能仍接受旧 token)
    if not is_main and expires_at is not None:
        try:
            if int(expires_at) < int(datetime.now().timestamp() * 1000):
                expired_day = datetime.fromtimestamp(int(expires_at) / 1000).strftime("%Y-%m-%d")
                log("[%s] BACKUP_TOKEN_EXPIRED (expired %s)" % (label, expired_day))
        except Exception:
            pass

    # 状态检查
    try:
        status, text = api_post("/v2/billing/meter/checkin-status", token, uid)
    except Exception as e:
        log("[%s] status_check_failed: %s" % (label, e))
        return 2
    if status != 200:
        log("[%s] status_check_failed: %s" % (label, text[:200]))
        return 2
    try:
        jstatus = json.loads(text)
    except Exception:
        log("[%s] status_check_failed: %s" % (label, text[:200]))
        return 2
    if not isinstance(jstatus, dict) or jstatus.get("code") != 0:
        log("[%s] status_check_failed: %s" % (label, text[:200]))
        return 2

    # 签到
    try:
        status2, text2 = api_post("/v2/billing/meter/daily-checkin", token, uid, {})
    except Exception as e:
        log("[%s] CHECKIN_FAIL - %s" % (label, e))
        return 1

    jchk = None
    try:
        jchk = json.loads(text2)
    except Exception:
        jchk = None

    code = jchk.get("code") if isinstance(jchk, dict) else None
    msg = ""
    if isinstance(jchk, dict):
        msg = jchk.get("msg") or jchk.get("message") or ""

    if code == 0:
        summary = summarize(jchk.get("data"))
        result = ("CHECKIN_OK " + summary).strip()
        rc = 0
    elif code == 10001 or ("已签到" in str(msg)) or ("已签到" in text2):
        result = "ALREADY_CHECKED_IN"
        rc = 0
    else:
        result = ("CHECKIN_FAIL %s %s" % (code, msg)).strip()
        rc = 1

    # 余额查询只对主号执行 (冷备号设备 token 与账号绑定关系未验证, 避免噪音)
    if is_main:
        credits = fetch_credits(token, uid)
        log("[%s] %s  credits_total=%s" % (label, result, credits))
    else:
        log("[%s] %s" % (label, result))
    return rc


def main():
    # 1. 主号凭据
    rc = 0
    try:
        main_entry = ("main",) + load_auth()
    except Exception as e:
        log("[main] auth_failed: %s" % e)
        main_entry = ("main", None, None, None)
        rc = 2

    # 账号列表: 主号 + 冷备号快照 (无 accounts/ 目录时只有主号)
    accounts = [main_entry] + load_backup_accounts()

    for acct in accounts:
        label = acct[0]
        acct_token = acct[1]
        acct_uid = acct[2]
        acct_exp = acct[3] if len(acct) > 3 else None
        if label == "main" and acct_token is None:
            log("[main] SKIP (auth file missing)")
            continue
        r = run_account(label, acct_token, acct_uid, label == "main", acct_exp)
        # 退出码由主号结果决定; 冷备号失败不改变 rc, 仅记日志
        if label == "main":
            rc = r
    return rc


if __name__ == "__main__":
    sys.exit(main())
