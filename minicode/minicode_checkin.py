#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MiniCode（mcode / MiniMax Code CLI）账号每日福利签到（token plan 每日送积分）。

原理（2026-10-01 minicode 开源仓库源码实证）：
- 签到接口走 MiniMax 公共网关（tui/src/runtime/public-gateway.ts + tui/src/checkin/）：
    GET  {agent.minimaxi.com}/minimax-cloud/api/v1/signin/status  查 7 天面板
    POST {agent.minimaxi.com}/minimax-cloud/api/v1/signin/claim    body {} 领取今日
- 请求需三件套（均源码常量）：
    1) Authorization: Bearer <mcode 登录 token>（真实鉴权）；
    2) URL 公共查询串 device_platform=web&biz_id=3&app_id=3001&version_code=22201
       &is_desktop=1&desktop_version=<版本>&unix=<ms>&timezone_offset=<秒>
       &sys_language=zh&lang=zh&device_id=0&os_name=win32&browser_name=mcode
       &user_id=<realUserID>&client=mcode；
    3) 客户端归属头（"wire-protocol constants"，非安全边界）：
       yy = md5(encodeURIComponent(path?query) + "_" + <body|'{}'> + md5(String(unix_ms)) + "ooui")
       x-timestamp = 秒
       x-signature = md5(秒 + "I*7Cf%WZ#S&%1RlZJ&C2" + <body|''>)
- realUserID 无本地缓存时经开放平台取（matrix-account-client.ts fetchAccountIdentity）：
    GET {www.minimaxi.com}/v1/api/user/info（matrix 查询串风味，user_id=0）
- 登录 token 复用反代通道模块 proxy_core.minicode：mtime 感知 + 过期自动 OAuth 刷新
  （refresh_token 轮换立即写回、generation 不动、与官方客户端无缝共存）。

日志只记录结果与摘要，绝不落 token（SECRET 正则兜底脱敏）。
与 codely_points_checkin.py / wb_checkin.py 同款独立脚本 + 计划任务调度。
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

BASE = Path(__file__).resolve().parent.parent          # 中转项目根
sys.path.insert(0, str(BASE))

from proxy_core import minicode  # noqa: E402
from proxy_core.settings import load_config  # noqa: E402

LOG = Path(__file__).resolve().parent / "checkin.log"
ACCOUNTS_FILE = Path(__file__).resolve().parent / "accounts.json"

APP_VERSION = "0.5.10"                    # tui/package.json 版本（desktop_version 查询参数）
GATEWAY_ORIGIN = "https://agent.minimaxi.com"      # 签到公共网关（cn prod）
OPEN_PLATFORM_ORIGIN = "https://www.minimaxi.com"  # 开放平台（realUserID 解析）
STATUS_PATH = "/minimax-cloud/api/v1/signin/status"
CLAIM_PATH = "/minimax-cloud/api/v1/signin/claim"
USER_INFO_PATH = "/v1/api/user/info"
NEED_LOGIN_CODE = 1_000_048              # base_resp 业务码：登录失效

# 日志脱敏：任何 bearer/JWT 形态串统一替换
SECRET = re.compile(r"(?i)bearer\s+\S+|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*|mmoat_\S+|mmort_\S+")


def log(msg):
    line = datetime.now().strftime("%Y-%m-%d %H:%M:%S") + "  " + msg
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(line)


def load_accounts(accounts_file=None):
    """minicode/accounts.json -> [{"label","auth_path"}...]（多账号追加）。

    - dict {"accounts": [...]} 或裸 list 均可；
    - enabled=false 跳过（记一行日志）；缺 auth_path 跳过；
    - label 缺省取 auth 文件名 stem。
    容错：文件缺失/损坏 -> []（仅主号签到，行为同旧版）。"""
    path = Path(accounts_file) if accounts_file else ACCOUNTS_FILE
    try:
        doc = json.load(path.open(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except Exception as e:
        log(f"accounts.json 解析失败: {SECRET.sub('<redacted>', str(e))[:120]}")
        return []
    items = doc.get("accounts") if isinstance(doc, dict) else doc
    if not isinstance(items, list):
        log("accounts.json 结构应为 list 或 {accounts: list}，忽略")
        return []
    out = []
    for i, it in enumerate(items, 1):
        if not isinstance(it, dict):
            continue
        ap = str(it.get("auth_path") or "").strip()
        if not ap:
            log(f"accounts[{i}] 缺 auth_path，跳过")
            continue
        if it.get("enabled", True) is False:
            log(f"accounts[{i}] enabled=false，跳过")
            continue
        label = str(it.get("label") or Path(ap).stem or f"account-{i}")
        out.append({"label": label, "auth_path": ap})
    return out


def _md5(text):
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def _encode_uri_component(text):
    """JS encodeURIComponent 语义（Python quote 默认不编码 -_.~，此处补齐 !~*'()）。"""
    return urllib.parse.quote(text, safe="-_.!~*'()")


def _tz_seconds():
    try:
        return int(datetime.now().astimezone().utcoffset().total_seconds())
    except Exception:
        return 28800


def _request(method, origin, path, token, real_user_id, flavor, body=None, timeout=30):
    """发起一次公共网关/matrix 风味请求，返回 (http_status, json_or_text)。"""
    now_ms = int(time.time() * 1000)
    second = now_ms // 1000
    if flavor == "gateway":
        # public-gateway.ts 查询串风味（签到接口）
        query = {
            "device_platform": "web", "biz_id": "3", "app_id": "3001",
            "version_code": "22201", "is_desktop": "1",
            "desktop_version": APP_VERSION, "unix": str(now_ms),
            "timezone_offset": str(_tz_seconds()), "sys_language": "zh", "lang": "zh",
            "device_id": "0", "os_name": "win32", "browser_name": "mcode",
            "user_id": str(real_user_id or "0"), "client": "mcode",
        }
    else:
        # matrix-account-client.ts buildUrl 风味（user/info 等账号接口）
        query = {
            "device_platform": "mcode", "biz_id": "3", "app_id": "3001",
            "version_code": "22201", "unix": str(now_ms),
            "timezone_offset": str(_tz_seconds()), "sys_language": "zh", "lang": "zh",
            "device_id": "0", "os_name": "win32", "browser_name": "mcode",
            "user_id": str(real_user_id or "0"), "client": "mcode",
        }
    qs = urllib.parse.urlencode(query)
    url = f"{origin}{path}?{qs}"
    path_with_search = f"{path}?{qs}"
    yy_body = body if body is not None else "{}"
    sig_body = body if body is not None else ""
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "MiniMaxCode",
        "Authorization": f"Bearer {token}",
        "yy": _md5(f"{_encode_uri_component(path_with_search)}_{yy_body}{_md5(str(now_ms))}ooui"),
        "x-timestamp": str(second),
        "x-signature": _md5(f"{second}I*7Cf%WZ#S&%1RlZJ&C2{sig_body}"),
    }
    req = urllib.request.Request(url, data=(body.encode() if body is not None else None),
                                 method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def _parse_envelope(status, text, what):
    """校验 {base_resp, data} 信封；返回 (ok, data, err_note)。"""
    if status in (401, 403):
        return None, "AUTH"
    try:
        body = json.loads(text)
    except Exception:
        return None, f"BAD_JSON:{text[:120]}"
    base = body.get("base_resp") or {}
    code = base.get("status_code", 0)
    if isinstance(code, int) and code != 0:
        if code == NEED_LOGIN_CODE:
            return None, "AUTH"
        return None, f"BIZ_{code}:{str(base.get('status_msg') or '')[:80]}"
    data = body.get("data")
    if not isinstance(data, dict):
        return None, f"NO_DATA:{text[:120]}"
    return data, None


def _fetch_real_user_id(token, timeout):
    """经开放平台 user/info 解析 realUserID（与 fetchAccountIdentity 同构）。"""
    status, text = _request("GET", OPEN_PLATFORM_ORIGIN, USER_INFO_PATH, token,
                            "0", "matrix", None, timeout)
    data, err = _parse_envelope(status, text, "user info")
    if err:
        raise RuntimeError(f"realUserID 解析失败 {err}")
    user = None
    if isinstance(data, dict):
        user = (data.get("userInfo") or data.get("user_info")
                or data.get("userinfo"))
    if not isinstance(user, dict):
        raise RuntimeError("user info 响应缺 userInfo")
    uid = user.get("realUserID") or user.get("real_user_id")
    if not uid:
        raise RuntimeError("user info 响应缺 realUserID")
    return str(uid)


def _day_status_name(v):
    return {1: "未开始", 2: "可领", 3: "已领", 4: "禁用"}.get(v, str(v))


def _panel_line(panel):
    days = panel.get("days") or []
    today = next((d for d in days if d.get("is_today")), None)
    if today:
        return f"今日第{today.get('day_no')}天[{_day_status_name(today.get('status'))}] " \
               f"奖励{today.get('points')}积分"
    return "面板无今日条目"


def checkin_channel(ch, timeout, dry_run, uid_seen=None):
    """单账号签到：token（自动刷新）→ realUserID → status →（可领则）claim。

    返回分类：CLAIMED / ALREADY / UNAVAILABLE / AUTH_ERROR / FAILED。
    """
    label = ch.label
    try:
        token = ch.get_token()   # 过期自动 OAuth 刷新 + 写回（复用反代通道逻辑）
    except Exception as e:
        log(f"[{label}] token 获取/刷新失败（需 mcode login？）: {SECRET.sub('<redacted>', str(e))[:160]}")
        return "AUTH_ERROR"
    try:
        uid = _fetch_real_user_id(token, timeout)
    except Exception as e:
        log(f"[{label}] {SECRET.sub('<redacted>', str(e))[:160]}")
        return "FAILED"

    # ── uid 身份去重（2026-10-13）：主号通道跟随当前登录态，
    # 可能与 accounts.json 快照是同一账号——同 uid 只签一次，
    # 后到者直接跳过（省一次 claim 请求与日志噪音）。
    if uid_seen is not None:
        if uid in uid_seen:
            log(f"[{label}] 与已签到账号同身份（uid 相同），跳过")
            return "ALREADY"
        uid_seen.add(uid)

    status, text = _request("GET", GATEWAY_ORIGIN, STATUS_PATH, token, uid,
                            "gateway", None, timeout)
    panel, err = _parse_envelope(status, text, "signin status")
    if err == "AUTH":
        log(f"[{label}] 登录已失效（401/1000048），请 mcode login 后重试")
        return "AUTH_ERROR"
    if err:
        log(f"[{label}] 签到面板查询失败: {err}")
        return "FAILED"

    days = panel.get("days") or []
    today = next((d for d in days if d.get("is_today")), None)
    log(f"[{label}] 签到面板: {_panel_line(panel)}（7日: "
        + " ".join(f"D{d.get('day_no')}{_day_status_name(d.get('status'))}" for d in days)
        + "）")
    if today and today.get("status") == 3:      # 已领
        log(f"[{label}] 今日已签过，跳过")
        return "ALREADY"
    if not any(d.get("status") == 2 for d in days):
        log(f"[{label}] 面板无可领日（周期可能已完成/断裂），跳过")
        return "UNAVAILABLE"

    if dry_run:
        log(f"[{label}] dry-run：今日可领，未实际领取")
        return "CLAIMABLE_DRY"

    status, text = _request("POST", GATEWAY_ORIGIN, CLAIM_PATH, token, uid,
                            "gateway", "{}", timeout)
    claim, err = _parse_envelope(status, text, "signin claim")
    if err == "AUTH":
        log(f"[{label}] 领取时登录失效，请 mcode login 后重试")
        return "AUTH_ERROR"
    if err:
        log(f"[{label}] 签到领取失败: {err}")
        return "FAILED"
    result = claim.get("claim_result")
    day_no = claim.get("day_no")
    points = claim.get("points")
    if result == 1:
        log(f"[{label}] ✓ 签到成功：第{day_no}天 +{points}积分（{_panel_line(claim.get('panel') or {})}）")
        return "CLAIMED"
    if result == 2:
        log(f"[{label}] 今日已签过（服务端判定），跳过")
        return "ALREADY"
    log(f"[{label}] 未知的 claim_result={result}")
    return "FAILED"


def main():
    ap = argparse.ArgumentParser(description="MiniCode（mcode）账号每日福利签到")
    ap.add_argument("--timeout", type=int, default=30)
    ap.add_argument("--delay", type=float, default=2.0, help="多账号间隔秒数")
    ap.add_argument("--dry-run", action="store_true", help="只查面板不领取")
    args = ap.parse_args()

    cfg = load_config()
    minicode.reload_channels_from_config(cfg)
    targets = []
    for name, p in (cfg.get("providers") or {}).items():
        if isinstance(p, dict) and p.get("type") == "minicode":
            targets.extend(minicode.channels_for(name))

    # ── 多账号（2026-10-13）：minicode/accounts.json 追加账号 ──
    # 主号 = config providers 的 minicode 通道；小号 = accounts.json
    # 逐条 auth_path（各自 mcode 登录后拷贝的 auth.json，凭证
    # 刷新/写回按文件隔离互不影响）。同一凭证文件只签一次。
    seen_paths = set()
    for ch in targets:
        if ch.auth_path:
            seen_paths.add(os.path.normcase(os.path.abspath(ch.auth_path)))
    base_p = next((p for p in (cfg.get("providers") or {}).values()
                   if isinstance(p, dict) and p.get("type") == "minicode"), {})
    region = str(base_p.get("region") or "cn")
    for acc in load_accounts():
        ap = os.path.normcase(os.path.abspath(acc["auth_path"]))
        if ap in seen_paths:
            log(f"[{acc['label']}] 与主号同一 auth 文件，去重跳过")
            continue
        seen_paths.add(ap)
        if not Path(acc["auth_path"]).exists():
            log(f"[{acc['label']}] auth 文件不存在，跳过: {acc['auth_path']}")
            continue
        targets.append(minicode.MiniCodeChannel(
            "minicode", {"auth_path": acc["auth_path"], "region": region},
            label=acc["label"]))
    if not targets:
        log("config.json 无 type=minicode provider，退出")
        sys.exit(1)

    log(f"==== MiniCode 签到开始（{len(targets)} 账号，dry-run={args.dry_run}）====")
    ok, bad = {"CLAIMED", "ALREADY", "UNAVAILABLE", "CLAIMABLE_DRY"}, 0
    summary = {}
    uid_seen = set()
    for i, ch in enumerate(targets):
        if i:
            time.sleep(args.delay)
        summary[ch.label] = checkin_channel(ch, args.timeout, args.dry_run,
                                            uid_seen)
    good = sum(1 for v in summary.values() if v in ok)
    log("==== 签到结束：" + "、".join(f"{k}={v}" for k, v in summary.items()) + " ====")
    sys.exit(0 if good == len(targets) else 1)


if __name__ == "__main__":
    main()
