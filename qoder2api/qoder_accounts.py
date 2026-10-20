"""qoder_accounts.py —— Qoder 双区域账号池、OAuth 设备授权与凭证生命周期

覆盖与 WorkBuddy 网关同等完整的账号能力：

  - 双区域常量表 REALM_CONFIGS（国内 qoder.com.cn / 国际 qoder.com）
  - OAuth 设备授权（PKCE S256，浏览器授权 + /deviceToken/poll 轮询，
    dt- 30 天 / drt- 1 年）——免桌面客户端一键登录
  - PAT 导入（pt- 长期令牌 -> jobToken 交换 jt-/jrt-）
  - 按 token 前缀路由的刷新（drt- -> deviceToken/refresh；
    jrt- -> jobToken/refresh，失败回落 PAT 重新交换）
  - 每日签到 / 额度（quota）/ 套餐（plan）查询
  - 会话亲和（同一对话固定落到同一账号）与轮询负载
  - 账号导入导出（Dry-Run 预检）与 JSON 持久化（原子写）
"""
import base64
import collections
import datetime
import ipaddress
import json
import os
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
import uuid

from qoder_fingerprint import (derive_id, generate_request_id,
                               derive_machine_token, derive_machine_type,
                               vm_status)

# ---------------------------------------------------------------------------
# 区域常量（逆向自官方桌面/CLI 客户端）
# ---------------------------------------------------------------------------
REALM_CONFIGS = {
    "cn": {
        "name": "国内版 (China)",
        "openapi": "https://openapi.qoder.com.cn",
        "gateway": "https://gateway.qoder.com.cn",
        "website": "https://qoder.com.cn",
        "client_id": "1c5e33e1-364d-4ce6-b02c-acaa81274a5c",
        "redirect_uri": "qoder-work-cn://",
        "domain": "qoder.com.cn",
        "ua": "QoderWork/1.1.64",
        # has_checkin 只是"历史上该区域曾开放 sash 签到"的提示位，**不再作为
        # 门控**：能力改为运行时探测（见 Account.checkin_capability）。官方把
        # 每日领取活动搬到 campaign 平台后，任何区域都可能新增/下线接口。
        "has_checkin": True,
        "send_client_id": True,    # CN 设备授权 URL: client_id + machine_id + redirect_uri
        "send_redirect_uri": True,
        "nonce_dashed": True,      # CN nonce 使用带横线 uuid
        "home_dir": ".qoder-cn",   # 官方客户端本地目录（凭证 / 模型目录缓存）
        "app_dir": "com.qodercn.app.stable",   # 桌面 App Roaming 数据目录
    },
    "intl": {
        "name": "国际版 (Global)",
        "openapi": "https://openapi.qoder.sh",
        # 推理主机取自 0.4.3 客户端的 endpoint 缓存/内置候选（api1 主选，
        # api2/api3 为官方故障切换域名）：api1 连不上时按顺序切换。
        "gateway": "https://api1.qoder.sh",
        "gateway_fallbacks": ("https://api2.qoder.sh", "https://api3.qoder.sh"),
        "website": "https://qoder.com",
        "client_id": "e883ade2-e6e3-4d6d-adf7-f92ceff5fdcb",
        "redirect_uri": "qoder://aicoding.aicoding-agent/login-success",
        "domain": "qoder.com",
        "ua": "Qoder/1.1.64",
        # 国际版目前 /sash/api/v1/me/daily-check-in/* 返回 404（实测），但活动
        # 页面同样挂着"每日领取 100 Credits"。该字段仅作提示，门控靠运行时探测。
        "has_checkin": False,
        "send_client_id": True,    # Intl 设备授权 URL: client_id + machine_id (无 redirect_uri)
        "send_redirect_uri": False,
        "nonce_dashed": False,     # intl nonce 为 32-hex uuid-simple
        "home_dir": ".qoder",
        "app_dir": "com.qoder.app.stable",
    },
}

CLIENT_UA = "Go-http-client/2.0"
LOGIN_TTL_SECONDS = 600
# 账号文件写入锁（/tasks 并行刷新时多个字段各自 save，必须串行落盘）
_SAVE_LOCK = threading.Lock()
DEFAULT_USER_TYPE = "personal_professional_trial"

# 业务端点（全部挂 openapi 基址，纯 Bearer，无 COSY 签名）
PATH_DEVICE_POLL = "/api/v1/deviceToken/poll"
PATH_DEVICE_REFRESH = "/api/v1/deviceToken/refresh"
PATH_JOB_EXCHANGE = "/api/v1/jobToken/exchange"
PATH_JOB_REFRESH = "/api/v1/jobToken/refresh"
PATH_USERINFO = "/api/v1/userinfo"
PATH_QUOTA = "/api/v2/quota/usage"
PATH_PLAN = "/api/v2/user/plan"
PATH_CHECKIN_STATUS = "/sash/api/v1/me/daily-check-in/status"
PATH_CHECKIN_CLAIM = "/sash/api/v1/me/daily-check-in/claim"
PATH_PRO_ELIGIBILITY = "/sash/api/v1/me/pro-upgrade/eligibility"
PATH_PRO_CLAIM = "/sash/api/v1/me/pro-upgrade/claim"
# 官方新活动平台（双区域通用，实测 cn/intl 均 200）：服务端下发活动列表与
# campaignUrl/JS，领取动作由桌面客户端承接；网关用它做状态呈现与提示。
PATH_CAMPAIGNS = "/sash/api/v1/me/campaigns"
# 单个活动的奖励查询 / 领取（逆向自官方 growth-page/activity-iframe 页面：
#   GET  /sash/api/v1/me/campaigns/{campaignId}/reward
#   POST /sash/api/v1/me/campaigns/{campaignId}/claim   —— 领取（幂等：
#        已领取返回 {"status":"CLAIMED","replayed":true}，不会重复发放）
PATH_CAMPAIGN_REWARD = "/sash/api/v1/me/campaigns/%s/reward"
PATH_CAMPAIGN_CLAIM = "/sash/api/v1/me/campaigns/%s/claim"

# 桌面端专用请求头（活动平台必需；CLI=5 / QoderWork=6 / 桌面端=10）
DESKTOP_CLIENT_TYPE = "10"
DESKTOP_CLIENT_VERSION = "0.4.3"      # 可用 QD_DESKTOP_VERSION 覆盖
MACHINE_OS = "x86_64_win32"
MACHINE_HOSTNAME = "DESKTOP-QODER"


def desktop_version():
    """桌面端版本号（Cosy-Version）；客户端更新后可用环境变量覆盖，
    或直接跑 `python _refresh_catalog.py` 时按已安装客户端自动对齐。"""
    return (os.environ.get("QD_DESKTOP_VERSION") or DESKTOP_CLIENT_VERSION).strip() \
        or DESKTOP_CLIENT_VERSION


# ---------------------------------------------------------------------------
# 官方桌面端原生风控身份（activity/campaign 列表按它过滤，必须是"真"身份）
# ---------------------------------------------------------------------------
# 官方桌面端在调用活动平台前，会 spawn 自己的原生桥取机器身份：
#     <install>/resources/umid/runtime-info.exe prod --account-stdin
#     stdin: {"account": <uid>}   stdout: {"machineToken","machineType","machineCode",...}
# 服务端**按这些值过滤设备定向活动**：派生的假身份不会报错，但活动列表里
# 会静默少掉"每日领取 100 Credits"这类条目（实测：换用原生身份后立刻出现
# CLAIMABLE 活动）。因此网关优先调用同一个官方二进制取真值，失败才回退派生值。
# 原生身份缓存：组件的输出由**种子文件** $HOME/.config/.locale_cfg 决定——
# 同一种子下重复调用返回同一个身份（#18 实测：69 分钟 24 次采样不变），
# 换身份只能靠"清种子 + 重调组件"（见 _purge_identity_seed）。旧身份长期被
# 服务端接受（实测复用 25s+ 依然 showCampaign=true），真正的成本是每次调
# 组件要跑约 3.7 秒的官方二进制。因此做长缓存（30 分钟）+ 落盘缓存，并用
# "列表被判为未认可时清种子换新身份重试一次"兜底自愈。
# 注意：身份是**机器级**的（不同账号/不存在的账号 id 都返回同一份），
# 因此按区域缓存即可，同一台机器上的多个账号共用是正确的。
NATIVE_IDENTITY_TTL = 1800
# 首次落盘前的**自适应**多数表决（#18 补充：组件的 VM 判定会抖到另一分支
# KVM/13 ↔ Docker/50——报告者实测 1.56%，本机复现 10.29%；单次采样会把用户
# 永久固定到少数派）。先投 IDENTITY_VOTE_ROUNDS 次：全一致直接采纳；出现
# 分歧再补 IDENTITY_VOTE_EXTEND_ROUNDS 次（上限 5，首次延迟有界）。
# 轮数按**最坏抖动率**选：10% 抖动下 3 轮误判≈2.7%，自适应到 5 轮≈0.85%。
# 表决只在"首次无可用缓存"这一次发生；可用 QD_MACHINE_IDENTITY_VOTE=0 关闭。
IDENTITY_VOTE_ROUNDS = 3
IDENTITY_VOTE_EXTEND_ROUNDS = 2
# machine_identity_source（及 campaigns().identity）的合法取值只有两种：
#   "runtime-info" —— runtime-info.exe 原生桥给出真身份（native_machine_identity）
#   "derived"      —— 无原生桥时的派生回退（desktop_headers）
# 生产端一律使用本常量。历史上消费端（campaigns() 自愈条件）误写为 "native"，
# 与生产端字面量不一致导致该分支永不命中（口径分裂 bug，已修）；"native" 仅
# 作为历史/测试桩别名在消费端兼容，不得作为新的生产端取值。
MACHINE_IDENTITY_NATIVE = "runtime-info"
# desktop_headers() 本次实际是否携带 cosy-machine* 机器头（与"身份来源"
# machine_identity_source 是两个正交维度，勿混用）：
#   "native"  —— 本次发送了原生桥给出的全套六头
#   "omitted" —— 本次未发送任何 cosy-machine* 头（无原生桥时的正确行为；
#                issue #10 修复前会发派生假头，实测会被服务端整条过滤活动）
MACHINE_HEADERS_NATIVE = "native"
MACHINE_HEADERS_OMITTED = "omitted"
_native_exe_cache = {}
_native_ident_cache = {}


def _localappdata():
    return os.environ.get("LOCALAPPDATA") or os.path.join(
        os.path.expanduser("~"), "AppData", "Local")


def _read_ini(path, key):
    """读 launcher 的 state.ini（UTF-8 或 UTF-16），返回 key=value 的 value。"""
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except Exception:
        return ""
    for enc in ("utf-8-sig", "utf-16"):
        try:
            text = raw.decode(enc)
        except Exception:
            continue
        for line in text.splitlines():
            line = line.strip()
            if line.lower().startswith(key.lower() + "="):
                return line.split("=", 1)[1].strip()
    return ""


def desktop_install_dir(realm):
    """桌面端安装目录（launcher state.ini 的 installDir；找不到返回空串）。"""
    base = _localappdata()
    names = ("Qoder CN", "QoderCN", "Qoder") if realm == "cn" else ("Qoder",)
    for name in names:
        for launcher in ("%s Launcher" % name, "Launcher"):
            ini = os.path.join(base, name, launcher, "state.ini")
            if os.path.isfile(ini):
                d = _read_ini(ini, "installDir")
                if d and os.path.isdir(d):
                    return d
    for name in names:
        d = os.path.join(base, "Programs", name)
        if os.path.isdir(d):
            return d
    return ""


def runtime_info_exe(realm):
    """定位 runtime-info 原生风控身份桥；找不到返回空串。

    查找顺序（先桌面客户端，再提取目录）：
      1. <桌面客户端安装目录>/resources/umid/runtime-info.exe（Windows 桌面端）；
      2. POSIX：$QD_UMID_DIR/runtime-info、<repo>/umid/runtime-info
         （由 _install_umid.py 从 @qoder-ai/qodercli 提取；两者调用契约一致。
          Windows 不参与此分支，保持原有桌面客户端路径逐字不变）。

    可用 QD_NATIVE_IDENTITY=0 关闭（测试/受限环境不希望拉起客户端二进制时）。
    """
    if (os.environ.get("QD_NATIVE_IDENTITY") or "1").strip() in ("0", "false", "no"):
        return ""
    if realm in _native_exe_cache:
        return _native_exe_cache[realm]
    found = ""
    roots = [os.path.join(desktop_install_dir(realm), "resources", "umid")]
    for root in roots:
        cand = os.path.join(root, "runtime-info.exe")
        if os.path.isfile(cand):
            found = cand
            break
    if not found and os.name == "posix":
        # 提取目录（_install_umid.py 的落地位置；Windows 不参与本分支，
        # 保持原有桌面客户端路径逐字不变）：显式 QD_UMID_DIR 优先于约定目录。
        extracted = []
        umid_dir = (os.environ.get("QD_UMID_DIR") or "").strip()
        if umid_dir:
            extracted.append(umid_dir)
        extracted.append(os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "umid"))
        for root in extracted:
            cand = os.path.join(root, "runtime-info")
            if os.path.isfile(cand):
                found = cand
                break
    _native_exe_cache[realm] = found
    return found


# ---------------------------------------------------------------------------
# 原生桥执行失败的一次性提示（issue #12）
# ---------------------------------------------------------------------------
# 「组件不存在」（runtime_info_exe 返回空串）是正常降级、保持静默；「组件在但
# 执行失败」在子进程 exec 阶段才暴露（典型：alpine 缺 glibc loader / libstdc++，
# 报 FileNotFoundError: /lib64/ld-linux-x86-64.so.2），过去被静默吞掉后只剩
# 「身份退化为 derived」，最容易被误诊成路径没配好。同类失败同进程只提示一次，
# 避免批量签到/巡检时刷屏。
_runtime_info_warned = set()


def _runtime_info_warn(category, message):
    """把原生桥失败按类别提示到 stderr（同一类别进程内只打一次）。"""
    if category in _runtime_info_warned:
        return
    _runtime_info_warned.add(category)
    try:
        import sys
        print("[runtime-info] %s" % message, file=sys.stderr)
    except Exception:
        pass


def run_runtime_info(realm, account_id=""):
    """调用 runtime-info 原生桥，返回其 JSON（失败返回 {}）。

    account 为空串同样可用：机器身份是机器级的，活动平台之外（如虚拟化体检）
    不需要账号上下文。

    失败可见性（issue #12）：「组件不存在」（runtime_info_exe 返回空串）保持
    静默；「组件在但执行失败」（缺 glibc loader / libstdc++、执行位丢失、输出
    异常等）会把底层异常打一次到 stderr——两种情况都照旧返回 {} 供上层回退。
    """
    exe = runtime_info_exe(realm)
    if not exe:
        return {}
    try:
        import subprocess
        proc = subprocess.run(
            [exe, "prod", "--account-stdin"],
            input=json.dumps({"account": account_id or ""}).encode("utf-8") + b" ",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=25,
            cwd=os.path.dirname(exe))
        out = proc.stdout.decode("utf-8", "replace").strip()
        if out:
            return json.loads(out.split("\n", 1)[0])
        _runtime_info_warn(
            "empty-output",
            "%s 运行结束但没有输出（exit=%s），身份将退化为 derived"
            % (exe, proc.returncode))
    except OSError as exc:
        # 文件存在但 exec 失败：FileNotFoundError 缺的通常不是组件本身，而是它
        # 的动态 loader（glibc 的 /lib64/ld-linux-x86-64.so.2，alpine/musl 没有）；
        # 底层异常必须透出来，否则会被误读成「路径没配好」。
        _runtime_info_warn(
            "exec:" + type(exc).__name__,
            "无法执行 %s：%s: %s（组件存在但跑不起来；Docker/alpine 需要 "
            "gcompat libstdc++ libgcc 兼容层，见 README 已知限制）"
            % (exe, type(exc).__name__, exc))
    except Exception as exc:
        # 其它失败（超时 / 输出不是 JSON / 非 OSError 异常）
        _runtime_info_warn(
            "run:" + type(exc).__name__,
            "%s 调用失败：%s: %s" % (exe, type(exc).__name__, exc))
    return {}


_vm_cache = {}


def local_vm_status(realm=None, force=False):
    """本机虚拟化状态（**中文输出**）：看板与 _diag_campaign.py 共用。

    优先用官方风控桥的 vmInfo（官方客户端就是这么判的），桥不可用时退化为
    本机交叉校验（CPU 型号 / 系统制造商 / 虚拟化驱动文件）。结果缓存 300s。
    """
    r = realm if realm in ("cn", "intl") else "cn"
    now = time.time()
    hit = _vm_cache.get(r)
    if hit and not force and now - hit[0] < 300:
        return hit[1]
    data = run_runtime_info(r)
    vm_info = data.get("vmInfo") if isinstance(data.get("vmInfo"), dict) else {}
    st = vm_status(bridge_vm_info=vm_info, bridge_available=bool(runtime_info_exe(r)))
    st["realm"] = r
    st["bridge_available"] = bool(runtime_info_exe(r))
    _vm_cache[r] = (now, st)
    return st


# ---------------------------------------------------------------------------
# 机器身份落盘缓存（issue #18：Docker 重建/升级容器后设备身份不变）
# ---------------------------------------------------------------------------
# 组件每次调用都会给出**新**身份——所以"调用后顺便更新缓存"等于没缓存。
# 本实现的纪律（设计 .team/00-IDENTITY-CACHE-DESIGN.md §9）：
#   · **缓存优先**：落盘命中就不调组件（「重建不换」的唯一来源）；
#   · 落盘只在两处写入：① 落盘没有该 realm 的可用记录（不存在 / 已按 TTL
#     失效 / 损坏）时取到的新身份；② 自愈路径（campaigns() 被判未认可后的
#     force 刷新）覆盖。其它路径一律不写，否则身份又会被自己刷掉。
#   · 失败回退：组件不可用时用落盘缓存（哪怕过期）——比 derived 假身份好。
_IDENTITY_CACHE_LOCK = threading.Lock()
_identity_cache_reuse_logged = set()     # 「复用缓存」INFO：每个 realm 一次
_identity_cache_corrupt_warned = False   # 「损坏/字段不全」WARN：进程内一次
_identity_cache_diff_warned = False      # 「与组件输出不一致」INFO：进程内一次
_identity_reset_done = False             # QD_MACHINE_IDENTITY_RESET 只消费一次


def machine_identity_cache_path():
    """机器身份落盘缓存路径：$ACCOUNTS_DIR/machine_identity.json（未设时用
    <repo>/accounts/machine_identity.json，与网关的账号目录一致）。"""
    base = (os.environ.get("ACCOUNTS_DIR") or "").strip()
    if not base:
        base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "accounts")
    return os.path.join(base, "machine_identity.json")


def _identity_cache_enabled():
    """QD_MACHINE_IDENTITY_CACHE：auto/on（默认启用）/ off（关闭＝仅内存＝旧行为）。"""
    value = (os.environ.get("QD_MACHINE_IDENTITY_CACHE") or "auto").strip().lower()
    return value not in ("off", "0", "false", "no", "disable", "disabled")


def _identity_vote_enabled():
    """QD_MACHINE_IDENTITY_VOTE：默认开启；0/off 跳过首次表决（只调一次组件）。

    给"不在乎这 1~2% 抖动、想要最快首启"的用户——首启表决约需 3×组件耗时。
    """
    value = (os.environ.get("QD_MACHINE_IDENTITY_VOTE") or "1").strip().lower()
    return value not in ("off", "0", "false", "no", "disable", "disabled")


def _identity_cache_ttl():
    """QD_MACHINE_IDENTITY_CACHE_TTL：秒；0（默认）＝不过期。

    正数=到期后清组件种子并重取（**真的换出新身份**，见 _purge_identity_seed）
    ——这是设计 §3.3 的"定期轮换"开关；到期只是重新调组件、不换种子的话，
    同种子下会拿回同一个身份，开关等于没生效。
    """
    try:
        ttl = float(os.environ.get("QD_MACHINE_IDENTITY_CACHE_TTL") or 0)
    except (TypeError, ValueError):
        ttl = 0.0
    return max(0.0, ttl)


def _identity_cache_log(level, message):
    """缓存事件输出到 stderr；去重由各调用点的标记控制。"""
    try:
        import sys
        print("[machine-identity] %s: %s" % (level, message), file=sys.stderr)
    except Exception:
        pass


def _identity_entry_to_ident(entry):
    """缓存条目 -> ident dict；字段不全/类型不对时返回 None（视为无缓存）。"""
    if not isinstance(entry, dict):
        return None
    token = str(entry.get("machineToken") or "").strip()
    mtype = str(entry.get("machineType") or "").strip()
    code = str(entry.get("machineCode") or "").strip()
    if not (token and mtype and code):
        return None
    vm_info = entry.get("vm_info") if isinstance(entry.get("vm_info"), dict) else {}
    return {"machineToken": token, "machineType": mtype, "machineCode": code,
            "vm": bool(entry.get("vm")),
            "vm_info": vm_info,
            "source": str(entry.get("source") or MACHINE_IDENTITY_NATIVE)}


def _identity_entry_expired(entry, ttl, now):
    """按 TTL 判断条目是否过期（TTL=0 永不过期；cached_at 缺失视为过期）。"""
    if ttl <= 0:
        return False
    try:
        cached_at = float(entry.get("cached_at") or 0)
    except (TypeError, ValueError):
        return True
    return (now - cached_at) >= ttl


def _identity_cache_warn_corrupt(reason):
    """损坏/字段不全：WARN 一次（进程内），随后按"无缓存"重建。"""
    global _identity_cache_corrupt_warned
    if _identity_cache_corrupt_warned:
        return
    _identity_cache_corrupt_warned = True
    _identity_cache_log(
        "WARN", "machine identity cache unreadable (%s); regenerating" % reason)


def _read_identity_cache_doc():
    """读缓存文档；文件缺失返回空壳；损坏/结构异常返回空壳（并首次 WARN）。

    绝不抛异常——缓存损坏只降级为"无缓存"，不影响业务流程。
    """
    path = machine_identity_cache_path()
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except FileNotFoundError:
        return {"version": 1, "realm": {}}
    except Exception as exc:
        _identity_cache_warn_corrupt("%s: %s" % (type(exc).__name__, exc))
        return {"version": 1, "realm": {}}
    if not (isinstance(doc, dict) and isinstance(doc.get("realm"), dict)):
        _identity_cache_warn_corrupt("unexpected structure")
        return {"version": 1, "realm": {}}
    return doc


def _load_identity_cache(realm, now, allow_expired=False):
    """读落盘缓存；返回 ident dict 或 None（无 / 损坏 / 字段不全 / 过期）。"""
    entry = (_read_identity_cache_doc().get("realm") or {}).get(realm)
    if entry is None:
        return None
    ident = _identity_entry_to_ident(entry)
    if ident is None:
        _identity_cache_warn_corrupt("missing fields")
        return None
    if not allow_expired and _identity_entry_expired(entry, _identity_cache_ttl(), now):
        return None
    return ident


def _identity_cache_expired_entry_exists(realm, now):
    """落盘存在该 realm 的**有效条目**、但已按 TTL 过期（TTL 轮换路径专用）。

    用于区分"首次无缓存/损坏"与"TTL 到期"：只有后者才在重取前清组件种子
    （让"定期轮换"真的换出新身份，见 _purge_identity_seed）。
    """
    entry = (_read_identity_cache_doc().get("realm") or {}).get(realm)
    if entry is None or _identity_entry_to_ident(entry) is None:
        return False
    return _identity_entry_expired(entry, _identity_cache_ttl(), now)


def _write_identity_cache_doc(doc):
    """原子写缓存文档（临时文件 + os.replace、权限 0600）；失败静默返回 False。"""
    path = machine_identity_cache_path()
    tmp = path + ".tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        return True
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False


def _save_identity_cache(realm, ident, force=False):
    """把身份写入落盘缓存（读-改-写 + 锁 + 原子替换）。

    返回 None = 采纳新值（已写盘）；返回 dict = 以磁盘既有值为准（并发防御：
    另一个实例刚写入未过期的缓存，见设计 §4②"keeping cached"）。
    """
    global _identity_cache_diff_warned
    with _IDENTITY_CACHE_LOCK:
        doc = _read_identity_cache_doc()
        realms = doc.setdefault("realm", {})
        old_entry = realms.get(realm)
        old_ident = _identity_entry_to_ident(old_entry) if old_entry else None
        if (not force and old_ident is not None
                and not _identity_entry_expired(old_entry, _identity_cache_ttl(),
                                                time.time())
                and old_ident != ident):
            if not _identity_cache_diff_warned:
                _identity_cache_diff_warned = True
                _identity_cache_log(
                    "INFO",
                    "identity cache differs from fresh component output; keeping cached")
            return old_ident
        entry = dict(ident)
        entry["cached_at"] = time.time()
        realms[realm] = entry
        doc["version"] = 1
        _write_identity_cache_doc(doc)
        if force and old_ident is not None:
            _identity_cache_log(
                "WARN",
                "machine identity refreshed after upstream rejection (cache updated) — %s"
                % realm)
        return None


def _purge_identity_seed():
    """删除组件的身份种子 $HOME/.config/.locale_cfg；缺失即忽略、异常不外抛。

    组件输出由该随机种子决定（#18 实测：同种子下重复调用返回同一身份，
    删种子后必换新身份并写回新种子）。只有三条**显式换身份**路径才清它：
    ① force 自愈（否则拿到还是被拒的同一个身份，自愈空转）；②
    QD_MACHINE_IDENTITY_RESET（用户主动换）；③ TTL 到期后的那次调用
    （定期轮换——不换种子这个开关就是空的）。其它路径（缓存命中、组件
    失败回退、首次无缓存获取）绝不动，否则「重建不换」的缓存会失去意义。
    返回是否真的删除了文件；任何失败都静默（身份拿不到才是大事故）。
    """
    try:
        path = os.path.join(os.path.expanduser("~"), ".config", ".locale_cfg")
        if os.path.exists(path):
            os.remove(path)
            return True
    except Exception:
        pass
    return False


def _consume_identity_reset():
    """QD_MACHINE_IDENTITY_RESET=1：清空落盘缓存（进程内只消费一次）。

    直接删 accounts/machine_identity.json 同样有效（最直观，不必记变量名）。
    """
    global _identity_reset_done
    if _identity_reset_done:
        return
    if (os.environ.get("QD_MACHINE_IDENTITY_RESET") or "").strip().lower() \
            not in ("1", "true", "yes", "on"):
        return
    _identity_reset_done = True
    with _IDENTITY_CACHE_LOCK:
        try:
            os.remove(machine_identity_cache_path())
            _identity_cache_log(
                "INFO", "machine identity cache cleared (QD_MACHINE_IDENTITY_RESET=1)")
        except FileNotFoundError:
            pass
        except OSError as exc:
            _identity_cache_log(
                "WARN", "could not clear machine identity cache: %s" % exc)
    # 主动换身份必须连种子一起换：只清缓存会复刻出一个**完全相同**的身份
    # （#18：身份由种子决定），等于没换。
    _purge_identity_seed()


def _sample_identity(realm, account_id):
    """单次调用组件并构造 ident dict；组件无输出或字段不全时返回 {}。"""
    data = run_runtime_info(realm, account_id)
    if not data:
        return {}
    token = str(data.get("machineToken") or "").strip()
    mtype = str(data.get("machineType") or "").strip()
    code = str(data.get("machineCode") or "").strip()
    vm_info = data.get("vmInfo") if isinstance(data.get("vmInfo"), dict) else {}
    if not (token and mtype and code):
        return {}
    return {"machineToken": token, "machineType": mtype, "machineCode": code,
            "vm": bool(vm_info.get("isVm")),
            "vm_info": vm_info,
            "source": MACHINE_IDENTITY_NATIVE}


def _identity_vote_key(ident):
    """表决键：token + 三元组判别字段（type/code/isVm/brand/vmTypeCode）。

    刻意排除 vmInfo.percentage 这类连续噪声字段——它们波动不应破坏多数表决；
    写盘采用选中样本的**完整** vmInfo（三元组天然自洽、不逐字段杂交）。
    """
    vm = ident.get("vm_info") if isinstance(ident.get("vm_info"), dict) else {}
    try:
        vm_type_code = int(vm.get("vmTypeCode") or 0)
    except (TypeError, ValueError):
        vm_type_code = -1
    return (ident.get("machineToken"), ident.get("machineType"),
            ident.get("machineCode"), bool(vm.get("isVm")),
            str(vm.get("brand") or ""), vm_type_code)


def _identity_branch_label(ident):
    """提取样本的（品牌, vmTypeCode），用于投票日志。"""
    vm = ident.get("vm_info") if isinstance(ident.get("vm_info"), dict) else {}
    return str(vm.get("brand") or "unknown"), vm.get("vmTypeCode")


def _first_identity_with_vote(realm, account_id, rounds=IDENTITY_VOTE_ROUNDS,
                              extend_rounds=IDENTITY_VOTE_EXTEND_ROUNDS):
    """首次落盘前的**自适应**多数表决：先采样 rounds 次；全一致直接采纳，
    出现分歧则再补 extend_rounds 次（上限有界），取出现次数最多的**完整样本**。

    - 取完整样本 = machineType / machineCode / vmInfo 天然自洽（不逐字段拼）；
    - 唯一多数 → 采纳；平票 / 全分歧 → 取**首次出现的**样本（确定性），
      日志明确写出不一致与补投过程；
    - 全部采样都无输出（组件不可用）时返回 {}，交给既有失败回退路径；
    - 本函数绝不清种子（那是 force / RESET / TTL 轮换的换身份路径）。
    """
    def take(count):
        got = []
        for _ in range(max(0, int(count))):
            sample = _sample_identity(realm, account_id)
            if sample:
                got.append(sample)
        return got

    samples = take(max(1, int(rounds)))
    if not samples:
        return {}
    tally = {}
    for sample in samples:
        tally.setdefault(_identity_vote_key(sample), []).append(sample)
    if len(tally) == 1:
        # 首轮全一致：直接采纳（证据见常量区注释——最常见情形，成本不增加）
        picked = samples[0]
        brand, vm_type_code = _identity_branch_label(picked)
        _identity_cache_log(
            "INFO", "identity vote: %d/%d %s (vmTypeCode=%s)"
            % (len(samples), len(samples), brand, vm_type_code))
        return picked
    # 出现分歧：补投（上限 rounds+extend_rounds，保证首次延迟有界）
    samples.extend(take(extend_rounds))
    tally = {}
    for sample in samples:
        tally.setdefault(_identity_vote_key(sample), []).append(sample)
    top_key = max(tally, key=lambda key: len(tally[key]))
    top_count = len(tally[top_key])
    total = len(samples)
    unique_top = sum(1 for key in tally if len(tally[key]) == top_count) == 1
    first_brand, first_vtc = _identity_branch_label(samples[0])
    if unique_top:
        picked = tally[top_key][0]
        brand, vm_type_code = _identity_branch_label(picked)
        minority_names = "/".join(
            _identity_branch_label(tally[key][0])[0]
            for key in tally if key != top_key)
        summary = ("%d rounds disagreed; extended to %d -> %d/%d %s "
                   "(%d minority %s; vmTypeCode=%s)"
                   % (int(rounds), total, top_count, total, brand,
                      total - top_count, minority_names, vm_type_code))
    else:
        picked = samples[0]
        distribution = " / ".join(
            "%s %d" % (_identity_branch_label(tally[key][0])[0], len(tally[key]))
            for key in tally)
        summary = ("%d rounds disagreed; extended to %d -> no majority (%s); "
                   "using first sample (%s, vmTypeCode=%s)"
                   % (int(rounds), total, distribution, first_brand, first_vtc))
    _identity_cache_log("INFO", "identity vote: " + summary)
    return picked


def native_machine_identity(realm, account_id, force=False):
    """调用官方原生桥取真实机器身份；任何失败返回 {}（调用方回退派生值）。

    身份是**机器级**的（实测不同 account id 返回同一份），按区域短缓存
    NATIVE_IDENTITY_TTL 秒；force=True 跳过缓存重新取值（服务端明确拒绝时的
    自愈路径才用它——见设计 §9.1）。

    落盘缓存（issue #18）：组件输出由本机种子文件（$HOME/.config/.locale_cfg）
    决定——同一种子下重复调用返回**同一个**身份，删种子才换新（见
    _purge_identity_seed）。只有**缓存优先**（落盘命中就不调组件）才能让
    Docker 重建/升级容器后身份不变；组件不可用时回退落盘缓存（哪怕过期），
    不再被迫退化到 derived 假身份。

    另：**首次落盘**前对组件做自适应多数表决（先 IDENTITY_VOTE_ROUNDS 次，
    分歧时补至 IDENTITY_VOTE_ROUNDS + IDENTITY_VOTE_EXTEND_ROUNDS 次，取出现
    次数最多的完整样本）——组件的 VM 判定会抖到另一分支（实测 1.56%~10.29%），
    单次采样会把用户永久固定到少数派（QD_MACHINE_IDENTITY_VOTE=0 可关闭表决）。
    """
    now = time.time()
    cache_on = _identity_cache_enabled()
    ttl_rotation = False
    _consume_identity_reset()
    if not force:
        # ① 内存缓存（1800s，现状不变）
        hit = _native_ident_cache.get(realm)
        if hit and now - hit[0] < NATIVE_IDENTITY_TTL:
            return hit[1]
        # ② 落盘缓存：命中就**不调组件**（本设计的核心）
        if cache_on:
            stored = _load_identity_cache(realm, now)
            if stored is not None:
                _native_ident_cache[realm] = (now, stored)
                if realm not in _identity_cache_reuse_logged:
                    _identity_cache_reuse_logged.add(realm)
                    _identity_cache_log(
                        "INFO",
                        "machine identity cache: reusing cached identity for %s "
                        "(rebuilds keep the same device)" % realm)
                return stored
            # TTL 到期（有记录但已过期）＝"定期轮换"路径；与"首次无缓存"
            # 区分开：只有它才在重取前清种子，让轮换真的换出**新身份**。
            ttl_rotation = _identity_cache_expired_entry_exists(realm, now)
    # 清种子的三条路径（后来人最容易漏第三条）：
    #   ① force=True：服务端已不认当前身份 → 自愈必须换一个**新的**
    #      （同种子下组件只会返回同一个被拒身份，不换种子等于自愈空转）；
    #   ② QD_MACHINE_IDENTITY_RESET=1：用户主动换
    #      （在 _consume_identity_reset 里处理：缓存与种子一起清）；
    #   ③ TTL 到期后的这次调用：让"定期轮换"开关真的生效——不换种子它
    #      只是重新调一次组件、拿回同一个身份，等于空开关。
    # 不清种子的路径：缓存命中（根本不调组件）、组件失败回退缓存、首次无
    # 缓存的正常获取（那时用户并没有要求换身份）。
    if force or ttl_rotation:
        _purge_identity_seed()
    # ③ 调组件（首次落盘时做多数表决：组件的 VM 判定有 1~2% 概率抖到另一
    #    分支，单次采样会把用户永久固定到少数派。表决只在"首次无可用缓存"
    #    发生；force 自愈 / TTL 轮换 / 缓存关闭 保持单次调用不变。）
    if cache_on and not force and not ttl_rotation and _identity_vote_enabled():
        ident = _first_identity_with_vote(realm, account_id)
    else:
        ident = _sample_identity(realm, account_id)
    if ident:
        if cache_on:
            kept = _save_identity_cache(realm, ident, force=force)
            if kept is not None:
                ident = kept
        _native_ident_cache[realm] = (now, ident)
        return ident
    # ④ 组件失败：回退落盘缓存（哪怕已过期）——alpine 缺兼容层/组件被删时
    #    仍能用真身份，比 derived 好（设计 §3.2 第 3 行）。
    if cache_on:
        stored = _load_identity_cache(realm, now, allow_expired=True)
        if stored is not None:
            _native_ident_cache[realm] = (now, stored)
            return stored
    _native_ident_cache[realm] = (now, {})
    return {}

# 签到能力探测缓存：404（接口不存在）后 N 秒内不再重复探测，避免每次巡检都
# 打一个必然失败的请求；到期自动重探，官方上线即可自动恢复。
CHECKIN_PROBE_TTL = 6 * 3600
# 活动列表缓存 TTL：活动状态变化很慢（每日一轮），20 秒内复用可让看板切换视图
# /账号不再等那 1–4 秒的上游请求；领取动作会强制绕过并立即失效缓存。
CAMPAIGNS_TTL = 20

# 活动平台 claim 请求之间的默认间隔（秒）：调用方（qoder_tasks.run_checkin）的
# gap 未透传时的安全默认，保持「>= 1.0s 防风控」语义（见 qoder_tasks 模块声明）。
CLAIM_GAP_DEFAULT = 1.0

def campaign_label(c):
    """活动显示名：官方中文标题（placements content.zh.title）优先，其次 key。"""
    c = c or {}
    return str(c.get("title_zh") or "").strip() or         str(c.get("campaign_key") or c.get("campaign_id") or "")


# 活动领取失败码 -> 中文说明（与官方 growth-page/activity-iframe 前端一致）
_CAMPAIGN_FAILURE_CN = {
    "REDEMPTION_CODE_OUT_OF_STOCK": "今日名额已发完（每日 10:00 刷新，次日再来）",
    "ACHIEVEMENT_NOT_COMPLETED": "需先完成新人任务（成就未完成）",
    "CAMPAIGN_NOT_ACTIVE": "活动已结束/未开始",
    "RISK_BLOCKED": "风控拦截（当前设备/账号不可领取）",
    "RISK_DEPENDENCY_UNAVAILABLE": "风控服务不可用，稍后重试",
}
CHECKIN_REASON_NOT_FOUND = "checkin_endpoint_not_found"

# 会话死亡标记：上游主动吊销离线会话，刷新已无意义，需要重新登录。
SESSION_DEAD_MARKERS = ("TOKEN_EXPIRE", "12153", "Offline user session not found")


def session_dead(msg):
    s = str(msg or "")
    return any(m in s for m in SESSION_DEAD_MARKERS)


def get_realm_config(realm):
    return REALM_CONFIGS.get(realm) or REALM_CONFIGS["cn"]


def gateway_candidates(realm):
    """该区域的推理主机候选列表（官方客户端同款：主选 + 故障切换域名）。

    签名只覆盖 path，因此同一请求换主机后签名依旧有效。
    """
    cfg = get_realm_config(realm)
    out = [cfg["gateway"]]
    for host in cfg.get("gateway_fallbacks") or ():
        if host and host not in out:
            out.append(host)
    return out


def detect_realm_from_domain(domain):
    d = str(domain or "").lower()
    if "qoder.sh" in d or (d.endswith("qoder.com") and "qoder.com.cn" not in d) \
            or "qoder.com/" in d:
        return "intl"
    return "cn"


def normalize_epoch(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0
    if number > 1e11:      # 毫秒
        number /= 1000.0
    return int(number)


def effective_remain(credits):
    """有效余额 = 套餐 remain + 可用的共享池 remain（issue #39）。

    团队账号「套餐已用满（remain=0）+ 共享池仍有余额」时，只看 remain 会把
    可用账号误判成余额耗尽（护栏停号、402 不解除、汇总显示 0）——所以额度
    判定与展示一律走本函数。快照里没有 remain_effective 的（旧数据 / 非 dict）
    回退 remain，与本字段引入前的行为完全一致。
    """
    if not isinstance(credits, dict):
        return None
    value = credits.get("remain_effective")
    return credits.get("remain") if value is None else value


def _truthy(value, default=False):
    """上游布尔字段的防御性解读：字符串 "false" / "0" / "no" 也算假。

    `bool("false")` 是 True——共享池的 available 若从 JSON 布尔漂移成字符串，
    就会把「不可用」当成「可用」，凭空多算额度（issue #39 的字段形态）。
    value 为 None（显式 null）时取 default：**缺键语义必须由调用方决定**，
    因为「没这个字段」和「字段是 null」在额度语义上并不等价。
    """
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() not in (
            "", "0", "false", "no", "off", "none", "null")
    return bool(value)


# ---------------------------------------------------------------------------
# 每日签到的「下次可签到时间」
# ---------------------------------------------------------------------------
# 官方规则：每日 10:00（UTC+8）刷新，错过不补。因此这条时间**必须**按固定
# UTC+8 计算与呈现，**不能**用系统本地时区——容器里常是 UTC，若按本地渲染，
# 用户看到的「下次」会和官方说明差 8 小时，对不上。
CHECKIN_WINDOW_HOUR_UTC8 = 10
_UTC8 = datetime.timezone(datetime.timedelta(hours=8))


def next_checkin_window(now=None):
    """下一个「每日 10:00（UTC+8）」窗口 → (epoch 秒:int, 人类可读:str)。

    边界（与 issue #20 的口径一致）：
      · 今天 10:00 **之前**（now < 当日 10:00）→ 今天 10:00；
      · 到达/晚于 10:00（含刚领取成功的情形）→ 明天 10:00。
      取「到达即算下一轮」是为了不返回一个已经到点的时刻。
    note 形如 "10-05 10:00（UTC+8）"，始终以 UTC+8 呈现；两个返回值同源，
    调用方只需调一次即可拿到成对的字段，避免两处各算一遍导致不一致。
    """
    ts = time.time() if now is None else float(now)
    now8 = datetime.datetime.fromtimestamp(ts, _UTC8)
    boundary = now8.replace(hour=CHECKIN_WINDOW_HOUR_UTC8, minute=0,
                            second=0, microsecond=0)
    if now8 >= boundary:
        boundary += datetime.timedelta(days=1)
    return int(boundary.timestamp()), boundary.strftime("%m-%d %H:%M") + "（UTC+8）"


# ---------------------------------------------------------------------------
# 账号级预算护栏（task-61）：常量 + 当日用量折叠
# ---------------------------------------------------------------------------
# 设计来源：.team/_gap/03-candidate-deep-dive.md（wb 六个守卫的等价移植）。
# 四条护栏 G1 余额地板 / G2 当日 token / G3 当日 credit（free 豁免）/
# G4 单模型当日 token —— 全部**纯内存读**（R1，ready() 在选号热路径上），
# 阈值默认 0 = 关闭（R4：量纲未核对，上线由使用方显式开）。
_QUOTA_LOCK = threading.Lock()          # R6：限额字段专用锁（不是 _SAVE_LOCK）

# ---------------------------------------------------------------------------
# 账号级失败治理（task-74）：软限流退避 / 硬错误熔断 / 未知错误降权
# ---------------------------------------------------------------------------
# 移植自 wb_accounts.py:376-411（三族 + 常量）。**不另起冷却系统**：
#   · note_soft_rate 复用 cooldown_until（与普通冷却同字段、同判定路径）；
#   · note_failure / note_unknown_failure 各占一个新窗口字段，但在
#     ready() / throttle_wait() 里与既有冷却**同一处**取用（不是并行判定）；
#   · 全部状态读写走同一把 _QUOTA_LOCK（与护栏同锁；不用 _SAVE_LOCK 文件锁）。
# 复位路径（每族）见各方法 docstring；统一入口 Account.note_success()。
SOFT_RATE_BASE = 600.0          # 账号级 429 首次退避：10 分钟
SOFT_RATE_MAX = 7200.0          # …按 2 的幂翻倍，封顶 2 小时
BREAKER_THRESHOLD = 3           # 连续硬失败达到该次数才起跳熔断
BREAKER_COOLDOWN = 1800.0       # 首次熔断窗口：30 分钟
BREAKER_COOLDOWN_MAX = 21600.0  # …封顶 6 小时
DEGRADE_THRESHOLD = 5           # 连续未知失败达到该次数才降权
DEGRADE_COOLDOWN = 600.0        # 首次降权窗口：10 分钟
DEGRADE_COOLDOWN_MAX = 7200.0   # …封顶 2 小时


def _exponential_backoff(count, base, cap, offset):
    """base * 2 ** (count - offset)，封顶 cap、最多放大 20 步（wb:391 同式）。"""
    step = max(0, int(count) - int(offset))
    return min(float(base) * (2 ** min(step, 20)), float(cap))


def soft_backoff(streak):
    """连续 streak 次软限流对应的退避秒数。"""
    return _exponential_backoff(streak, SOFT_RATE_BASE, SOFT_RATE_MAX, 1)


def breaker_backoff(fails):
    """连续 fails 次硬失败对应的熔断窗口秒数。"""
    return _exponential_backoff(fails, BREAKER_COOLDOWN, BREAKER_COOLDOWN_MAX,
                                BREAKER_THRESHOLD)


def degrade_backoff(fails):
    """连续 fails 次未知失败对应的降权窗口秒数。"""
    return _exponential_backoff(fails, DEGRADE_COOLDOWN, DEGRADE_COOLDOWN_MAX,
                                DEGRADE_THRESHOLD)


def _scope_limit_value(scope, realm):
    """三级作用域取值：realm override > global > 0（与 qoder_settings.limit_value 同语义）。

    也接受扁平数值，便于调用方直接给标量。非法/负数一律归 0（= 关闭该护栏）。
    """
    val = scope
    if isinstance(scope, dict):
        val = scope.get(realm)
        if val is None:
            val = scope.get("global")
    if val is None:
        return 0
    try:
        return max(0, int(val))
    except (TypeError, ValueError):
        return 0
DEFAULT_CREDITS_REFRESH_HOURS = 12.0    # §2.5：与 wb 同默认
_QUOTA_REFRESHER_INTERVAL = 1800        # 每 tick 只刷一个账号 -> 每天 ≤48 次计费调用
_QUOTA_REFRESHER_PARK = 6 * 3600        # 刷新失败的账号停放 6h（§2.5）

# issue #43：**使用驱动**的余额刷新（与额度护栏解耦）。
# 护栏刷新器只在「某条护栏阈值 > 0」时惰性启动、且 30min/tick + 12h TTL，
# 阈值全 0（默认）时根本没有线程——持续使用两三个小时面板余额也不会变。
# 这里只刷新**本窗口内真正用过**的账号，默认 5 分钟合并一次、空闲不轮询。
_CREDITS_USAGE_INTERVAL = int(os.environ.get("QD_CREDITS_USAGE_INTERVAL") or 300)
_CREDITS_USAGE_BACKOFF_BASE = 60        # 失败退避：60s 起
_CREDITS_USAGE_BACKOFF_MAX = 900        # 上限 15min
_CREDITS_USAGE_MAX_PER_TICK = 8         # 单 tick 最多查几个号（限制上游调用）


# 测试/CLI 可显式指定 usage.jsonl（优先于环境变量）：注入夹具用。
# 生产链路（CreditsRefresher -> refresh_daily_usage()）走 QD_PROXY_USAGE_DIR，
# 与 qoder_proxy.USAGE_DIR 同源；唯一例外是 proxy 的 --usage-dir（只改 proxy 侧）。
USAGE_LOG_OVERRIDE = None
# 9999-12-31T00:00:00Z：官方给 CN 号的「永不过期」占位值（质检 E4 实测）——
# 临期权重与 soonest_expiring_days 必须跳过它，否则 CN 号会被算成"很久以后到期"。
_EXPIRY_NEVER_EPOCH = 253402214400


def usage_log_path():
    """usage.jsonl 的路径。

    优先级：USAGE_LOG_OVERRIDE（显式注入，测试/CLI）> QD_PROXY_USAGE_DIR
    > 本文件目录下的 usage/（与 qoder_proxy 的默认值一致）。
    """
    if USAGE_LOG_OVERRIDE:
        return str(USAGE_LOG_OVERRIDE)
    d = os.environ.get("QD_PROXY_USAGE_DIR") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "usage")
    return os.path.join(d, "usage.jsonl")


def fold_daily_usage(log_path=None, day=None, max_lines=200000):
    """折叠「当日、按 uid」的 token / credit（只读本地 usage.jsonl，**不联网**）。

    返回 {"day", "tokens"{uid:int}, "credits"{uid:float},
          "model_tokens"{uid:{model:int}}, "rows", "skipped"}。
    · 跨日自动归零：只统计 iso 日期 == day 的行（新的一天 -> 新桶）。
    · 文件缺失/不可读 -> 空桶（不是 None）；调用方据此把「有账号但今日无记录」
      折叠为 0（D4：默认 0 而不是 None，避免当天没流量的号永远 fail-open）。
    · max_lines 只保留文件末尾 N 行（大日志保护）。
    """
    day = day or time.strftime("%Y-%m-%d")
    out = {"day": day, "tokens": {}, "credits": {}, "model_tokens": {},
           "rows": 0, "skipped": 0}
    path = log_path or usage_log_path()
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            keep = int(max_lines) if max_lines and int(max_lines) > 0 else None
            tail = collections.deque(fh, maxlen=keep)
    except Exception:
        return out
    for line in tail:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except Exception:
            out["skipped"] += 1
            continue
        if not isinstance(row, dict) or row.get("error"):
            continue
        if str(row.get("iso") or "")[:10] != day:
            continue
        uid = str(row.get("account") or "")
        try:
            tokens = int(row.get("total_tokens") or 0)
        except (TypeError, ValueError):
            tokens = 0
        try:
            credit = float(row.get("credit") or 0)
        except (TypeError, ValueError):
            credit = 0.0
        out["rows"] += 1
        if not uid:
            continue
        out["tokens"][uid] = out["tokens"].get(uid, 0) + tokens
        out["credits"][uid] = round(out["credits"].get(uid, 0.0) + credit, 6)
        model = str(row.get("model") or "")
        if model:
            per = out["model_tokens"].setdefault(uid, {})
            per[model] = per.get(model, 0) + tokens
    return out


# ---------------------------------------------------------------------------
# 带重试的 HTTP JSON 工具
# ---------------------------------------------------------------------------
# 198.18.0.0/15 (RFC 2544 benchmarking) 与 fdfe:dcba:9876::/48 被 Clash/mihomo
# 等本地代理用作 fake-IP DNS 段：开启透明代理的机器上所有公网域名都会解析到
# 这些网段。命中它说明 DNS 已被本机代理接管、真实 IP 不可见，此时跳过解析级
# 校验（名称级校验已完成）。
_FAKEIP_NETS = [
    ipaddress.ip_network("198.18.0.0/15"),
    ipaddress.ip_network("2001:2::/48"),
    ipaddress.ip_network("fdfe:dcba:9876::/48"),
]


def _host_boundary_violation(ip, allow_local):
    """True when the resolved/IP address must be refused."""
    if ip.is_loopback:
        return not allow_local
    if (ip.is_private or ip.is_reserved or ip.is_link_local
            or ip.is_multicast or ip.is_unspecified):
        return True
    return False


def validate_public_http_url(url, allow_local=False):
    """SSRF 防护：仅允许 http/https，且 host 不得指向本机/私有/保留网段。

    上游网关与 openapi 域名均为公网地址；任何指向 localhost、回环、内网或
    保留地址的 URL 一律拒绝，防止上游配置或导入数据把请求引向内网。
    allow_local 仅供显式面向本机网关的开发/验证脚本开启（如 _verify_models.py），
    服务端请求路径一律使用默认 False。
    """
    parsed = urllib.parse.urlsplit(str(url or ""))
    if parsed.scheme not in ("http", "https"):
        raise ValueError("only http/https URLs are allowed")
    host = (parsed.hostname or "").strip().strip("[]").lower()
    if not host:
        raise ValueError("URL host is required")
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        if not allow_local:
            raise ValueError("requests to localhost are not allowed")
        return url
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if _host_boundary_violation(literal, allow_local):
            raise ValueError(
                "requests to private/reserved address %s are not allowed" % literal)
        return url
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError("cannot resolve URL host %r: %s" % (host, exc))
    addrs = []
    for info in infos:
        addr = str(info[4][0]).strip("[]")
        try:
            addrs.append(ipaddress.ip_address(addr))
        except ValueError:
            raise ValueError("URL host resolved to a non-IP address: %r" % addr)
    if addrs and all(any(ip in net for net in _FAKEIP_NETS) for ip in addrs):
        return url  # fake-IP DNS：真实 IP 不可见，名称级校验已通过
    for ip in addrs:
        if _host_boundary_violation(ip, allow_local):
            raise ValueError(
                "requests to private/reserved address %s are not allowed" % ip)
    return url


def _retryable(exc):
    """Transient network faults worth another attempt (TLS resets, timeouts, 5xx)."""
    if isinstance(exc, ssl.SSLError):
        return True
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code >= 500
    if isinstance(exc, urllib.error.URLError):
        return True
    if isinstance(exc, (TimeoutError, ConnectionResetError, ConnectionAbortedError, OSError)):
        return True
    return False


# ---------------------------------------------------------------------------
# 出站代理策略（#27）：**只看环境变量，忽略 Windows 注册表；每请求现读、不固化**。
# ---------------------------------------------------------------------------
# 背景（Windows 实测根因）：urllib 的 getproxies() 在环境变量缺失时会回退读
# WinINET 注册表（Internet Settings 的 ProxyEnable/ProxyServer）；而**默认
# opener 在进程首次请求时就固化了当时的代理配置**。后果：网关启动时系统代理
# 开着（如 Clash 127.0.0.1:7897），之后用户关掉代理工具 -> 长驻进程仍去连那个
# 已无人监听的端口（WinError 10061），token 刷新与全部模型调用全挂到重启。
#
# 本仓策略（一句话）：显式设置了 HTTPS_PROXY / HTTP_PROXY / ALL_PROXY（任一
# 大小写）-> 走该代理；**没设 -> 直连**（不回退注册表）。每请求现读环境变量、
# 现建 opener——没有「首次请求固化」，用户中途开关代理立即生效。
#
# 依赖系统代理才能上网的用户：设 HTTPS_PROXY=http://127.0.0.1:7897 即可恢复
# （README「系统代理（Windows）」节有完整说明）；TUN / fake-ip 型代理（Clash
# 等）在网络层透明转发，无需任何设置。
_PROXY_SCHEMES = (("https", ("HTTPS_PROXY", "https_proxy")),
                  ("http", ("HTTP_PROXY", "http_proxy")),
                  ("all", ("ALL_PROXY", "all_proxy")))


def _proxy_map_from_env():
    """只从环境变量收集代理映射（http/https；ALL_PROXY 落到两者）。

    刻意**不调用** urllib.getproxies()——它在 Windows 上会回退读注册表，
    那正是 #27 的根因路径。没有环境变量 -> {}（直连）。
    """
    out = {}
    for scheme, keys in _PROXY_SCHEMES:
        for key in keys:
            value = (os.environ.get(key) or "").strip()
            if value:
                out[scheme] = value
                break
    all_proxy = out.pop("all", "")
    if all_proxy:
        out.setdefault("https", all_proxy)
        out.setdefault("http", all_proxy)
    return out


def _opener_for_request():
    """每请求现建 opener：代理 = 当前环境变量（空 = 直连）。

    「现建」是对 #27 固化行为的修复——默认 opener（urllib.request.urlopen）
    首次使用时缓存代理配置；本函数让每次请求看到的环境变化立即生效。
    """
    proxies = _proxy_map_from_env()
    return urllib.request.build_opener(urllib.request.ProxyHandler(proxies))


def http_json(url, data=None, method=None, headers=None, timeout=30,
              retries=3, backoff=1.0, log=None):
    """urlopen + json decode with retries. 所有 openapi 调用统一走这里。"""
    validate_public_http_url(url)
    attempts = max(1, int(retries or 1))
    last = None
    for attempt in range(1, attempts + 1):
        req = urllib.request.Request(
            url,
            data=data,
            method=method or ("POST" if data is not None else "GET"),
            headers=headers or {},
        )
        try:
            with _opener_for_request().open(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            last = exc
            if attempt >= attempts or not _retryable(exc):
                break
            if log:
                log("network retry %d/%d after %s" % (attempt, attempts, exc))
            time.sleep(backoff * attempt)
    raise last


# ---------------------------------------------------------------------------
# Account
# ---------------------------------------------------------------------------
class Account(object):
    def __init__(self, data, path=None):
        data = data or {}
        self.path = path
        self.uid = str(data.get("uid") or "")
        self.nickname = str(data.get("nickname") or "")
        self.domain = str(data.get("domain") or "")
        self.realm = str(data.get("realm") or detect_realm_from_domain(self.domain))
        if self.realm not in REALM_CONFIGS:
            self.realm = "cn"
        if not self.domain:
            self.domain = get_realm_config(self.realm)["domain"]
        self.platform = str(data.get("platform") or "CLI")
        self.access_token = str(data.get("accessToken") or "")
        self.refresh_token = str(data.get("refreshToken") or "")
        self.personal_token = str(data.get("personalToken") or "")
        self.expires_at = normalize_epoch(data.get("expiresAt"))
        self.added_at = data.get("addedAt") or time.time()
        self.source = str(data.get("source") or "oauth")
        self.enabled = data.get("enabled", True)
        self.last_error = str(data.get("lastError") or "")
        self.cooldown_until = float(data.get("cooldownUntil") or 0)
        # 按模型粒度的限流冷却：上游频控只针对单模型，不能拖垮整个账号。
        self.model_cooldowns = {}
        self.credits = data.get("credits") or None
        # #25-d：最近一次「不可信 quota 响应」的记录（含截断 raw 摘要）——
        # 供事后区分「上游真说超额」与「解析失败」（报告者最贵的教训）。
        self.last_quota_anomaly = data.get("lastQuotaAnomaly") or None
        # issue #43：使用驱动的余额刷新状态（**transient，不落盘**）——
        # 面板据此显示「待刷新 / 查询中 / 失败 + 最后成功时间」。
        self.credits_pending = False
        self.credits_refresh_state = "idle"     # idle|pending|refreshing|failed
        self.credits_refresh_error = ""
        self.credits_refresh_failures = 0
        self.credits_refresh_next = 0.0
        # issue #40 后续：最近一次「上游返回全 0 退化体」的降级标记（面板显示 0*）
        self.credits_degraded = data.get("creditsDegraded") or None
        # ---- task-61：账号级预算护栏状态（默认全关；计数 None = 未知 -> fail-open）----
        self.reserve_credits = 0            # G1 余额地板（<=0 关）
        self.daily_token_limit = 0          # G2 当日 token 总额（<=0 关）
        self.daily_tokens_today = None      # None = 未折叠 -> 放行
        self.daily_credit_limit = 0         # G3 当日 credit 总额（<=0 关）
        self.daily_credits_today = None
        self.model_daily_token_limit = 0    # G4 单模型当日 token（<=0 关）
        self.model_daily_tokens = {}        # {model: tokens}
        self.free_models = frozenset()      # 目录 is_free 名单（与守卫同批推送，R3）
        self.expiring_window_days = 0       # R7：即将过期窗口（天），默认 0 = 关
        self.balance_until = 0.0            # 402 停放（时钟 = UTC+8 每日 10:00，R5）
        self.balance_reason = ""
        # task-74 失败治理三族（运行期状态，重启重置）：
        self.soft_streak = 0                # 连续软限流（429）计数，喂 cooldown_until
        self.fails = 0                      # 连续硬失败计数（喂熔断）
        self.degrade_count = 0              # 连续未知失败计数
        self.breaker_until = 0.0            # 硬错误熔断窗口截止（ready() 会挡）
        self.degrade_until = 0.0            # 未知错误降权窗口截止（不挡 ready）
        self.plan = str(data.get("plan") or "")
        self.last_checkin = data.get("lastCheckin") or None
        # 已领取活动的兑换码（如「奶茶免单卡」REDEMPTION_CODE）：活动只发一次，
        # 必须落盘持久化，否则网关重启后用户就找不回兑换码了。
        self.campaign_codes = dict(data.get("campaignCodes") or {})
        # 被"同人已领取"挡下的活动 -> 冷却截止时间戳。服务端按"人"去重（同机器/
        # 同身份多账号共用一张），被挡后反复重试没有意义，只会刷日志。
        self.campaign_blocked_until = dict(data.get("campaignBlockedUntil") or {})
        self.user_type = str(data.get("userType") or "") or DEFAULT_USER_TYPE
        self.organization_id = str(data.get("organizationId") or "")
        self.organization_name = str(data.get("organizationName") or "")
        # 签到能力：None=未探测 / True=接口存在 / False=接口不存在（404）。
        # 运行时探测而非按区域硬编码——官方随时可能在任一区域增删活动接口。
        self._checkin_cap = None
        self._checkin_cap_reason = ""
        self._checkin_cap_at = 0.0
        # 最近一次 campaign 平台状态快照（/sash/api/v1/me/campaigns）
        self.campaign_status = None
        # 活动列表短缓存 (at, payload)：该请求约 1–4 秒（上游最慢的一环），
        # 看板切换视图/账号会连续取，缓存后由"领取动作"显式失效。
        self._campaigns_cache = None
        # 活动平台用的机器身份来源：runtime-info(官方原生桥) / derived(派生回退)
        self.machine_identity_source = "derived"
        # 最近一次 desktop_headers() 实际是否携带 cosy-machine* 头：
        # native / omitted（未构造过时按 omitted 保守处理）；与身份来源正交
        self.machine_headers_state = MACHINE_HEADERS_OMITTED

    # -- 持久化 ------------------------------------------------------------
    def to_dict(self):
        return {
            "uid": self.uid,
            "nickname": self.nickname,
            "domain": self.domain,
            "realm": self.realm,
            "platform": self.platform,
            "accessToken": self.access_token,
            "refreshToken": self.refresh_token,
            "personalToken": self.personal_token,
            "expiresAt": self.expires_at,
            "addedAt": self.added_at,
            "source": self.source,
            "enabled": self.enabled,
            "lastError": self.last_error,
            "cooldownUntil": self.cooldown_until,
            "credits": self.credits,
            "lastQuotaAnomaly": self.last_quota_anomaly,
            "creditsDegraded": self.credits_degraded,
            "plan": self.plan,
            "lastCheckin": self.last_checkin,
            "campaignCodes": self.campaign_codes,
            "campaignBlockedUntil": self.campaign_blocked_until,
            "userType": self.user_type,
            "organizationId": self.organization_id,
            "organizationName": self.organization_name,
        }

    def public(self):
        exp = self.expires_at
        return {
            "uid": self.uid,
            "nickname": self.nickname or (self.uid[:8] if self.uid else "?"),
            "domain": self.domain,
            "realm": self.realm,
            "platform": self.platform,
            "enabled": bool(self.enabled),
            "source": self.source,
            "tokenFamily": token_family(self),
            "expiresAt": exp,
            "expiresIn": _human_delta(exp - time.time()) if exp else None,
            "hasRefreshToken": bool(self.refresh_token),
            "hasPAT": bool(self.personal_token),
            "lastError": self.last_error,
            "inCooldown": self.cooldown_until > time.time(),
            "cooldownFor": round(max(0.0, self.cooldown_until - time.time())) or None,
            "addedAt": self.added_at,
            "file": os.path.basename(self.path) if self.path else None,
            "credits": self.credits,
            # issue #43：余额新鲜度（面板显示「最后成功查询时间 + 刷新状态」）
            "creditsUpdatedAt": (self.credits.get("updated_at")
                                 if isinstance(self.credits, dict) else None),
            "creditsUpdatedIso": (self.credits.get("updated_iso")
                                  if isinstance(self.credits, dict) else None),
            "creditsRefreshState": self.credits_refresh_state,
            "creditsRefreshError": self.credits_refresh_error,
            "creditsPending": bool(self.credits_pending),
            "creditsDegraded": self.credits_degraded,
            # task-61 护栏状态（面板三态：没开 / 开了但未知 / 已超额）
            "reserveCredits": self.reserve_credits,
            "reserveBlocked": self.reserve_blocked(),
            "dailyTokenLimit": self.daily_token_limit,
            "dailyTokensToday": self.daily_tokens_today,
            "dailyLimitBlocked": self.daily_limit_blocked(),
            "dailyCreditLimit": self.daily_credit_limit,
            "dailyCreditsToday": self.daily_credits_today,
            "creditLimitReached": self.credit_limit_reached(),
            "modelTokenBlocked": sorted(self.blocked_model_names()),
            "freeModelCount": len(self.free_models or ()),
            "expiringWindowDays": self.expiring_window_days,
            "expiringSoonDays": self.soonest_expiring_days(),
            "inExpiringWindow": self.in_expiring_window(),
            "balanceParked": self.balance_until > time.time(),
            "balanceUntil": self.balance_until or None,
            "softStreak": int(self.soft_streak),
            "breakerFor": round(max(0.0, self.breaker_until - time.time())) or None,
            "degradeFor": round(max(0.0, self.degrade_until - time.time())) or None,
            "plan": self.plan,
            "lastCheckin": self.last_checkin,
            # 运行时探测：None=未探测（照常尝试）/ True / False（本区域无接口）
            "canCheckin": self.can_checkin(),
            "checkinCapability": ("unknown" if self.checkin_capability()[0] is None
                                  else ("available" if self.checkin_capability()[0]
                                        else "not_found")),
            "checkinReason": self.checkin_capability()[1],
            "userType": self.user_type,
            "machineId": derive_id(self.uid, "machine"),
            "sessionId": derive_id(self.uid, "session"),
        }

    def save(self, directory):
        base = Path(directory).resolve()
        base.mkdir(parents=True, exist_ok=True)
        safe_uid = re.sub(r"[^A-Za-z0-9_-]", "_", str(self.uid or "")).strip("_ ")
        name = (safe_uid or uuid.uuid4().hex) + ".json"
        path = base / name
        tmp = base / (name + ".tmp")
        if not (path.is_relative_to(base) and tmp.is_relative_to(base)):
            raise ValueError("invalid path for account save")
        # 并发保护：/tasks 面板接口会并行刷新同一账号的多个字段（credits/plan/…），
        # 每个都可能触发 save；同一 tmp 路径被两个线程同时写会落出坏 JSON。
        with _SAVE_LOCK:
            tmp.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
                           encoding="utf-8")
            os.replace(tmp, path)
        self.path = str(path)
        return self.path
    def delete(self):
        if self.path and os.path.exists(self.path):
            os.remove(self.path)

    # -- 健康与冷却 --------------------------------------------------------
    def ready(self, model=None):
        if not self.enabled or not self.access_token:
            return False
        if self.cooldown_until > time.time():
            return False
        if model and self.model_cooldowns.get(model, 0.0) > time.time():
            return False
        # ---- task-61：账号级预算护栏（纯内存读，无网络分支；R1）----
        if self.balance_until > time.time():       # 402 停放（UTC+8 10:00 解封）
            return False
        if self.breaker_until > time.time():       # task-74 硬错误熔断窗口（自动恢复）
            return False
        if self.reserve_blocked():                 # G1 余额地板
            return False
        if self.daily_limit_blocked():             # G2 当日 token 总额
            return False
        if self.credit_limit_blocked(model):       # G3 当日 credit（free 豁免）
            return False
        if self.model_token_limit_blocked(model):  # G4 单模型当日 token
            return False
        # ---- 护栏结束（下面才是 token 过期 -> refresh 的最后手段）----
        exp = self.expires_at
        if not exp:
            return True
        remaining = exp - time.time()
        if remaining > 240:          # 剩余 >4 分钟直接用（jt- 24h / dt- 30d）
            return True
        if remaining > 0:
            self.refresh()
            return True
        return self.refresh()

    # -- 账号级预算护栏（task-61，全部纯内存读）------------------------------
    def reserve_blocked(self):
        """G1 余额地板：remain <= reserve 即停（含等号）。三道短路 -> 放行。"""
        reserve = self.reserve_credits
        if not reserve or reserve <= 0:
            return False
        cred = self.credits
        if not isinstance(cred, dict):
            return False
        # issue #39：团队账号「套餐已用满 + 共享池仍有余额」按有效余额判定，
        # 否则共享额度白有、账号被误停。
        remain = effective_remain(cred)
        if remain is None:
            return False
        try:
            return int(float(remain)) <= int(reserve)
        except (TypeError, ValueError):
            return False

    def daily_limit_blocked(self):
        """G2 当日 token 总额。计数 None = 未折叠 -> fail-open（R2）。"""
        limit = self.daily_token_limit
        if not limit or limit <= 0:
            return False
        used = self.daily_tokens_today
        if used is None:
            return False
        try:
            return int(used) >= int(limit)
        except (TypeError, ValueError):
            return False

    def model_is_free(self, model):
        """目录 is_free 名单命中（空 model 恒 False）。"""
        return bool(model) and model in (self.free_models or ())

    def credit_limit_reached(self):
        """G3a 当日 credit 总额（账号级事实，与模型无关）。"""
        limit = self.daily_credit_limit
        if not limit or limit <= 0:
            return False
        used = self.daily_credits_today
        if used is None:
            return False
        try:
            return float(used) >= float(limit)
        except (TypeError, ValueError):
            return False

    def credit_limit_blocked(self, model=None):
        """G3 当日 credit 的模型感知包装：额度爆了免费模型照跑（唯一反向条件）。"""
        if not model or self.model_is_free(model):
            return False
        return self.credit_limit_reached()

    def model_token_limit_blocked(self, model=None):
        """G4 单模型当日 token 额。"""
        limit = self.model_daily_token_limit
        if not limit or limit <= 0 or not model:
            return False
        per = self.model_daily_tokens
        if not isinstance(per, dict):
            return False
        used = per.get(model)
        if used is None:
            return False
        try:
            return int(used) >= int(limit)
        except (TypeError, ValueError):
            return False

    def blocked_model_names(self):
        """当前超额的模型名集合（只服务日志：状态翻转才打印）。"""
        limit = self.model_daily_token_limit
        if not limit or limit <= 0 \
                or not isinstance(self.model_daily_tokens, dict):
            return set()
        out = set()
        for name, used in self.model_daily_tokens.items():
            try:
                if int(used) >= int(limit):
                    out.add(name)
            except (TypeError, ValueError):
                continue
        return out

    def soonest_expiring_days(self, now=None):
        """「还有余额、最早到期」的积分包剩余天数；无法判断返回 None。

        数据源：self.credits["packages"]（fetch_credits 写入）。每包支持：
          · `days_left`（数值，直接给，优先）；
          · `expires_at` / `expireAt`（epoch，现算 (exp-now)/86400）。
        跳过：no_expiry / package_code == "enterprise"（企业额度是重置不是作废）/
        is_expired；只接受 days_left >= 0 且 remain > 0 的包。
        未知数据永不构成偏好（R7：expiring_window_days 默认 0 = 关闭）。

        S1 实测（2026-10）：上游 /quota/usage 的 userQuota / addOnQuota 内部
        **没有**任何到期字段——真正的到期只有响应**顶层** expiresAt（已原样存
        在 credits["expires_at"]；其归属未经证实，故不映射到包级：误贴会把
        「按月重置」的额度算成临期、反向消耗）。因此在真实数据上本函数恒返回
        None = 偏好不触发（安全侧）；fetch_credits 已对未来的包级到期字段做
        透传，上游一补就自动生效（回归守卫见 tests/_test_credit_expiry.py）。
        """
        cred = self.credits
        if not isinstance(cred, dict):
            return None
        packages = cred.get("packages")
        if not isinstance(packages, list):
            return None
        now = now if now is not None else time.time()
        best = None
        for pkg in packages:
            if not isinstance(pkg, dict):
                continue
            if pkg.get("no_expiry"):
                continue
            if str(pkg.get("package_code") or "").lower() == "enterprise":
                continue
            if pkg.get("is_expired"):
                continue
            try:
                remain = float(pkg.get("remain") or 0)
            except (TypeError, ValueError):
                remain = 0.0
            if remain <= 0:
                continue
            days = pkg.get("days_left")
            if days is None:
                exp = pkg.get("expires_at")
                if exp is None:
                    exp = pkg.get("expireAt")
                if exp is None:
                    continue                    # 该包无法判断 -> 不参与
                try:
                    exp_f = float(exp)
                except (TypeError, ValueError):
                    continue
                if exp_f >= _EXPIRY_NEVER_EPOCH:
                    continue        # 永不过期占位（CN 号 253402214400）-> 不参与
                days = (exp_f - now) / 86400.0
            try:
                days = float(days)
            except (TypeError, ValueError):
                continue
            if days < 0:
                continue
            if best is None or days < best:
                best = days
        return best

    def in_expiring_window(self, now=None):
        """是否落在「即将过期」窗口内（window <= 0 直接 False 短路）。"""
        window = self.expiring_window_days
        if not window or window <= 0:
            return False
        soonest = self.soonest_expiring_days(now=now)
        return soonest is not None and soonest <= float(window)

    def quota_blocked(self, model=None):
        """四条护栏的汇总：返回命中的那条（""=未命中）。面板用。"""
        if self.reserve_blocked():
            return "reserve"
        if self.daily_limit_blocked():
            return "daily_tokens"
        if self.credit_limit_blocked(model):
            return "daily_credits"
        if self.model_token_limit_blocked(model):
            return "model_tokens"
        return ""

    def note_balance_cooled(self, detail="", until=None):
        """402 停放：停到**本仓的额度重置窗口**（UTC+8 每日 10:00，R5）。

        与 G1 是两套机制（G1 预防性、无时钟；这里事后停放）。短冷却无意义——
        额度每日重置，几分钟后放出去只会再撞一次 402。
        """
        deadline = float(until) if until else float(next_checkin_window()[0])
        if deadline > self.balance_until:
            self.balance_until = deadline
        self.balance_reason = str(detail or "")[:200]
        return self.balance_until

    def revive_balance_cooldown(self):
        """只有**余额刷新看到 remain > 0** 才解 402 停放（不是通用 clear_error）。"""
        if not self.balance_until:
            return False
        cred = self.credits
        # issue #39：团队账号可能套餐 remain=0 但共享池仍有余额——按有效余额解封。
        remain = effective_remain(cred)
        try:
            if remain is None or float(remain) <= 0:
                return False
        except (TypeError, ValueError):
            return False
        self.balance_until = 0.0
        self.balance_reason = ""
        return True

    def note_error(self, message, cooldown=60, single_account=False, model=None, until=None):
        """记录一次错误并设置冷却；**返回实际生效的冷却秒数**（#29）。

        日志要打真值：单号池（single_account）会被压到 3s，照请求值打会把
        3s 显示成 60s，误导 429/503 的排查。返回值对既有调用方零影响
        （它们都是语句式调用、忽略返回值）。
        """
        self.last_error = str(message)[:200]
        if model:
            wait = max(1.0, float(until) - time.time()) if until else (
                3.0 if single_account else float(cooldown))
            self.model_cooldowns[model] = time.time() + wait
            return wait
        actual_cooldown = 3 if single_account else cooldown
        self.cooldown_until = time.time() + actual_cooldown
        return actual_cooldown

    def throttle_wait(self, model=None):
        """Seconds until this account can serve `model` again (0 = right now)."""
        if not self.enabled or not self.access_token:
            return 0.0
        now = time.time()
        wait = max(0.0, self.cooldown_until - now,
                   self.breaker_until - now)
        if model:
            wait = max(wait, max(0.0, self.model_cooldowns.get(model, 0.0) - now))
        return wait

    def clear_error(self, model=None):
        if model:
            self.model_cooldowns.pop(model, None)
        else:
            self.model_cooldowns.clear()
        if self.last_error or self.cooldown_until:
            self.last_error = ""
            self.cooldown_until = 0

    # -- 失败治理三族（task-74；状态写入统一走 _QUOTA_LOCK）------------------
    def note_soft_rate(self, message):
        """三族一：账号级软限流（429 类）→ 指数退避，复用 cooldown_until。

        第 n 次连续软限流退避 min(600 * 2^(n-1), 7200) 秒（封顶 2 小时）。
        复位：note_success() 立即清零；或退避自然到期。退避不是封禁——本方
        法绝不改 enabled，账号只是稍后再试。
        """
        with _QUOTA_LOCK:
            self.last_error = str(message)[:200]
            self.soft_streak += 1
            wait = soft_backoff(self.soft_streak)
            deadline = time.time() + wait
            if deadline > self.cooldown_until:
                self.cooldown_until = deadline
        return wait

    def note_failure(self, message):
        """三族二：硬错误熔断——连续 >= BREAKER_THRESHOLD 次才起跳窗口。

        窗口期间 ready() 直接 False（不再重试）；窗口过后自动恢复（连续失败
        越多窗口越长，封顶 6 小时）。复位：note_success() 清计数与窗口。
        注意：凭证永久失效（session dead）仍走既有 enabled=False 路径，不是
        本族——那是必须重新登录，不是稍后自愈。
        """
        with _QUOTA_LOCK:
            self.last_error = str(message)[:200]
            self.fails += 1
            if self.fails >= BREAKER_THRESHOLD:
                wait = breaker_backoff(self.fails)
                deadline = time.time() + wait
                if deadline > self.breaker_until:
                    self.breaker_until = deadline
        return self.breaker_until

    def note_unknown_failure(self, message):
        """三族三：未知错误降权窗口——不是不可用。

        连续 >= DEGRADE_THRESHOLD 次后进入降权窗口：ready() 不受影响（账号仍
        可用），但 AccountPool._rotate_pick 两轮扫描会先跳过降权中的账号，只
        有没有更健康候选时才回落使用它（不楔死）。同时喂熔断计数（未知也可
        能真是硬错误）。复位：note_success() 清零；窗口自然到期隐式恢复。
        """
        with _QUOTA_LOCK:
            self.last_error = str(message)[:200]
            self.degrade_count += 1
            self.fails += 1
            now = time.time()
            if self.degrade_count >= DEGRADE_THRESHOLD:
                wait = degrade_backoff(self.degrade_count)
                deadline = now + wait
                if deadline > self.degrade_until:
                    self.degrade_until = deadline
            if self.fails >= BREAKER_THRESHOLD:
                wait = breaker_backoff(self.fails)
                deadline = now + wait
                if deadline > self.breaker_until:
                    self.breaker_until = deadline
        return self.degrade_until

    def degraded(self, now=None):
        """是否处于降权窗口（供选号排序降权；非阻断，不参与 ready）。"""
        now = now if now is not None else time.time()
        return self.degrade_until > now

    def note_success(self, model=None):
        """一次成功请求清空全部账号级惩罚（三族计数 + 软冷却 + 模型冷却）。

        三族的统一复位入口：成功后 soft_streak / fails / degrade_count 归零、
        breaker / degrade 窗口清零。刻意不动 balance_until——402 停放专属，
        只由 revive_balance_cooldown() 在余额刷新看到 remain>0 时解除（两套
        机制不合并，见 03 文档 2.9.4 的移植约定）。
        """
        with _QUOTA_LOCK:
            if model:
                self.model_cooldowns.pop(model, None)
            self.soft_streak = 0
            self.fails = 0
            self.degrade_count = 0
            self.breaker_until = 0.0
            self.degrade_until = 0.0
            self.last_error = ""
            self.cooldown_until = 0.0

    # -- 出站头 ------------------------------------------------------------
    def headers(self, purpose="openapi"):
        cfg = get_realm_config(self.realm)
        return {
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "User-Agent": CLIENT_UA,
            "Authorization": "Bearer " + self.access_token,
            "X-Request-ID": generate_request_id(self.uid),
            "X-Machine-ID": derive_id(self.uid, "machine"),
            "X-Session-ID": derive_id(self.uid, "session"),
            "Origin": cfg["website"],
            "Referer": cfg["website"] + "/",
        }

    def desktop_headers(self):
        """桌面端 0.4.3 同款出站头（活动平台 /sash/... 必需）。

        官方桌面端调用 `/sash/api/v1/me/campaigns` 时携带：
          Authorization: Bearer <token>
          User-Agent: Qoder
          Cosy-ClientType: 10（桌面端；CLI 是 5、QoderWork 是 6）
          Cosy-Version: <桌面端版本>
          Cosy-MachineOS / MachineHostname / MachineId / MachineToken /
          MachineType / MachineCode

        两层坑（都已踩过）：
          1. 缺 UA / cosy-clienttype / cosy-version → 服务端不报错但返回
             **空活动列表**（实测这三个是展示活动所必需）。
          2. 机器头「半套」问题（issue #10，Linux/Docker 实测）：服务端把
             **全套派生** cosy-machine* 六头判定为非官方客户端，把 CLAIMABLE 的
             「每日领取 100 Credits」**整条过滤**，列表只剩 VIEW_DETAILS 类；
             已领取账号不受影响（所以首次领取时最易误判为"本来就没活动"）。
             逐头隔离实测：六头任一个**单独**出现 → 活动可见；六头全发 →
             被过滤；去掉 machinetoken 或 machineid → 可见。
        因此本函数的策略（issue #10 修复）：
          - 原生桥给出真身份（machineToken 非空）→ 发全套六头，保持官方
            客户端同款行为（真头是否额外解锁设备定向活动未验证，维持现状）；
          - derived 分支（无原生桥，如 Linux/Docker）→ **一律不发** cosy-machine*
            六头，只发 UA / cosy-clienttype / cosy-version。
        machine_identity_source 依旧如实记录（看板与诊断在用）。
        """
        h = dict(self.headers())
        h["User-Agent"] = "Qoder"
        h["cosy-clienttype"] = DESKTOP_CLIENT_TYPE
        h["cosy-version"] = desktop_version()
        ident = native_machine_identity(self.realm, self.uid)
        if ident.get("machineToken"):
            # 仅原生身份可用时发送机器头（issue #10：派生六头会被服务端判定
            # 为非官方客户端并整条过滤 CLAIMABLE 活动；详见上方 docstring）。
            h["cosy-machineid"] = derive_id(self.uid, "machine")
            h["cosy-machinetoken"] = ident.get("machineToken") or \
                derive_machine_token(self.uid)
            h["cosy-machinetype"] = ident.get("machineType") or \
                derive_machine_type(self.uid)
            h["cosy-machinecode"] = ident.get("machineCode") or \
                derive_id(self.uid, "machinecode")
            h["cosy-machineos"] = MACHINE_OS
            h["cosy-machinehostname"] = MACHINE_HOSTNAME
            self.machine_headers_state = MACHINE_HEADERS_NATIVE
        else:
            # 无原生桥：一个机器头都不发（issue #10）。状态如实登记，供
            # campaigns().machine_headers 与 INTL 已知限制提示使用。
            self.machine_headers_state = MACHINE_HEADERS_OMITTED
        self.machine_identity_source = ident.get("source") or "derived"
        return h

    def _machine_headers_hint(self):
        """机器头状态相关的可读提示；当前只在 INTL + 未发送机器头时非空。

        国际版服务端要求真实的 UMID 机器身份（官方客户端组件生成、每 50 分钟
        刷新）。本机无该组件时必须"不发头"（issue #10），服务端可能因此不返回
        活动——这是**已知限制**，不要误报成"今天没有活动"。CN 侧不发头是正确
        行为，不给提示。
        """
        if self.realm == "intl" and \
                getattr(self, "machine_headers_state", "") == MACHINE_HEADERS_OMITTED:
            return ("国际版服务端要求真实的 UMID 机器身份（由官方客户端组件生成、"
                    "每 50 分钟刷新），本机没有该组件、本次未发送机器头：活动列表"
                    "可能不可见、领取可能失败。这是已知限制，不等于今天没有活动。")
        return ""

    # -- 刷新（按 token 前缀路由） ----------------------------------------
    def refresh(self):
        """刷新 access token。drt- 走 deviceToken，jrt-/PAT 走 jobToken。

        PAT 永不覆盖活跃的 OAuth 会话，只做 jrt- 过期后的最终兜底。
        """
        cfg = get_realm_config(self.realm)
        base = cfg["openapi"]
        # 1) OAuth 设备族
        if self.refresh_token.startswith("drt-"):
            return self._post_token(base + PATH_DEVICE_REFRESH,
                                    {"refresh_token": self.refresh_token}, kind="device")
        # 2) jobToken 族：jrt- 优先，失败回落 PAT 重新交换
        if self.refresh_token:
            if self._post_token(base + PATH_JOB_REFRESH,
                                {"refresh_token": self.refresh_token}, kind="job"):
                return True
        if self.personal_token:
            if self._post_token(base + PATH_JOB_EXCHANGE,
                                {"personal_token": self.personal_token}, kind="job"):
                return True
        if not self.refresh_token and not self.personal_token:
            self.last_error = "no refresh token; sign in again"
        return False

    def _post_token(self, url, payload, kind):
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": CLIENT_UA,
        }
        try:
            data = http_json(url, data=json.dumps(payload).encode(), method="POST",
                             headers=headers, timeout=30)
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read().decode("utf-8", "replace")
            except Exception:
                body = ""
            self.last_error = "refresh failed: HTTP %d %s" % (exc.code, body[:160])
            if exc.code in (401, 403) and session_dead(body):
                self.enabled = False
                self.last_error = "session dead (TOKEN_EXPIRE): re-login required"
            return False
        except Exception as exc:
            self.last_error = "refresh failed: %s" % exc
            return False

        if kind == "device":
            token = data.get("token") or data.get("device_token") or ""
            refresh = data.get("refresh_token") or self.refresh_token
            exp = _device_expiry(data)
        else:
            token = data.get("token") or ""
            refresh = data.get("refresh_token") or self.refresh_token
            if data.get("expires_in"):
                exp = int(time.time() + int(data["expires_in"]) / 1000)
            else:
                exp = self.expires_at
        if not token:
            self.last_error = "refresh returned no token"
            return False
        self.access_token = token
        self.refresh_token = refresh
        self.expires_at = exp or self.expires_at
        self.last_error = ""
        self.cooldown_until = 0
        if self.path and os.path.exists(os.path.dirname(self.path)):
            self.save(os.path.dirname(self.path))
        try:
            from qoder_sign import SESSIONS
            SESSIONS.invalidate(self.uid)   # 旧 COSY 会话携带旧 token，必须重建
        except Exception:
            pass
        return True

    # -- 签到 / 额度 / 套餐 ------------------------------------------------
    def _mark_checkin_capability(self, available, reason=""):
        self._checkin_cap = bool(available)
        self._checkin_cap_reason = reason or ""
        self._checkin_cap_at = time.time()

    def checkin_capability(self):
        """签到能力（运行时探测结果）。

        None  = 尚未探测（调用方应实际尝试一次）
        True  = 本账号所在区域存在 /daily-check-in 接口
        False = 接口不存在（404/405/410，实测国际版即如此）——缓存 TTL 内跳过
        """
        if self._checkin_cap is None:
            return None, ""
        if time.time() - self._checkin_cap_at > CHECKIN_PROBE_TTL:
            return None, self._checkin_cap_reason
        return self._checkin_cap, self._checkin_cap_reason

    def can_checkin(self):
        """今日是否还需要签到（能力由运行时探测，不再按区域硬编码）。"""
        capable, _ = self.checkin_capability()
        if capable is False:
            return False
        if not self.last_checkin:
            return True
        return not self._checkin_stamped_today()

    def checkin_status(self):
        """GET daily-check-in/status -> (ok, summary|error)。

        接口在本区域不存在时返回 (False, {"unavailable": True, reason:...})，
        并记录能力探测结果（活动上线后 TTL 到期会自动重探）。
        """
        cfg = get_realm_config(self.realm)
        url = cfg["openapi"] + PATH_CHECKIN_STATUS
        try:
            q = http_json(url, method="GET", headers=self.headers(), timeout=15,
                          retries=2)
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read().decode("utf-8", "replace")
            except Exception:
                body = ""
            if exc.code in (404, 405, 410):
                self._mark_checkin_capability(
                    False, "%s (HTTP %d)" % (CHECKIN_REASON_NOT_FOUND, exc.code))
                return False, {
                    "unavailable": True,
                    "reason": CHECKIN_REASON_NOT_FOUND,
                    "http": exc.code,
                    "error": "HTTP %d %s" % (exc.code, body[:160]),
                }
            if exc.code == 403:
                # issue #40：403 多为**出口/WAF 拦截**（国际版该接口本来就不存在，
                # 正常应 404）。不写能力缓存（下次仍重探），给出可读原因——
                # 面板显示「上游拒绝访问」而不是一句裸 HTTP 403。
                return False, {
                    "unavailable": True,
                    "reason": "上游拒绝访问（HTTP 403）：可能是出口 IP/代理被拦截，"
                              "或本区域未开放该接口",
                    "http": 403,
                    "error": "HTTP 403 %s" % body[:160],
                }
            return False, {"unavailable": False,
                           "error": "HTTP %d %s" % (exc.code, body[:160])}
        except Exception as exc:
            return False, {"unavailable": False, "error": str(exc)}
        self._mark_checkin_capability(True, "")
        last = ""
        if q.get("lastClaimedAt"):
            try:
                last = time.strftime("%Y-%m-%d",
                                     time.localtime(int(q["lastClaimedAt"])))
            except Exception:
                last = ""
        today = time.strftime("%Y-%m-%d")
        status = str(q.get("status") or "")
        return True, {
            "status": status,
            "active": status in ("CLAIMABLE", "CLAIMED"),
            "today_checked_in": status == "CLAIMED" and last == today,
            "streak_days": int(q.get("currentStreakDays") or 0),
            "total_claim_days": int(q.get("totalClaimDays") or 0),
            "reward_credits": int(q.get("rewardCredits") or 0),
            "total_reward_credits": int(q.get("totalRewardCredits") or 0),
            "next_claim_at": int(q.get("nextClaimAt") or 0),
            "last_claimed_at": int(q.get("lastClaimedAt") or 0),
            "reward_expires_at": int(q.get("rewardExpiresAt") or 0),
        }

    def checkin(self):
        """每日签到：先查状态，未签则领取。返回 {ok, msg, ...}。

        不再按区域门控：接口不存在（国际版 404）按"本区域无此接口"跳过并给
        出明确原因，而不是静默什么都不做（历史问题：看板点签到毫无反应）。
        """
        ok, st = self.checkin_status()
        if not ok:
            if st.get("unavailable"):
                return {"ok": True, "unavailable": True,
                        "reason": st.get("reason"),
                        "msg": "本区域未开放 /sash/api/v1/me/daily-check-in 接口"
                               "（HTTP %s）：每日领取活动改由官方客户端承接"
                               % st.get("http")}
            return {"ok": False, "error": st.get("error") or str(st)}
        if st["today_checked_in"]:
            # #25-e：上游已领 -> 本地同步盖章（否则 lastCheckin 恒 null，
            # can_checkin 恒 True，调度器每轮重走该号——naonao 实测）。
            if not self._checkin_stamped_today():
                self._stamp_checkin()
            return {"ok": True, "already": True, "msg": "今日已签到",
                    "streak_days": st["streak_days"], "reward_credits": st["reward_credits"]}
        if not st["active"]:
            # CLAIMABLE / CLAIMED 之外的状态（如 DISABLED：活动批次下线），
            # 不发起无意义的 claim，按"活动未开放"成功跳过。
            return {"ok": True, "disabled": True, "status": st.get("status"),
                    "msg": "官方签到活动未开放 (status=%s)" % (st.get("status") or "?")}
        cfg = get_realm_config(self.realm)
        url = cfg["openapi"] + PATH_CHECKIN_CLAIM
        try:
            res = http_json(url, data=b"{}", method="POST",
                            headers=self.headers(), timeout=15, retries=1)
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read().decode("utf-8", "replace")
            except Exception:
                body = ""
            # 上游并发/重复领取返回 409 ALREADY_CLAIMED —— 归一化为“已签”
            if "ALREADY_CLAIMED" in body:
                self._stamp_checkin()
                self._refresh_credits_quietly()
                return {"ok": True, "already": True, "msg": "今日已签到"}
            return {"ok": False, "error": "HTTP %d %s" % (exc.code, body[:160])}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        if str(res.get("result") or "") == "ALREADY_CLAIMED" or res.get("success") is False and "ALREADY" in str(res.get("error") or ""):
            self._stamp_checkin()
            self._refresh_credits_quietly()
            return {"ok": True, "already": True, "msg": "今日已签到"}
        if res.get("success") is False:
            # 复查一次：上游可能已记账
            ok2, st2 = self.checkin_status()
            if ok2 and st2["today_checked_in"]:
                self._stamp_checkin()
                return {"ok": True, "already": True, "msg": "今日已签到（复查确认）"}
            return {"ok": False, "error": str(res.get("error") or res)[:160]}
        reward = int(res.get("rewardCredits") or 0)
        self._stamp_checkin()
        # issue #40 后续：签到发的积分要**立刻反映到面板**。上游把奖励记进
        # addOnQuota，不刷余额的话面板仍显示旧值（报告者看到的
        # 「提示 +100 积分但余额不显示」）。失败不影响签到结果本身。
        self._refresh_credits_quietly()
        return {"ok": True, "msg": "签到成功 +%d 积分" % reward,
                "reward_credits": reward}

    def _refresh_credits_quietly(self):
        """签到/领奖后顺手刷新余额（best-effort，绝不抛）。"""
        try:
            return self.fetch_credits()
        except Exception:
            return {"ok": False, "error": "credits refresh after checkin failed"}

    def _campaigns_get(self):
        """一次活动列表请求（桌面端头 → 401/403 回退普通头）。

        返回 (payload|None, http_code, error_str)；payload 为服务端 JSON。
        """
        url = get_realm_config(self.realm)["openapi"] + PATH_CAMPAIGNS
        try:
            return http_json(url, method="GET", headers=self.desktop_headers(),
                             timeout=15, retries=1), 200, ""
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read().decode("utf-8", "replace")
            except Exception:
                body = ""
            # 桌面端头被拒（401/403）时回退普通头，至少保留"能不能看到"的信息
            if exc.code in (401, 403):
                try:
                    return http_json(url, method="GET", headers=self.headers(),
                                     timeout=15, retries=1), 200, ""
                except urllib.error.HTTPError as exc2:
                    return None, exc2.code, ("HTTP %d (desktop) / HTTP %d (plain)"
                                             % (exc.code, exc2.code))
                except Exception as exc2:
                    return None, exc.code, "HTTP %d (desktop) / %s (plain)" % (exc.code, exc2)
            return None, exc.code, "HTTP %d %s" % (exc.code, body[:160])
        except Exception as exc:
            return None, 0, str(exc)

    def campaigns(self, force=False):
        """GET /sash/api/v1/me/campaigns -> 官方活动平台状态（双区域通用）。

        官方把"每日领取 100 Credits"等限时活动搬到了 campaign 平台，取列表要
        **两层都对**：① 桌面端请求头（`desktop_headers()`）；② 服务端认可的
        **真实机器身份**（原生桥取，见 `native_machine_identity`）。两者任一
        不对都表现为 HTTP 200 + 列表里少活动（不报错），这正是"领不到"的根因。

        身份被拒时的自愈：若本次 `showCampaign=false`（通常意味着身份被判定为
        非官方客户端），清掉组件种子、强制换一个**新的**身份并重试，避免被拒
        的身份一直卡到重建容器（#18：同种子下组件只会返回同一个身份）。
        结果短缓存 CAMPAIGNS_TTL 秒（force=True 绕过）——该请求是上游最慢的
        一环，看板切换视图时不该重复等它。

        返回 {ok, available, show_campaign, claimable, campaign_url, campaigns,
              identity}，每条活动含 action_type / claim_status / benefit / end_at。
        """
        now = time.time()
        if not force and self._campaigns_cache \
                and now - self._campaigns_cache[0] < CAMPAIGNS_TTL:
            return self._campaigns_cache[1]
        q, code, err = self._campaigns_get()
        # 自愈条件必须与生产端同源：machine_identity_source 的合法值是
        # MACHINE_IDENTITY_NATIVE("runtime-info") 或 "derived"；"native" 仅为
        # 历史/测试桩别名。此前误用 native 字面量做等值判断，导致此分支永不命中
        # （口径分裂，已按 Lead 裁决统一到 runtime-info）。
        # 取舍留痕（勿误读为漏写）：此处**不做**缓存新鲜度节流。曾评估"仅当
        # _native_ident_cache 缺失或 ≥NATIVE_IDENTITY_TTL 才自愈"的方案，未采纳：
        # 该阈值会在"缓存新鲜但身份已被服务端作废"时阻止自愈，把用户重新打回
        # "整天领不到"的原始故障；宁可多一次刷新（原生桥实测 3.7s，且触发条件
        # 仅为服务端明确返回 showCampaign=false、并非高频路径），也不制造
        # "为什么没自愈"的新谜题（Lead 裁决，不实施）。
        if isinstance(q, dict) and not q.get("showCampaign") \
                and getattr(self, "machine_identity_source", "") in \
                (MACHINE_IDENTITY_NATIVE, "native"):
            # 服务端已不认当前身份，必须换一个**新的**：force 跳过全部缓存并
            # **覆盖落盘**（设计 §9.1/§9.2 写入点②），否则重建后仍会用被拒的旧身份。
            native_machine_identity(self.realm, self.uid, force=True)
            q2, code2, err2 = self._campaigns_get()
            if isinstance(q2, dict) and q2.get("showCampaign"):
                q, code, err = q2, code2, err2
        if not isinstance(q, dict):
            return {"ok": False, "available": code not in (404, 405, 410),
                    "error": err or ("HTTP %d" % code),
                    "machine_headers": getattr(self, "machine_headers_state",
                                               MACHINE_HEADERS_OMITTED),
                    "hint": self._machine_headers_hint()}
        items = []
        raw = q.get("campaigns")
        for c in (raw if isinstance(raw, list) else []):
            if not isinstance(c, dict):
                continue
            placements = c.get("placements")
            benefit = c.get("benefit") if isinstance(c.get("benefit"), dict) else {}
            # 官方中文标题/说明/详情页（placements[].content.zh）——活动名直接显示中文
            title_zh = title_en = desc_zh = detail = button = ""
            for pl in (placements if isinstance(placements, list) else []):
                cont = (pl or {}).get("content")
                if not isinstance(cont, dict):
                    continue
                zh = cont.get("zh") if isinstance(cont.get("zh"), dict) else {}
                en = cont.get("en") if isinstance(cont.get("en"), dict) else {}
                title_zh = title_zh or str(zh.get("title") or "")
                title_en = title_en or str(en.get("title") or "")
                desc_zh = desc_zh or str(zh.get("description") or "")
                detail = detail or str(zh.get("detailUrl") or en.get("detailUrl") or "")
                button = button or str(zh.get("buttonText") or en.get("buttonText") or "")
                if title_zh and desc_zh and detail:
                    break
            items.append({
                "title_zh": title_zh,
                "title_en": title_en,
                "desc_zh": desc_zh,
                "detail_url": detail,
                "button_text": button,
                "campaign_id": str(c.get("campaignId") or c.get("campaign_id") or ""),
                "campaign_key": str(c.get("campaignKey") or c.get("campaign_key") or ""),
                "action_type": str(c.get("actionType") or c.get("action_type") or ""),
                "claim_status": str(c.get("claimStatus") or c.get("claim_status") or ""),
                "start_at": normalize_epoch(c.get("startAt") or c.get("start_at")),
                "end_at": normalize_epoch(c.get("endAt") or c.get("end_at")),
                "benefit": {
                    "kind": str(benefit.get("kind") or ""),
                    "amount": int(benefit.get("amount") or 0),
                },
                "required_achievement_key": str(
                    c.get("requiredAchievementKey") or ""),
                "achievement_completed": bool(c.get("achievementCompleted")),
                "unavailable_reason": str(c.get("unavailableReason") or ""),
                "placements": placements if isinstance(placements, list) else [],
            })
        st = {
            "ok": True,
            "available": True,
            "show_campaign": bool(q.get("showCampaign")),
            "claimable": bool(q.get("claimable")),
            "campaign_url": str(q.get("campaignUrl") or ""),
            "campaigns": items,
            # 身份来源（runtime-info=原生桥 / derived=派生回退；与下面的
            # machine_headers 正交——derived 时本次根本不发机器头，issue #10）
            "identity": getattr(self, "machine_identity_source", "derived"),
            # 本次实际是否携带 cosy-machine* 机器头：native / omitted
            "machine_headers": getattr(self, "machine_headers_state",
                                       MACHINE_HEADERS_OMITTED),
            # INTL+omitted 时的"已知限制"提示；其余场景为空串（CN 不发提示）
            "hint": self._machine_headers_hint(),
        }
        self.campaign_status = st
        self._campaigns_cache = (time.time(), st)
        return st

    def campaign_reward(self, campaign_id):
        """GET …/campaigns/{id}/reward -> 该活动的发放状态（幂等，只读）。"""
        cfg = get_realm_config(self.realm)
        url = cfg["openapi"] + (PATH_CAMPAIGN_REWARD % campaign_id)
        try:
            return http_json(url, method="GET", headers=self.desktop_headers(),
                             timeout=20, retries=1)
        except Exception as exc:
            return {"error": str(exc)}

    def claim_campaign(self, campaign_id):
        """POST …/campaigns/{id}/claim -> 领取该活动奖励（官方幂等语义）。

        返回 {ok, status, replayed, failure_code, grant_id, amount, message}。
        服务端按"人"去重（同一机器指纹下的多账号合并为一人）：
          - 已领取 -> status=CLAIMED + replayed=true（幂等，不重复发放）；
          - 同人已领 -> status=BLOCKED + failureCode=SAME_PERSON_ALREADY_CLAIMED
            （实测：同机多号共享每轮一次的额度，第二个号会被 BLOCKED 且列表里
            隐藏该活动——官方文档写"每账号"，实际执行是"每人"）。
        """
        cfg = get_realm_config(self.realm)
        url = cfg["openapi"] + (PATH_CAMPAIGN_CLAIM % campaign_id)
        try:
            r = http_json(url, data=b"{}", method="POST",
                          headers=self.desktop_headers(), timeout=20, retries=1)
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read().decode("utf-8", "replace")
            except Exception:
                body = ""
            code = ""
            try:
                code = str((json.loads(body) or {}).get("errorCode") or "")
            except Exception:
                code = ""
            if exc.code == 409 or code.upper() in ("ALREADY_CLAIMED", "REPLAYED"):
                return {"ok": True, "status": "CLAIMED", "replayed": True,
                        "message": "今日已领取（上游幂等确认）"}
            return {"ok": False, "error": "HTTP %d %s" % (exc.code, body[:160])}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        status = str(r.get("status") or "").upper()
        failure = str(r.get("failureCode") or "").upper()
        if failure == "SAME_PERSON_ALREADY_CLAIMED" or status == "BLOCKED":
            return {"ok": False, "blocked": True, "status": status or "BLOCKED",
                    "replayed": False, "failure_code": failure or "BLOCKED",
                    "amount": int(((r.get("benefit") or {})
                                   if isinstance(r.get("benefit"), dict)
                                   else {}).get("amount") or 0),
                    "message": "同人已领取（同一设备/身份下其他账号本轮已领，"
                               "服务端按人去重）",
                    "raw": r}
        if not status and r.get("success") is False:
            return {"ok": False, "error": str(r.get("error") or r)[:160]}
        # 兑换码类奖励（REDEMPTION_CODE）：官方客户端语义 = CLAIMED 且
        # redemptionCode 非空才算拿到；仅 CLAIMED 无码 = 发放确认中。
        code = str(r.get("redemptionCode") or "").strip()
        if code:
            self.campaign_codes[campaign_id] = code
            if self.path and os.path.exists(os.path.dirname(self.path)):
                self.save(os.path.dirname(self.path))
        if failure in _CAMPAIGN_FAILURE_CN:
            return {"ok": False, "status": status or "NOT_ELIGIBLE",
                    "failure_code": failure, "replayed": False,
                    "redemption_code": code,
                    "message": _CAMPAIGN_FAILURE_CN[failure], "raw": r}
        return {
            "ok": status in ("CLAIMED", "GRANTED", "SUCCESS"),
            "status": status,
            "replayed": bool(r.get("replayed")),
            "failure_code": failure or "",
            "grant_id": str(r.get("grantId") or ""),
            "redemption_code": code,
            "confirming": bool(status == "CLAIMED" and not code),
            "amount": int(((r.get("benefit") or {}) if isinstance(r.get("benefit"), dict)
                           else {}).get("amount") or r.get("amount") or 0),
            "message": ("已领取" if r.get("replayed") else "领取成功")
                       + (("，兑换码：%s" % code) if code else
                          ("，兑换码发放确认中" if status == "CLAIMED" else "")),
            "raw": r,
        }

    # 兼容类内调用：self.campaign_label(c) / qoder_accounts.campaign_label(c)
    campaign_label = staticmethod(campaign_label)

    def campaign_checkin(self, gap=None, only_kinds=None):
        """活动平台签到：领取所有 CLAIMABLE 的 Credits 活动（每日 100 等）。

        先取一次机器身份（内存 → 落盘 → 组件；**不**强制换新——避免把
        「重建不换」的落盘缓存刷掉，设计 §9.1），再列活动、逐个领取。
        多账户场景下每个账号独立走这一遍。

        only_kinds: 只领这些 benefit.kind 的活动（如 ("", "CREDITS") 表示只做
                    每日签到领积分，不动兑换码/券类福利）。
        gap: 相邻两次 claim 请求之间的间隔（秒）；None=使用模块级安全默认
             CLAIM_GAP_DEFAULT（>=1.0s），显式传入时按传入值（下限 0）。

        返回 {ok, claimed:[...], already:[...], earned, message, campaigns,
              next_available_at, next_available_note}
          - 已是 CLAIMED 的活动计入 already（"今日已领取"）
          - 无可领取项且没有任何活动 -> ok=True + message 说明
          - next_available_at / next_available_note：下一个「每日 10:00（UTC+8）」
            的 epoch 秒与可读文本（见 next_checkin_window）；**无论本次是否真的
            领到都给出**，前端只在"没到账"时渲染。
        """
        claim_gap = CLAIM_GAP_DEFAULT if gap is None else max(0.0, float(gap))
        # 领取前只需"身份有效"（内存 → 落盘 → 组件），**不要** force 刷新：
        # force 会让每次签到都换一套身份并把落盘缓存刷掉，「重建不换」就失效了
        # （设计 §9.1）。身份真被服务端作废时由 campaigns() 的自愈兜底刷新。
        native_machine_identity(self.realm, self.uid)
        st = self.campaigns(force=True)      # 领取路径必须绕过缓存，看最新状态
        if not st.get("ok"):
            next_at, next_note = next_checkin_window()
            return {"ok": False, "error": st.get("error") or "campaigns 查询失败",
                    "earned": 0, "claimed": [], "already": [], "blocked": [],
                    "pending": [], "locked": [], "codes": [], "views": [],
                    "next_available_at": next_at,
                    "next_available_note": next_note}
        claimed, already, earned, errors, blocked = [], [], 0, [], []
        pending, locked, codes, views = [], [], [], []   # 券/成就/详情类
        for c in st["campaigns"]:
            if only_kinds is not None:
                kind = str((c.get("benefit") or {}).get("kind") or "").upper()
                if kind not in only_kinds:
                    continue      # 只要"每日签到领积分"时跳过券类/其它奖励
            cid = c["campaign_id"]
            cname = c["campaign_key"] or cid
            label = self.campaign_label(c)
            reason = str(c.get("unavailable_reason") or "").upper()
            action_type = str(c.get("action_type") or "")
            try:
                amount = int((c.get("benefit") or {}).get("amount") or 0)
            except (TypeError, ValueError):
                amount = 0
            if c["claim_status"] == "CLAIMED":
                # #25 遗留：**归类先于 action_type 过滤**——官方在领取后会把
                # actionType 降级成 VIEW_DETAILS（intl 实测），若先按 action_type
                # 过滤会把「已领」事实丢掉，兜底成「当前账号暂无可领取的官方
                # 活动」（报告者看到的那句日志）。归类判据与 tasks 侧同源：
                # 有奖励（amount>0）或 action_type 仍是可领类 -> 已领取；
                # 真·详情类（无奖励）才算 views。
                if amount > 0 or action_type in ("", "CLAIM_BENEFIT"):
                    already.append(c)
                    # #25-e：已领 = 今日签到已完成——循环后统一盖章（见下），
                    # 不在循环内 save（多活动时不重复写盘）。
                else:
                    views.append(c)      # 详情类活动：没有奖励可领，不算"已领取"
                if self.campaign_codes.get(cid):
                    codes.append({"campaign": label, "code": self.campaign_codes[cid]})
                continue
            # 以下只针对"还没领"的活动：详情类无奖励，不参与领取/归类
            if action_type not in ("", "CLAIM_BENEFIT"):
                continue
            if c["claim_status"] != "CLAIMABLE":
                # 不可领取但并非"与我们无关"：名额发完 / 成就未完成要如实报出来
                # （官方前端状态机：REDEMPTION_CODE_OUT_OF_STOCK=outOfStock，
                #   ACHIEVEMENT_NOT_COMPLETED=locked）
                if reason == "REDEMPTION_CODE_OUT_OF_STOCK":
                    pending.append(c)
                elif reason == "ACHIEVEMENT_NOT_COMPLETED"                         or c.get("achievement_completed") is False:
                    locked.append(c)
                continue
            if c["action_type"] and c["action_type"] != "CLAIM_BENEFIT":
                continue      # VIEW_DETAILS 类活动无需（也不能）领取
            if self.campaign_blocked_until.get(cid, 0) > time.time():
                blocked.append({"campaign": cname,
                                "failure_code": "SAME_PERSON_ALREADY_CLAIMED",
                                "cooldown": True})
                continue      # 同人已领（冷却中）：不再重复 POST
            res = self.claim_campaign(cid)
            if res.get("ok"):
                amount = res.get("amount") or c["benefit"]["amount"] or 0
                self._stamp_checkin()
                if res.get("replayed"):
                    already.append(c)
                else:
                    claimed.append(c)
                    earned += int(amount or 0)
                if res.get("redemption_code"):
                    codes.append({"campaign": label,
                                  "code": res["redemption_code"]})
            elif res.get("blocked"):
                # 服务端按"人"去重：同机器/同身份下其他账号本轮已领。
                # 记 6 小时冷却（多账号同机器时不必每轮都试），并保留原因。
                self.campaign_blocked_until[cid] = time.time() + 6 * 3600
                if self.path and os.path.exists(os.path.dirname(self.path)):
                    self.save(os.path.dirname(self.path))
                blocked.append({"campaign": label,
                                "failure_code": res.get("failure_code")})
            elif res.get("failure_code") in _CAMPAIGN_FAILURE_CN:
                # 领取瞬间名额发完 / 任务未完成等：按"待重试/被锁"分类，不算失败
                slot = (pending if res["failure_code"] == "REDEMPTION_CODE_OUT_OF_STOCK"
                        else locked)
                slot.append(c)
            else:
                errors.append("%s: %s" % (cname, res.get("error")))
            # 每个 claim 请求之后统一等待一次：成功/被挡/失败都刚打过上游，
            # 相邻请求间隔由 gap 保证（默认 CLAIM_GAP_DEFAULT，防风控）。
            time.sleep(claim_gap)
        if already and not self._checkin_stamped_today():
            # #25-e：已有 CLAIMED 的签到类活动 = 今日已领 -> 本地盖章一次。
            self._stamp_checkin()
        if claimed:
            msg = "活动领取成功 +%d Credits（%s）" % (
                earned, "、".join(self.campaign_label(c) for c in claimed))
        elif blocked:
            msg = ("同人已领取：同一设备/身份下的其他账号本轮已领过（服务端按人去重，"
                   "failureCode=%s）" % blocked[0].get("failure_code"))
        elif already:
            msg = "今日活动奖励已领取（%s）" % "、".join(
                self.campaign_label(c) for c in already)
        elif pending:
            msg = ("名额已发完，次日 10:00 后可再领：%s（活动 %s）" % (
                "、".join(c["required_achievement_key"] or "无门槛任务"
                          for c in pending),
                ", ".join(c["campaign_key"] or c["campaign_id"] for c in pending)))
        elif locked:
            msg = ("需先在官方桌面端完成新人任务后可领：%s（活动 %s）" % (
                ", ".join(c["required_achievement_key"] or "?"
                          for c in locked),
                ", ".join(c["campaign_key"] or c["campaign_id"] for c in locked)))
        elif errors:
            msg = "活动领取失败：%s" % "; ".join(errors)[:200]
        else:
            msg = "当前账号暂无可领取的官方活动"
        # 有待补货/待完成任务的活动时，把它们附在结论里（一键签到日志要能看到）
        extra = ""
        if pending:
            extra = "；另有活动今日名额已发完、次日 10:00 后重试：%s" % "、".join(
                self.campaign_label(c) for c in pending)
        elif locked:
            extra = "；另有活动需先完成新人任务（%s）：%s" % (", ".join(
                c.get("required_achievement_key") or "?" for c in locked),
                "、".join(self.campaign_label(c) for c in locked))
        if extra and "名额已发完" not in msg and "新人任务" not in msg:
            msg += extra
        # 领取动作会改变活动状态：让下一次列表查询重新拉取（不吃 20s 缓存）
        self._campaigns_cache = None
        # 「下次可签到时间」无条件给出（真领取 / 已领 / 被挡 / 名额发完 / 任务未完成 /
        # 无可领项 全部走这一个出口）：是否渲染由前端按需决定，后端不替前端判断。
        next_at, next_note = next_checkin_window()
        return {"ok": not errors, "claimed": claimed, "already": already,
                "blocked": blocked, "earned": earned, "message": msg,
                "pending": pending, "locked": locked, "codes": codes,
                "views": views,
                "campaigns": st["campaigns"], "errors": errors,
                "next_available_at": next_at,
                "next_available_note": next_note}


    def _checkin_stamped_today(self):
        """#25-e：本地今天是否已盖章（防重复写盘的短路判据）。"""
        return str(self.last_checkin or "").startswith(time.strftime("%Y-%m-%d"))

    def _note_quota_anomaly(self, raw, reason):
        """#25-d：记录一次「不可信的 quota 响应」并**保留旧快照**。

        落盘 lastQuotaAnomaly（含截断 raw 摘要）——下次能把「上游真说
        超额」与「解析失败」一眼分开（报告者最贵的教训：没有原始响应，
        那次写入至今无法回溯）。返回 ok=False 的调用方语义。
        """
        try:
            if isinstance(raw, (str, bytes)):
                raw_digest = str(raw)[:400]
            else:
                raw_digest = json.dumps(raw, ensure_ascii=False)[:400]
        except Exception:
            raw_digest = repr(raw)[:400]
        anomaly = {"at": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "reason": reason, "raw": raw_digest}
        self.last_quota_anomaly = anomaly
        # issue #40 后续：全 0 退化体（团队/免费号常见）此前只在日志里留痕，
        # 面板一片空白看不出"上游确实返回了 0"。这里单独记一个**降级标记**，
        # 面板据此显示「0*（上游返回空额度）」而不是假装没查过。
        self.credits_degraded = {
            "at": anomaly["at"],
            "reason": str(reason or ""),
            "raw": raw_digest[:200],
        }
        if self.path and os.path.exists(os.path.dirname(self.path)):
            self.save(os.path.dirname(self.path))
        return {"ok": False,
                "error": "degenerate quota body (%s); kept previous snapshot"
                         % reason,
                "anomaly": anomaly}

    def _stamp_checkin(self):
        self.last_checkin = time.strftime("%Y-%m-%d %H:%M:%S")
        if self.path and os.path.exists(os.path.dirname(self.path)):
            self.save(os.path.dirname(self.path))

    def _other_realm_quota_hint(self):
        """issue #40 探测：令牌是否属于**另一个区域**（realm 标记错误）。

        部分 403 是「账号标了 intl、令牌却是国内版」导致的——同一个 token
        打另一区的 quota 会成功。只在 403 时探测一次，不发任何写操作。
        返回可读提示字符串；不属于该情形返回空串。
        """
        other = "cn" if self.realm != "cn" else "intl"
        try:
            url = get_realm_config(other)["openapi"] + PATH_QUOTA
            q = http_json(url, method="GET", headers=self.headers(),
                          timeout=20, retries=1)
        except Exception:
            return ""
        if not isinstance(q, dict):
            return ""
        if not any(k in q for k in ("userQuota", "addOnQuota")):
            return ""
        name = "国内版" if other == "cn" else "国际版"
        return ("该账号的令牌在%s可用，本区 403 很可能是**区域标记错误**："
                "请在面板重新扫描/导入该账号（或把 realm 改成 %s）"
                % (name, other))

    def fetch_credits(self):
        """GET /api/v2/quota/usage -> 聚合基础额度 + 赠送/签到额度。"""
        cfg = get_realm_config(self.realm)
        url = cfg["openapi"] + PATH_QUOTA
        hint = ""
        try:
            q = http_json(url, method="GET", headers=self.headers(), timeout=30)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                # issue #40：403 常见于 WAF/出口被拦、或令牌区域标记错误。
                # 先用**桌面端同款头**重试一次（官方客户端会用这套），仍失败
                # 再给出可操作说明——不再把裸 403 当唯一信息。
                try:
                    q = http_json(url, method="GET",
                                  headers=self.desktop_headers(), timeout=30)
                except Exception as exc2:
                    hint = self._other_realm_quota_hint()
                    return {
                        "ok": False,
                        "http": exc.code,
                        "error": "HTTP %s %s%s"
                                 % (exc.code, str(exc2)[:160],
                                    (" ｜ " + hint) if hint else ""),
                    }
            else:
                return {"ok": False, "http": exc.code, "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        # #25-d：非 dict 响应（解析失败形态）直接走退化路径——旧实现会在这里
        # 抛 AttributeError，且不留任何现场。
        if not isinstance(q, dict):
            return self._note_quota_anomaly(q, "non-dict response")
        uq = q.get("userQuota") or {}
        aq = q.get("addOnQuota") or {}

        def _num(d, k):
            try:
                return float(d.get(k) or 0)
            except Exception:
                return 0.0

        def _expiry_of(src):
            """把上游额度对象里的到期信息**防御性透传**进 packages（存在才写）。

            S1 实测（2026-10）：上游 userQuota / addOnQuota 内部**没有**任何
            到期字段，只有响应顶层 expiresAt（归属未证实，刻意不映射到包级——
            误贴会把「按月重置」的额度算成临期，反向消耗）。本函数只做透传、
            不臆造：哪天上游补上 expiresAt / daysLeft / packageCode，
            soonest_expiring_days 无需改码即可生效。
            """
            if not isinstance(src, dict):
                return {}
            out = {}
            for key in ("expiresAt", "expireAt", "expires_at"):
                if src.get(key) is not None:
                    out["expires_at"] = normalize_epoch(src[key])
                    break
            for key in ("daysLeft", "days_left"):
                if src.get(key) is not None:
                    out["days_left"] = src[key]
                    break
            if src.get("noExpiry") is not None:
                out["no_expiry"] = bool(src.get("noExpiry"))
            code = src.get("packageCode") or src.get("package_code")
            if code:
                out["package_code"] = str(code)
            return out

        remain = int(_num(uq, "remaining") + _num(aq, "remaining"))
        used = int(_num(uq, "used") + _num(aq, "used"))
        size = int(_num(uq, "total") + _num(aq, "total"))
        # ---- #25-d：退化 body 合理性守卫（保守）--------------------------
        # 形态（naonao 实测）：isQuotaExceeded=true + 两个 quota 全 0/缺键。
        # 判定**不依赖** expiresAt=253402214400——那是 CN 号「永不过期」的
        # 正常占位，健康号同样返回它，拿它当判据会误伤。
        # 判据：exceeded=true 且 remain 合计=0 且 size 合计=0（「从没有过
        # 额度」；真·耗尽的特征是 size>0 而 remain=0，会被放行）；或两个
        # quota 键都缺失（解析不了 -> 拒绝写入比写 0 诚实）。
        # 保守性：任一信号单独出现都放行——真·余额归零与新号不受影响。
        both_missing = (not isinstance(q.get("userQuota"), dict)
                        and not isinstance(q.get("addOnQuota"), dict))
        degenerate = both_missing or (bool(q.get("isQuotaExceeded"))
                                      and remain == 0 and size == 0)
        if degenerate:
            return self._note_quota_anomaly(
                q, "both quota objects missing" if both_missing
                else "exceeded=true with zero size/remain")
        # issue #39：上游若有**共享额度池**（团队/企业/池化），原样纳入
        # packages 与 credits.shared——此前只显示套餐内 + 赠送，共享额度被漏掉。
        # 字段名按常见形态兼容；不存在就不编造（面板不显示该行）。
        # 实测字段（issue #39 报告者提供）：团队账号返回
        #   "orgResourcePackage": {"used":1548,"remaining":952,"percentage":0.62,
        #                          "unit":"credits","cap":2500,"available":true}
        # 注意它用 cap（不是 total），且带 available——不可用时不计入有效余额。
        shared = None
        for _key in ("orgResourcePackage", "sharedQuota", "teamQuota",
                     "poolQuota", "sharedCredits", "sharedCredit"):
            _src = q.get(_key)
            if isinstance(_src, dict):
                shared = {
                    "remain": int(_num(_src, "remaining")),
                    "used": int(_num(_src, "used")),
                    "size": int(_num(_src, "cap") or _num(_src, "total")),
                    # 缺键 = "没声明不可用" = 可用（与原 bool(_src.get("available", True)) 等价）；
                    # 显式 null / "false" / 0 = 不可用。写成 .get(k, True) + 默认 True
                    # 才保留原语义——只写 _truthy(_src.get("available")) 会把缺键
                    # 变成 False，共享额度白有（issue #39 的症状原样回来）。
                    "available": _truthy(_src.get("available", True), default=True),
                    "source": _key,
                    **_expiry_of(_src),
                }
                break
            if isinstance(_src, (int, float)) and not isinstance(_src, bool):
                shared = {"remain": int(_src), "used": 0, "size": int(_src),
                          "available": True, "source": _key}
                break
        _pkgs = [
            {"name": "基础额度", "remain": int(_num(uq, "remaining")),
             "used": int(_num(uq, "used")), "size": int(_num(uq, "total")),
             **_expiry_of(uq)},
            {"name": "赠送/签到额度", "remain": int(_num(aq, "remaining")),
             "used": int(_num(aq, "used")), "size": int(_num(aq, "total")),
             **_expiry_of(aq)},
        ]
        if shared:
            _pkgs.append({"name": "共享额度", **shared})
        # issue #39：团队账号的 userQuota 可能已用满（remain=0）而共享池仍有余额
        # ——有效余额 = 套餐 remain + 可用的共享 remain。护栏与汇总按它判断，
        # 否则「套餐用满但共享池还有 952」会被误判为余额耗尽。
        _shared_avail = (shared.get("remain") or 0) \
            if (shared and shared.get("available", True)) else 0
        remain_effective = int(remain + _shared_avail)
        self.credits = {
            "remain": remain,
            "remain_effective": remain_effective,
            "used": used,
            "size": size,
            "exceeded": bool(q.get("isQuotaExceeded")),
            "usage_pct": q.get("totalUsagePercentage"),
            "expires_at": normalize_epoch(q.get("expiresAt")),
            "shared": shared,
            "packages": _pkgs,
            "updated_at": time.time(),
            "updated_iso": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        self.last_quota_anomaly = None    # 正常体：清掉上次异常标记
        self.credits_degraded = None
        # task-61：余额刷新是 402 停放的**唯一**解封路径（remain > 0 才解）。
        self.revive_balance_cooldown()
        if self.path and os.path.exists(os.path.dirname(self.path)):
            self.save(os.path.dirname(self.path))
        return {"ok": True, "credits": self.credits}

    def fetch_plan(self):
        """GET /api/v2/user/plan -> 套餐名（Pro Trial 等）。"""
        cfg = get_realm_config(self.realm)
        url = cfg["openapi"] + PATH_PLAN
        try:
            p = http_json(url, method="GET", headers=self.headers(), timeout=15,
                          retries=1)
        except Exception:
            return self.plan
        name = str(p.get("plan_tier_name") or p.get("user_type") or "")
        if name and name != self.plan:
            self.plan = name
            if self.path and os.path.exists(os.path.dirname(self.path)):
                self.save(os.path.dirname(self.path))
        return name

    # -- Pro 升级包（一次性 +1800） ---------------------------------------
    def pro_eligibility(self):
        cfg = get_realm_config(self.realm)
        url = cfg["openapi"] + PATH_PRO_ELIGIBILITY
        try:
            m = http_json(url, method="GET", headers=self.headers(), timeout=15,
                          retries=1)
            return True, bool(m.get("eligible"))
        except urllib.error.HTTPError as exc:
            if exc.code in (404, 403, 410):
                # 端点不存在 / 活动已下线：查询成功，只是不可领取
                return True, False
            return False, "HTTP %d" % exc.code
        except Exception as exc:
            return False, str(exc)

    def pro_claim(self):
        cfg = get_realm_config(self.realm)
        url = cfg["openapi"] + PATH_PRO_CLAIM
        try:
            m = http_json(url, data=b"{}", method="POST", headers=self.headers(),
                          timeout=15, retries=1)
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read().decode("utf-8", "replace")
            except Exception:
                body = ""
            if exc.code == 409 or "ALREADY" in body:
                return {"ok": True, "already": True, "msg": "Pro 升级包已领取过"}
            return {"ok": False, "error": "HTTP %d %s" % (exc.code, body[:160])}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        if m.get("success") is False:
            return {"ok": False, "error": str(m.get("message") or m)[:160]}
        return {"ok": True, "msg": "Pro 升级包领取成功", "data": m}


def token_family(acc):
    """返回账号当前的凭证族：device(OAuth) / job(PAT交换) / pat。"""
    rt = acc.refresh_token or ""
    if rt.startswith("drt-"):
        return "device"
    if rt.startswith("jrt-"):
        return "job"
    if (acc.access_token or "").startswith("pt-"):
        return "pat"
    return "unknown"


def _device_expiry(data):
    """deviceToken 响应的过期时间：expires_in(ms) / expires_at(RFC3339)，默认 30 天。"""
    if data.get("expires_in"):
        return int(time.time() + int(data["expires_in"]) / 1000)
    if data.get("expires_at"):
        try:
            import datetime
            dt = datetime.datetime.strptime(str(data["expires_at"])[:19],
                                             "%Y-%m-%dT%H:%M:%S")
            return int(dt.timestamp())
        except Exception:
            pass
    return int(time.time()) + 30 * 86400


def _human_delta(seconds):
    if seconds is None:
        return None
    if seconds <= 0:
        return "expired"
    days = seconds / 86400.0
    if days >= 1:
        return "%.0f days" % days
    hours = seconds / 3600.0
    if hours >= 1:
        return "%.1f hours" % hours
    return "%d min" % int(seconds / 60)


# ---------------------------------------------------------------------------
# 会话亲和（同一对话固定同一账号，上游按账号缓存 prompt prefix）
# ---------------------------------------------------------------------------
class SessionAffinity(object):
    def __init__(self, ttl=7200, max_entries=5000):
        self.ttl = ttl
        self.max_entries = max_entries
        self.bindings = {}
        self._lock = threading.Lock()

    def get(self, key):
        if not key:
            return None
        with self._lock:
            entry = self.bindings.get(key)
            if not entry:
                return None
            uid, exp = entry
            if time.time() > exp:
                self.bindings.pop(key, None)
                return None
            self.bindings[key] = (uid, time.time() + self.ttl)
            return uid

    def bind(self, key, uid):
        if not key or not uid:
            return
        with self._lock:
            if len(self.bindings) >= self.max_entries:
                now = time.time()
                self.bindings = {k: v for k, v in self.bindings.items() if v[1] > now}
            self.bindings[key] = (uid, time.time() + self.ttl)

    def unbind(self, key):
        if not key:
            return
        with self._lock:
            self.bindings.pop(key, None)


# ---------------------------------------------------------------------------
# AccountPool
# ---------------------------------------------------------------------------
_ACTIVE_POOL = None


def add_to_pool(account):
    """把导入的账号写入活动账号池（AccountPool 构造时自注册）。"""
    if _ACTIVE_POOL is None:
        raise RuntimeError("account pool not initialised")
    return _ACTIVE_POOL.add(account)


class CreditsRefresher(object):
    """后台余额刷新 + 当日用量折叠（task-61 / §2.5，G1 的自动恢复路径）。

    · 每个 tick **最多**刷新一个账号（最陈旧且已过 TTL）—— 上游计费调用有界：
      默认 30min tick + 12h TTL -> 每账号每天约 2 次，与池大小无关。
    · 同一 tick 顺带折叠本地 usage（不联网），把当日 token/credit 推给守卫。
    · 刷新失败的账号停放 6h（§2.5），避免对着坏凭证连打。
    · **惰性启动**：只有某个账号的护栏阈值 > 0 时（apply_quota_limits 内）才
      start；阈值全 0 时线程根本不存在，测试 / CLI 场景零副作用。
    """

    def __init__(self, pool, interval_seconds=None,
                 ttl_seconds=None, log=None):
        self.pool = pool
        self.interval_seconds = max(60, int(interval_seconds
                                        or _QUOTA_REFRESHER_INTERVAL))
        hours = DEFAULT_CREDITS_REFRESH_HOURS if ttl_seconds is None \
            else float(ttl_seconds) / 3600.0
        self.ttl_seconds = max(0.0, float(hours) * 3600.0)
        self._log = log or (lambda *a, **k: None)
        self._parked = {}
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None

    # -- 生命周期 ---------------------------------------------------------
    def start(self):
        if self._thread and self._thread.is_alive():
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._run,
                                        name="credits-refresher", daemon=True)
        self._thread.start()
        return True

    def stop(self, timeout=2.0):
        self._stop.set()
        self._wake.set()
        th = self._thread
        if th and th.is_alive():
            th.join(timeout=timeout)
        self._thread = None

    def wake(self):
        self._wake.set()

    def running(self):
        return bool(self._thread and self._thread.is_alive())

    # -- 单次动作 ---------------------------------------------------------
    def stalest_account(self, now=None):
        """最陈旧且已过 TTL 的可用账号；都没过期 -> None（不产生上游调用）。"""
        now = now if now is not None else time.time()
        best, best_age = None, None
        for a in (self.pool.accounts if self.pool else []):
            if not a.enabled or not a.access_token:
                continue
            if self._parked.get(a.uid, 0.0) > now:
                continue
            cred = a.credits if isinstance(a.credits, dict) else {}
            updated = cred.get("updated_at")
            try:
                age = now - float(updated)
            except (TypeError, ValueError):
                age = float("inf")      # 从未取过余额 -> 最陈旧（优先填充）
            if age < self.ttl_seconds:
                continue
            if best_age is None or age > best_age:
                best, best_age = a, age
        return best

    def refresh_once(self, now=None):
        """刷新一个账号的余额；无可刷账号返回 None（零上游调用）。"""
        if self.ttl_seconds <= 0:
            return None                 # TTL=0 -> 关闭（§5.5 E1）
        acct = self.stalest_account(now=now)
        if acct is None:
            return None
        try:
            res = acct.fetch_credits()
        except Exception as exc:
            res = {"ok": False, "error": str(exc)}
        if isinstance(res, dict) and res.get("ok"):
            self._parked.pop(acct.uid, None)
            self._log_msg("quota refresher: %s refreshed (remain=%s)"
                          % (acct.uid[:8], (acct.credits or {}).get("remain")))
        else:
            self._parked[acct.uid] = (now if now is not None else time.time()) \
                + _QUOTA_REFRESHER_PARK
            self._log_msg("quota refresher: %s failed -> parked 6h (%s)"
                          % (acct.uid[:8], (res or {}).get("error")),
                          level="WARN")
        return res

    def _log_msg(self, msg, level=None):
        """容错日志：AccountPool.log 默认是 `lambda msg: None`（只吃一个位置参数），

        直接传 level= 会 TypeError —— 那会在 daemon 线程里静默炸掉整条刷新链路
        （实测踩过：线程 Exception 后 running() 变 False）。
        """
        try:
            self._log(msg, level=level)
            return
        except TypeError:
            pass
        except Exception:
            return
        try:
            self._log(msg)
        except Exception:
            pass

    def _run(self):
        while not self._stop.is_set():
            try:
                self.refresh_once()
                if self.pool is not None:
                    self.pool.refresh_daily_usage()
            except Exception as exc:
                self._log_msg("quota refresher tick failed: %s" % exc,
                              level="WARN")
            self._wake.wait(self.interval_seconds)
            self._wake.clear()


class CreditsUsageRefresher(object):
    """使用驱动的余额刷新（issue #43）——**与额度护栏完全解耦**。

    为什么需要它：护栏刷新器（CreditsRefresher）只在某个阈值 > 0 时才启动，
    且 30min/tick + 12h TTL；阈值全 0（默认）时线程根本不存在，于是连续使用
    两三个小时后面板余额一直是旧快照，界面上也没有「最后成功查询时间」。

    语义（报告者的验收口径）：
      · mark_used(uid)：请求线程只做一次轻量打标（dict 写 + Event.set），
        **绝不**在请求路径上等余额接口；
      · 同一窗口内多次使用 -> 合并成一次查询；
      · 无待刷新 -> 不发请求（空闲不轮询）；用完之后仍会补完最后一次；
      · 失败保留旧快照与最后成功时间，退避重试（60s 起，翻倍到 15min）；
      · 手动刷新覆盖到了同一账号 -> 清 pending，避免紧接着重复查询。
    """

    def __init__(self, pool, interval_seconds=None, log=None, fetcher=None):
        self.pool = pool
        self.interval_seconds = max(30, int(interval_seconds
                                            or _CREDITS_USAGE_INTERVAL))
        self._log = log or (lambda *a, **k: None)
        self._fetcher = fetcher or (lambda acc: self._default_fetch(acc))
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None

    @staticmethod
    def _default_fetch(acc):
        return acc.fetch_credits()

    # -- 生命周期 ---------------------------------------------------------
    def start(self):
        if self._thread and self._thread.is_alive():
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._run,
                                        name="credits-usage-refresher",
                                        daemon=True)
        self._thread.start()
        return True

    def stop(self, timeout=2.0):
        self._stop.set()
        self._wake.set()
        th = self._thread
        if th and th.is_alive():
            th.join(timeout=timeout)
        self._thread = None

    def wake(self):
        self._wake.set()

    def running(self):
        return bool(self._thread and self._thread.is_alive())

    # -- 打标 / 选择 -------------------------------------------------------
    def _find(self, uid):
        for a in (self.pool.accounts if self.pool else []):
            if a.uid == uid:
                return a
        return None

    def mark_used(self, uid, now=None):
        """请求线程调用：只打标，不联网。返回是否真的产生了待刷新。"""
        if not uid:
            return False
        a = self._find(uid)
        if a is None or not a.enabled or not a.access_token:
            return False
        if getattr(a, "credits_pending", False):
            return False          # 已待刷新：本窗口内多次使用合并成一次
        a.credits_pending = True
        a.credits_refresh_state = "pending"
        self._wake.set()
        return True

    def mark_manual(self, uid, at=None):
        """手动查询成功后调用：清掉 pending 与失败计数（避免立刻重复查）。"""
        a = self._find(uid)
        if a is None:
            return False
        a.credits_pending = False
        a.credits_refresh_state = "idle"
        a.credits_refresh_error = ""
        a.credits_refresh_failures = 0
        a.credits_refresh_next = 0.0
        return True

    def due_accounts(self, now=None):
        """本 tick 该刷新的账号（到期 + 启用 + 有凭证）。"""
        now = time.time() if now is None else now
        out = []
        for a in (self.pool.accounts if self.pool else []):
            if not getattr(a, "credits_pending", False):
                continue
            if not a.enabled or not a.access_token:
                continue
            if float(getattr(a, "credits_refresh_next", 0) or 0) > now:
                continue
            out.append(a)
            if len(out) >= _CREDITS_USAGE_MAX_PER_TICK:
                break
        return out

    def tick_once(self, now=None):
        """刷新一批待处理账号；返回 {"refreshed": n, "failed": n}。"""
        now = time.time() if now is None else now
        stats = {"refreshed": 0, "failed": 0}
        for a in self.due_accounts(now):
            a.credits_refresh_state = "refreshing"
            try:
                res = self._fetcher(a)
            except Exception as exc:
                res = {"ok": False, "error": str(exc)}
            ok = bool(isinstance(res, dict) and res.get("ok"))
            if ok:
                a.credits_pending = False
                a.credits_refresh_state = "idle"
                a.credits_refresh_error = ""
                a.credits_refresh_failures = 0
                a.credits_refresh_next = 0.0
                stats["refreshed"] += 1
            else:
                failures = int(getattr(a, "credits_refresh_failures", 0) or 0) + 1
                a.credits_refresh_failures = failures
                a.credits_refresh_state = "failed"
                err = ""
                if isinstance(res, dict):
                    err = str(res.get("error") or "")
                a.credits_refresh_error = err[:200]
                delay = min(_CREDITS_USAGE_BACKOFF_MAX,
                            _CREDITS_USAGE_BACKOFF_BASE * (2 ** (failures - 1)))
                a.credits_refresh_next = time.time() + delay
                stats["failed"] += 1
                self._log("credits refresh failed for %s (try %d): %s - retry in %ds"
                          % (a.uid[:8], failures, err[:120], delay))
        return stats

    def _run(self):
        while not self._stop.is_set():
            try:
                self.tick_once()
            except Exception as exc:
                self._log("credits usage refresher tick failed: %s" % exc)
            self._wake.wait(self.interval_seconds)
            self._wake.clear()


class AccountPool(object):
    def __init__(self, directory, log=None):
        global _ACTIVE_POOL
        self.dir = directory
        self.log = log or (lambda msg: None)
        self.accounts = []
        self.logins = {}
        self._lock = threading.RLock()
        self._cursor = 0
        self.affinity = SessionAffinity()
        # task-61：后台余额刷新（惰性启动，见 apply_quota_limits）
        self.quota_refresher = None
        # task-61 §2.6：临期加权轮询的运行期状态（uid -> 当前累计权重，重启即重置）
        self._expiry_weights = {}
        _ACTIVE_POOL = self

    def load(self):
        with self._lock:
            self.accounts = []
            if not os.path.isdir(self.dir):
                return self.accounts
            for name in sorted(os.listdir(self.dir)):
                if not name.endswith(".json"):
                    continue
                if name in ("settings.json", "active_realm.json"):
                    continue
                path = os.path.join(self.dir, name)
                try:
                    with open(path, encoding="utf-8") as fh:
                        account = Account(json.load(fh), path)
                except Exception as exc:
                    self.log("account %s unreadable: %s" % (name, exc))
                    continue
                if account.uid:
                    self.accounts.append(account)
            return self.accounts

    def list_public(self, realm=None):
        with self._lock:
            accs = self.accounts if (not realm or realm == "all") else \
                [a for a in self.accounts if a.realm == realm]
            return [a.public() for a in accs]

    def get(self, uid):
        with self._lock:
            for account in self.accounts:
                if account.uid == uid:
                    return account
        return None

    def add(self, account):
        with self._lock:
            existing = self.get(account.uid)
            if existing is not None:
                account.added_at = existing.added_at
                account.path = existing.path
                if not account.credits and existing.credits:
                    account.credits = existing.credits
                if not account.plan and existing.plan:
                    account.plan = existing.plan
                if not account.last_checkin and existing.last_checkin:
                    account.last_checkin = existing.last_checkin
                if not account.personal_token and existing.personal_token:
                    account.personal_token = existing.personal_token
                self.accounts[self.accounts.index(existing)] = account
            else:
                self.accounts.append(account)
            account.save(self.dir)
            return account

    def remove(self, uid):
        with self._lock:
            account = self.get(uid)
            if account is None:
                return False
            account.delete()
            self.accounts.remove(account)
            return True

    # -- 限额护栏（task-61）------------------------------------------------
    def apply_quota_limits(self, limits=None, usage=None, free_models=None):
        """一次 apply 推送四条护栏的配置 + 计数 + free 名单（R3 必须同批）。

        limits   : {"reserve_credits","daily_token_limit","daily_credit_limit",
                    "model_daily_token_limit"}（缺省键保持原值；<=0 = 关闭）
        usage    : fold_daily_usage() 的产物。**None 保留上次计数**——设置改动
                   不会把已折叠的计数打回未知（D2）；传 dict 时，有账号但今日
                   无记录的 uid 折叠为 **0 而不是 None**（D4）。
        free_models : 与守卫同批推送的免费名单（R3：空集会拦住全部模型）。
        返回状态翻转列表（was/now），调用方据此只打印翻转（D3）。
        """
        flips = []
        with _QUOTA_LOCK:
            accounts = list(self.accounts)
            # D3：翻转比对覆盖四条护栏（含 reserve 与 free 名单改动），
            # 快照必须取在应用之前 —— 只有状态真的变化才产生一条日志。
            was_state = {a.uid: (a.reserve_blocked(), a.daily_limit_blocked(),
                                 a.credit_limit_reached(),
                                 bool(a.blocked_model_names()))
                         for a in accounts}
            if limits:
                for key in ("reserve_credits", "daily_token_limit",
                            "daily_credit_limit", "model_daily_token_limit",
                            "expiring_window_days"):
                    if key not in limits:
                        continue
                    try:
                        val = max(0, int(limits.get(key) or 0))
                    except (TypeError, ValueError):
                        val = 0
                    for a in accounts:
                        setattr(a, key, val)
            if free_models is not None:
                names = frozenset(str(x) for x in (free_models or ()) if x)
                for a in accounts:
                    a.free_models = names
            if usage is not None:
                tokens = usage.get("tokens") or {}
                credits = usage.get("credits") or {}
                per_model = usage.get("model_tokens") or {}
                for a in accounts:
                    uid = a.uid
                    a.daily_tokens_today = int(tokens.get(uid, 0) or 0)
                    a.daily_credits_today = float(credits.get(uid, 0) or 0)
                    a.model_daily_tokens = dict(per_model.get(uid) or {})
            for a in accounts:
                now = (a.reserve_blocked(), a.daily_limit_blocked(),
                       a.credit_limit_reached(),
                       bool(a.blocked_model_names()))
                if was_state.get(a.uid) != now:
                    flips.append({"uid": a.uid, "was": was_state.get(a.uid),
                                  "now": now})
            needs_refresher = any(
                a.reserve_credits or a.daily_token_limit
                or a.daily_credit_limit or a.model_daily_token_limit
                for a in accounts)
            credit_on = any(a.daily_credit_limit > 0 for a in accounts)
            free_count = len(accounts[0].free_models) if accounts else 0
        for f in flips:
            self.log("quota guard flip: %s was=%s now=%s"
                     % (f["uid"][:8], f["was"], f["now"]))
        if credit_on and free_count == 0:
            self.log("quota guard: daily_credit_limit 已启用但 free 名单为空 —— "
                     "credit 守卫会拦住**全部**模型（R3，请与守卫同批推送 is_free）",
                     level="WARN")
        if needs_refresher:
            self._ensure_quota_refresher()
        return flips

    def apply_settings(self, limits_data=None, free_models=None):
        """把运行设置里的护栏阈值推给全池（启动时 + 每次设置保存后各一次）。

        limits_data: qoder_settings.limits_data() 的三级作用域结构
                     （{"reserve_credits": {"global":0,"intl":0,"cn":0}, ...}）；
                     缺键/None 按 0 处理（= 关闭该护栏）。
        free_models: is_free 名单，**与阈值同批推送**（R3：空集会拦住全部模型）；
                     None = 保持池内现值（不覆盖）。

        逐账号按自己的 realm 取值（realm override > global），再复用
        apply_quota_limits 做翻转日志与「惰性启动后台刷新」。
        """
        keys = ("reserve_credits", "daily_token_limit", "daily_credit_limit",
                "model_daily_token_limit", "expiring_window_days")
        limits = limits_data if isinstance(limits_data, dict) else {}
        flips = []
        with _QUOTA_LOCK:
            accounts = list(self.accounts)
            # 快照取在设值之前：这样通过设置页改阈值也会产生翻转日志（D3）。
            was_state = {a.uid: (a.reserve_blocked(), a.daily_limit_blocked(),
                                 a.credit_limit_reached(),
                                 bool(a.blocked_model_names()))
                         for a in accounts}
            for a in accounts:
                for key in keys:
                    setattr(a, key, _scope_limit_value(limits.get(key), a.realm))
            for a in accounts:
                now = (a.reserve_blocked(), a.daily_limit_blocked(),
                       a.credit_limit_reached(),
                       bool(a.blocked_model_names()))
                if was_state.get(a.uid) != now:
                    flips.append({"uid": a.uid, "was": was_state.get(a.uid),
                                  "now": now})
        for f in flips:
            self.log("quota guard flip (settings): %s was=%s now=%s"
                     % (f["uid"][:8], f["was"], f["now"]))
        return self.apply_quota_limits(free_models=free_models)

    def refresh_daily_usage(self, log_path=None):
        """折叠本地 usage -> 各账号当日计数（只读文件，不联网）。

        log_path 优先级（与 fold_daily_usage 同一套）：
          显式参数 > qoder_accounts.USAGE_LOG_OVERRIDE > QD_PROXY_USAGE_DIR
          > <包目录>/usage/usage.jsonl（与 qoder_proxy.USAGE_DIR 默认同源）。
        生产链路（CreditsRefresher）不传参 —— 夹具注入请用
        `QD_PROXY_USAGE_DIR=<dir>` 或 `qoder_accounts.USAGE_LOG_OVERRIDE=<file>`。
        """
        data = fold_daily_usage(log_path)
        self.apply_quota_limits(usage=data)
        return data

    def quota_enabled(self):
        """是否有账号开着至少一条护栏（决定 refresher 是否值得启动）。"""
        return any(a.reserve_credits or a.daily_token_limit
                   or a.daily_credit_limit or a.model_daily_token_limit
                   for a in self.accounts)

    def _ensure_quota_refresher(self):
        """惰性启动后台刷新（G1 的唯一周期性恢复路径，不能省）。"""
        if self.quota_refresher is None:
            self.quota_refresher = CreditsRefresher(self, log=self.log)
        if self.quota_enabled() and not self.quota_refresher.running():
            self.quota_refresher.start()
            self.log("quota refresher started (tick=%ds ttl=%.1fh)"
                     % (self.quota_refresher.interval_seconds,
                        self.quota_refresher.ttl_seconds / 3600.0))
        return self.quota_refresher

    # -- 选择与健康 --------------------------------------------------------
    def count_ready(self, realm=None, model=None):
        with self._lock:
            snapshot = [a for a in self.accounts if not realm or a.realm == realm]
        return sum(1 for a in snapshot if a.enabled and a.access_token and
                   a.ready(model=model))

    def pick_for_session(self, realm=None, session_key=None, exclude=None, model=None):
        exclude = exclude or set()
        if session_key:
            bound_uid = self.affinity.get(session_key)
            if bound_uid and bound_uid not in exclude:
                account = self.get(bound_uid)
                if account and account.realm == realm and account.ready(model=model):
                    return account
                self.affinity.unbind(session_key)
        account = self.pick(realm=realm, exclude=exclude, model=model)
        if account and session_key:
            self.affinity.bind(session_key, account.uid)
        return account

    def _expiry_weight(self, account):
        """临期权重 = max(1, int(round(window - soonest_days)) + 1)（wb:2420 同式）。

        离过期越近权重越大（线性）；地板 1 防止窗口边缘的号被饿死。
        window <= 0 或无法判断（soonest is None）-> 1，等同普通轮转。
        """
        window = account.expiring_window_days or 0
        if window <= 0:
            return 1
        soonest = account.soonest_expiring_days()
        if soonest is None:
            return 1
        try:
            return max(1, int(round(float(window) - float(soonest))) + 1)
        except (TypeError, ValueError):
            return 1

    def _weighted_pick(self, candidates):
        """nginx smooth weighted round-robin（wb:2422 同算法）。

        每轮各账号累加权重、最大者胜、胜者回吐本轮总权重 —— 等权时退化为严格轮转。
        状态在 self._lock 内读写；_expiry_weights 是运行期状态（重启重置）。
        """
        if not candidates:
            return None
        weights = [self._expiry_weight(a) for a in candidates]
        total = sum(weights) or 1
        with self._lock:
            state = self._expiry_weights
            best_i, best_cur = 0, None
            for i, a in enumerate(candidates):
                cur = state.get(a.uid, 0) + weights[i]
                if best_cur is None or cur > best_cur:
                    best_i, best_cur = i, cur
            winner = candidates[best_i]
            state[winner.uid] = best_cur - total
            if len(state) > 256:        # 防无界增长：清掉不在本轮候选里的 uid
                keep = {a.uid for a in candidates}
                for uid in [u for u in state if u not in keep]:
                    state.pop(uid, None)
        return winner

    def _pick_expiring_first(self, realm=None, exclude=None, model=None):
        """窗口内（临期）的可用账号里做加权轮询；没有候选 -> None（由 pick 回落）。"""
        exclude = exclude or set()
        with self._lock:
            snapshot = [a for a in self.accounts if not realm or a.realm == realm]
        cands = [a for a in snapshot
                 if a.uid not in exclude and a.ready(model=model)
                 and a.in_expiring_window()]
        if not cands:
            return None
        return self._weighted_pick(cands)

    def _rotate_pick(self, realm=None, exclude=None, model=None):
        """纯 round-robin + 两轮降权扫描（task-74 三族之三）。

        第一轮先跳过降权窗口内的账号（degraded()），第二轮不跳过——降权号仍然
        可用（不是封禁），只是没有更健康候选时才回落使用（不楔死）。无降权号
        的池行为与原实现逐字节一致。
        """
        exclude = exclude or set()
        with self._lock:
            snapshot = [a for a in self.accounts if not realm or a.realm == realm]
            start = self._cursor
        total = len(snapshot)
        if total == 0:
            return None
        now = time.time()
        for skip_degraded in (True, False):
            for offset in range(total):
                index = (start + offset) % total
                account = snapshot[index]
                if account.uid in exclude:
                    continue
                if skip_degraded and account.degraded(now):
                    continue
                if account.ready(model=model):
                    with self._lock:
                        self._cursor = (index + 1) % total
                    return account
        return None

    def pick(self, realm=None, exclude=None, model=None):
        """选号：临期积分优先（平滑加权），否则回落普通轮转。

        `expiring_window_days` 默认 0（R7）-> in_expiring_window() 恒 False ->
        _pick_expiring_first 返回 None -> 与旧的纯 round-robin **完全一致**。
        """
        account = self._pick_expiring_first(realm=realm, exclude=exclude,
                                            model=model)
        if account is not None:
            return account
        return self._rotate_pick(realm=realm, exclude=exclude, model=model)

    def representative(self, realm=None):
        with self._lock:
            candidates = [a for a in self.accounts if not realm or a.realm == realm]
            for account in candidates:
                if account.access_token:
                    return account
            return candidates[0] if candidates else None

    def set_enabled(self, uid, enabled):
        account = self.get(uid)
        if account is None:
            return None
        account.enabled = bool(enabled)
        if enabled:
            account.clear_error()
        account.save(self.dir)
        return account.public()

    def set_all_enabled(self, enabled, realm=None):
        with self._lock:
            for account in self.accounts:
                if realm and account.realm != realm:
                    continue
                account.enabled = bool(enabled)
                if enabled:
                    account.clear_error()
                account.save(self.dir)

    # -- 导入 / 导出 -------------------------------------------------------
    def preview_import_rows(self, rows, realm=None, overwrite=False):
        """报告 import_rows() 会做什么，不触碰账号池（Dry-Run）。"""
        preview = {"added": [], "updated": [], "skipped": [], "invalid": []}
        known = {a.uid for a in self.accounts}
        seen = set()
        for index, row in enumerate(rows):
            try:
                kwargs = normalise_import_row(row, realm=realm)
            except Exception as exc:
                preview["invalid"].append({"index": index + 1, "reason": str(exc)})
                continue
            uid = kwargs["uid"]
            if uid in seen:
                preview["skipped"].append({"uid": uid,
                                           "reason": "duplicate inside the document"})
            elif uid in known and not overwrite:
                preview["skipped"].append({"uid": uid, "reason": "already exists"})
            elif uid in known:
                preview["updated"].append(uid)
            else:
                preview["added"].append(uid)
            seen.add(uid)
        return preview

    def import_rows(self, rows, realm=None, overwrite=False):
        """从导出/外部文档批量导入账号。

        返回报告：added / updated / skipped / invalid。
        一行解析失败不影响其余行；全部解析通过才写盘。
        """
        added, updated, skipped, invalid = [], [], [], []
        seen = set()
        for index, row in enumerate(rows):
            try:
                kwargs = normalise_import_row(row, realm=realm)
            except Exception as exc:
                invalid.append({"index": index + 1, "reason": str(exc)})
                continue
            uid = kwargs["uid"]
            if uid in seen:
                skipped.append({"uid": uid,
                                "reason": "duplicate inside the document"})
                continue
            seen.add(uid)
            existing = self.get(uid) is not None
            if existing and not overwrite:
                skipped.append({"uid": uid, "reason": "already exists"})
                continue
            try:
                self.add(Account(kwargs))
            except Exception as exc:
                invalid.append({"index": index + 1, "reason": str(exc)})
                continue
            (updated if existing else added).append(uid)
        return {
            "added": added,
            "updated": updated,
            "skipped": skipped,
            "invalid": invalid,
        }

    # -- OAuth 设备授权登录 ------------------------------------------------
    @staticmethod
    def _local_machine_id(realm):
        """读取本机官方客户端的 machine_id（优先，保持设备一致），缺则生成。"""
        cfg = get_realm_config(realm)
        home = os.path.join(os.path.expanduser("~"), cfg["home_dir"], ".auth")
        p = os.path.join(home, "machine_id")
        try:
            with open(p, encoding="utf-8") as fh:
                mid = fh.read().strip()
            if mid:
                return mid
        except Exception:
            pass
        return str(uuid.uuid4())

    def start_login(self, realm="cn", platform="CLI"):
        """构造 PKCE 设备授权 URL（浏览器打开完成授权）。

        双区 URL 参数差异（官方逆向）：
          CN   : challenge, challenge_method, nonce(带横线), redirect_uri,
                 client_id, machine_id
          Intl : challenge, challenge_method, nonce(32-hex), client_id,
                 machine_id（新协议带 client_id/machine_id、不带 redirect_uri）
        """
        cfg = get_realm_config(realm)
        verifier, challenge = _make_pkce()
        nonce = uuid.uuid4().hex if not cfg["nonce_dashed"] else str(uuid.uuid4())
        q = {
            "challenge": challenge,
            "challenge_method": "S256",
            "nonce": nonce,
        }
        if cfg.get("send_redirect_uri"):
            q["redirect_uri"] = cfg["redirect_uri"]
        if cfg.get("send_client_id"):
            q["client_id"] = cfg["client_id"]
            q["machine_id"] = self._local_machine_id(realm)
        auth_url = cfg["website"] + "/device/selectAccounts?" + urllib.parse.urlencode(q)
        state = "qd-%d" % time.time_ns()
        with self._lock:
            self.logins[state] = {
                "created": time.time(),
                "verifier": verifier,
                "nonce": nonce,
                "region": realm,
                "platform": platform,
            }
        return {"state": state, "authUrl": auth_url, "realm": realm,
                "platform": platform}

    def poll_login(self, state):
        state = str(state or "").strip()
        with self._lock:
            info = self.logins.get(state)
        if not info:
            return {"status": "unknown",
                    "message": "state not recognised - start the login again"}
        if time.time() - info["created"] > LOGIN_TTL_SECONDS:
            with self._lock:
                self.logins.pop(state, None)
            return {"status": "expired", "message": "login window expired - start again"}
        realm = info.get("region") or "cn"
        cfg = get_realm_config(realm)
        q = urllib.parse.urlencode({
            "nonce": info["nonce"],
            "verifier": info["verifier"],
            "challenge_method": "S256",
        })
        url = cfg["openapi"] + PATH_DEVICE_POLL + "?" + q
        validate_public_http_url(url)
        # task-80 判定：本调用**不带账号凭证**（device flow 的 nonce/verifier
        # challenge，无 Authorization），且依赖原始 HTTPError 语义
        # （404/202 = 尚未授权 -> pending）——**故意保留直连**，不收口。
        req = urllib.request.Request(url, method="GET", headers={
            "Accept": "application/json",
            "User-Agent": "QoderWork",
        })
        try:
            with _opener_for_request().open(req, timeout=20) as resp:
                raw = resp.read().decode("utf-8")
                status = resp.status
        except urllib.error.HTTPError as exc:
            # 404 / 202 = 用户尚未完成授权（继续轮询）
            if exc.code in (404, 202):
                return {"status": "pending",
                        "message": "等待浏览器完成 Qoder 设备授权"}
            try:
                body = exc.read().decode("utf-8", "replace")
            except Exception:
                body = ""
            return {"status": "error", "message": "poll http %d %s" % (exc.code, body[:160])}
        except Exception as exc:
            return {"status": "pending", "message": "poll error: %s" % exc}
        if status in (404, 202):
            return {"status": "pending", "message": "等待浏览器完成 Qoder 设备授权"}
        try:
            data = json.loads(raw)
        except Exception:
            return {"status": "pending", "message": "waiting for grant"}
        token = data.get("token") or data.get("device_token") or ""
        if not token:
            return {"status": "pending", "message": "waiting for token"}

        uid = str(data.get("user_id") or "")
        nickname = ""
        # 拉取 userinfo 补全昵称/用户类型（尽力而为，不阻塞入库）
        try:
            ui_url = cfg["openapi"] + PATH_USERINFO
            # task-80 收口（P1-2 第一步）：本调用**携带账号凭证**
            # （Authorization: Bearer <token>），必须走统一出站函数 http_json
            # ——它是「所有带凭证调用都过同一出口」的唯一保证点（未来若上
            # 出站代理/审计，自动覆盖）。语义保持：retries=1（原实现单次）、
            # URL 校验由 http_json 内部执行、失败仍由外层 except 兜底。
            ui = http_json(ui_url, method="GET", headers={
                "Accept": "application/json",
                "User-Agent": CLIENT_UA,
                "Authorization": "Bearer " + token,
            }, timeout=15, retries=1)
            uid = str(ui.get("id") or uid)
            nickname = str(ui.get("name") or "")
            user_type = str(ui.get("user_type") or "") or DEFAULT_USER_TYPE
            org_id = str(ui.get("organization_id") or "")
            org_name = str(ui.get("organization_name") or "")
        except Exception:
            user_type, org_id, org_name = DEFAULT_USER_TYPE, "", ""

        account = Account({
            "uid": uid or ("q-" + uuid.uuid4().hex[:24]),
            "nickname": nickname or ("u" + (uid[-8:] if uid else "")),
            "domain": cfg["domain"],
            "realm": realm,
            "platform": info.get("platform") or "CLI",
            "accessToken": token,
            "refreshToken": data.get("refresh_token") or "",
            "expiresAt": _device_expiry(data),
            "source": "oauth",
            "enabled": True,
            "userType": user_type,
            "organizationId": org_id,
            "organizationName": org_name,
        })
        self.add(account)
        with self._lock:
            self.logins.pop(state, None)
        return {"status": "ok", "account": account.public()}

    def cancel_login(self, state):
        with self._lock:
            return self.logins.pop(state, None) is not None

    # -- PAT 导入 ----------------------------------------------------------
    def import_pat(self, pat, realm="cn"):
        """导入 pt- 个人访问令牌：交换 jobToken 并拉取身份后入库。"""
        pat = str(pat or "").strip()
        if not pat.startswith("pt-"):
            raise ValueError("PAT must start with pt-")
        cfg = get_realm_config(realm)
        data = http_json(cfg["openapi"] + PATH_JOB_EXCHANGE,
                         data=json.dumps({"personal_token": pat}).encode(),
                         method="POST",
                         headers={"Content-Type": "application/json",
                                  "Accept": "application/json",
                                  "User-Agent": CLIENT_UA},
                         timeout=30)
        token = data.get("token") or ""
        if not token:
            raise ValueError("jobToken exchange returned no token")
        uid, nickname, user_type, org_id, org_name = "", "", DEFAULT_USER_TYPE, "", ""
        try:
            ui = http_json(cfg["openapi"] + PATH_USERINFO, method="GET",
                           headers={"Accept": "application/json",
                                    "User-Agent": CLIENT_UA,
                                    "Authorization": "Bearer " + token},
                           timeout=15, retries=2)
            uid = str(ui.get("id") or "")
            nickname = str(ui.get("name") or "")
            user_type = str(ui.get("user_type") or "") or DEFAULT_USER_TYPE
            org_id = str(ui.get("organization_id") or "")
            org_name = str(ui.get("organization_name") or "")
        except Exception:
            pass
        if data.get("expires_in"):
            exp = int(time.time() + int(data["expires_in"]) / 1000)
        else:
            exp = int(time.time()) + 24 * 3600
        account = Account({
            "uid": uid or ("p-" + uuid.uuid4().hex[:24]),
            "nickname": nickname or ("u" + (uid[-8:] if uid else uid[:8])),
            "domain": cfg["domain"],
            "realm": realm,
            "platform": "CLI",
            "accessToken": token,
            "refreshToken": data.get("refresh_token") or "",
            "personalToken": pat,
            "expiresAt": exp,
            "source": "pat",
            "enabled": True,
            "userType": user_type,
            "organizationId": org_id,
            "organizationName": org_name,
        })
        self.add(account)
        return account


def _make_pkce():
    """RFC 7636 S256: (verifier, challenge)。"""
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
    raw = os.urandom(64)
    verifier = "".join(alphabet[b % len(alphabet)] for b in raw)
    challenge = __import__("hashlib").sha256(verifier.encode("ascii")).digest()
    import base64 as _b64
    return verifier, _b64.urlsafe_b64encode(challenge).decode("ascii").rstrip("=")


# ---------------------------------------------------------------------------
# 本机已登录凭证的只读探测与导入（与 wb 网关的桌面扫描同构，双区都支持）
#
# 两类官方存储：
#   1. 桌面 App（Electron）： %APPDATA%\com.qoder[.cn].app.stable\auth.v1.dat
#      布局 "v10" + AES-256-GCM；密钥在同目录 Local State 的
#      os_crypt.encrypted_key（DPAPI 保护）-> 剥 "DPAPI" 前缀 -> DPAPI 解出。
#      明文 schema: {schemaVersion, token(dt-), refreshToken(drt-), expiresAt,
#                    user:{id,name,email,...}}
#   2. CLI/官方客户端： ~/.qoder[.cn]/.auth/user[.{profile}]
#      AES-128-CBC key=iv=machine_id 前 16 字符，标准 Base64（strict padding）；
#      明文 UserInfo JSON（或明文以 "{" 开头的兼容形态）。
# 扫描全程只读；看板两步确认后才写入网关账号池。
# ---------------------------------------------------------------------------
def _read_chromium_os_crypt_key(app_dir):
    """Local State.os_crypt.encrypted_key -> DPAPI 解出的 32 字节 AES key。"""
    import base64 as _b64
    from qoder_sign import dpapi_unprotect
    p = os.path.join(app_dir, "Local State")
    with open(p, encoding="utf-8") as fh:
        state = json.load(fh)
    ek = (state.get("os_crypt") or {}).get("encrypted_key")
    if not ek:
        raise RuntimeError("Local State has no os_crypt.encrypted_key")
    blob = _b64.b64decode(ek)
    if blob[:5] != b"DPAPI":
        raise RuntimeError("unexpected encrypted_key header %r" % blob[:5])
    return dpapi_unprotect(blob[5:])


def _roaming_app_dir(cfg):
    base = os.environ.get("APPDATA") or os.path.join(
        os.path.expanduser("~"), "AppData", "Roaming")
    return os.path.join(base, cfg["app_dir"])


def _load_app_auth(realm):
    """解出桌面 App auth.v1.dat 的明文 dict；失败抛异常。"""
    from qoder_sign import chromium_decrypt_v10
    cfg = get_realm_config(realm)
    app_dir = _roaming_app_dir(cfg)
    key = _read_chromium_os_crypt_key(app_dir)
    with open(os.path.join(app_dir, "auth.v1.dat"), "rb") as fh:
        blob = fh.read()
    plain = chromium_decrypt_v10(blob, key)
    data = json.loads(plain.decode("utf-8"))
    if not isinstance(data, dict) or not data.get("token"):
        raise RuntimeError("auth.v1.dat has unexpected schema")
    return data


def _load_cli_user(realm, path, machine_key):
    """解 CLI 端 ~/.qoder*/.auth/user（AES-128-CBC）或明文兼容形态。"""
    from qoder_sign import aes_cbc_decrypt
    with open(path, encoding="utf-8") as fh:
        raw = fh.read().strip()
    if raw.startswith("{"):
        return json.loads(raw)
    key = (machine_key or "")[:16].encode("utf-8")
    if len(key) != 16:
        raise RuntimeError("machine_id shorter than 16 bytes")
    pt = aes_cbc_decrypt(base64.b64decode(raw), key, key)
    return json.loads(pt.decode("utf-8"))


def scan_desktop_credentials():
    """只读探测本机双区已登录凭证。返回候选列表（不含任何明文令牌）。"""
    found = []
    for realm in ("intl", "cn"):
        cfg = get_realm_config(realm)
        # 1) 桌面 App (auth.v1.dat)
        item = {
            "kind": "app",
            "path": os.path.join(_roaming_app_dir(cfg), "auth.v1.dat"),
            "file": "auth.v1.dat",
            "realm": realm,
            "realmName": cfg["name"],
            "domain": cfg["domain"],
            "readable": False,
            "valid": False,
            "uid": "",
            "nickname": "",
            "expiresAt": 0,
            "error": "",
        }
        try:
            data = _load_app_auth(realm)
            user = data.get("user") or {}
            item["readable"] = True
            exp = normalize_epoch(data.get("expiresAt")) or 0
            if not exp and data.get("token"):
                # expiresAt 是 RFC3339 -> normalize_epoch 处理不了，单独解析
                try:
                    import datetime
                    exp = int(datetime.datetime.strptime(
                        str(data["expiresAt"])[:19], "%Y-%m-%dT%H:%M:%S"
                    ).timestamp())
                except Exception:
                    exp = 0
            token_prefix = str(data.get("token") or "")[:3]
            item.update({
                "valid": token_prefix == "dt-" or bool(data.get("refreshToken")),
                "uid": str(user.get("id") or ""),
                "nickname": str(user.get("name") or ""),
                "expiresAt": exp,
                "expiresIn": _human_delta(exp - time.time()) if exp else None,
            })
        except FileNotFoundError:
            item["error"] = "not found (未登录或未安装该版本客户端)"
        except Exception as exc:
            item["error"] = str(exc)
        found.append(item)

        # 2) CLI 端 user / user.{profile}
        auth_dir = os.path.join(os.path.expanduser("~"), cfg["home_dir"], ".auth")
        if os.path.isdir(auth_dir):
            machine_key = ""
            try:
                with open(os.path.join(auth_dir, "machine_id"),
                          encoding="utf-8") as fh:
                    machine_key = fh.read().strip()
            except Exception:
                pass
            try:
                names = [n for n in os.listdir(auth_dir)
                         if n == "user" or n.startswith("user.")]
            except Exception:
                names = []
            for n in names:
                p = os.path.join(auth_dir, n)
                cli_item = {
                    "kind": "cli",
                    "path": p,
                    "file": n,
                    "realm": realm,
                    "realmName": cfg["name"],
                    "domain": cfg["domain"],
                    "readable": False,
                    "valid": False,
                    "uid": "",
                    "nickname": "",
                    "expiresAt": 0,
                    "error": "",
                }
                try:
                    data = _load_cli_user(realm, p, machine_key)
                    token = str(data.get("access_token") or "")
                    cli_item["readable"] = True
                    exp = normalize_epoch(data.get("expire_time"))
                    cli_item.update({
                        "valid": token.startswith(("dt-", "jt-")),
                        "uid": str(data.get("uid") or ""),
                        "nickname": str(data.get("name") or ""),
                        "expiresAt": exp,
                        "expiresIn": _human_delta(exp - time.time()) if exp else None,
                    })
                except Exception as exc:
                    cli_item["error"] = str(exc)
                found.append(cli_item)
    return found


def import_desktop_credential(path=None, realm=None):
    """把扫描到的凭证导入账号池。path=None 时导入扫描到的全部有效项。"""
    if not path:
        imported, errors = [], []
        for item in scan_desktop_credentials():
            if not item.get("valid"):
                continue
            try:
                imported.append(import_desktop_credential(
                    path=item["path"], realm=item["realm"]))
            except Exception as exc:
                errors.append("%s/%s: %s" % (item["realm"], item["file"], exc))
        if errors:
            raise RuntimeError("; ".join(errors[:3]))
        return imported

    # 定位该 path 归属的 realm（按扫描结果匹配；否则按目录名猜）
    target_realm = realm
    matched = None
    for item in scan_desktop_credentials():
        if os.path.abspath(item["path"]) == os.path.abspath(path):
            matched = item
            target_realm = item["realm"]
            break
    if target_realm not in REALM_CONFIGS:
        target_realm = "cn"
    cfg = get_realm_config(target_realm)

    token = refresh = ""
    uid = nickname = ""
    exp = 0
    base = os.path.basename(path)
    if base == "auth.v1.dat":
        data = _load_app_auth(target_realm)
        token = str(data.get("token") or "")
        refresh = str(data.get("refreshToken") or "")
        user = data.get("user") or {}
        uid = str(user.get("id") or "")
        nickname = str(user.get("name") or "")
        try:
            import datetime
            exp = int(datetime.datetime.strptime(
                str(data.get("expiresAt") or "")[:19], "%Y-%m-%dT%H:%M:%S"
            ).timestamp())
        except Exception:
            exp = 0
    else:
        auth_dir = os.path.dirname(path)
        machine_key = ""
        try:
            with open(os.path.join(auth_dir, "machine_id"),
                      encoding="utf-8") as fh:
                machine_key = fh.read().strip()
        except Exception:
            pass
        data = _load_cli_user(target_realm, path, machine_key)
        token = str(data.get("access_token") or "")
        refresh = str(data.get("refresh_token") or "")
        uid = str(data.get("uid") or "")
        nickname = str(data.get("name") or "")
        exp = normalize_epoch(data.get("expire_time"))

    if not token:
        raise RuntimeError("credential has no access token")
    if not uid:
        uid = "d-" + uuid.uuid4().hex[:24]
    account = Account({
        "uid": uid,
        "nickname": nickname or uid[:8],
        "domain": cfg["domain"],
        "realm": target_realm,
        "platform": "CLI",
        "accessToken": token,
        "refreshToken": refresh,
        "expiresAt": exp or (int(time.time()) + 30 * 86400),
        "source": "desktop-app",
        "enabled": True,
    })
    return add_to_pool(account)


# ---------------------------------------------------------------------------
# 导入 / 导出（与 WorkBuddy 网关同构的文档格式）
# ---------------------------------------------------------------------------
EXPORT_FORMAT = "qoder-accounts"
EXPORT_VERSION = 1


def account_to_export(account):
    data = account.to_dict()
    data.pop("path", None)
    return data


def build_export_document(accounts, realm=None, include_secrets=True, uids=None):
    wanted = None
    if uids is not None:
        wanted = {str(u) for u in uids}
    rows = []
    for account in accounts:
        if realm and account.realm != realm:
            continue
        if wanted is not None and account.uid not in wanted:
            continue
        row = account_to_export(account)
        if not include_secrets:
            row.pop("accessToken", None)
            row.pop("refreshToken", None)
            row.pop("personalToken", None)
        rows.append(row)
    return {
        "format": EXPORT_FORMAT,
        "version": EXPORT_VERSION,
        "exportedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
        "count": len(rows),
        "accounts": rows,
    }


def _coerce_account_rows(blob):
    """把任意受支持的容器规整成账号 dict 列表。返回 (rows, error)。"""
    if isinstance(blob, list):
        rows = blob
    elif isinstance(blob, dict) and isinstance(blob.get("accounts"), list):
        rows = blob["accounts"]
    elif isinstance(blob, dict):
        looks_like_account = (
            blob.get("accessToken")
            or isinstance(blob.get("auth"), dict)
            or isinstance(blob.get("account"), dict)
        )
        if not looks_like_account:
            keys = ", ".join(sorted(blob.keys())[:6]) or "none"
            return [], ("not an account document (expected an accounts array, "
                        "a list, or an account object; got keys: %s)" % keys)
        rows = [blob]
    else:
        return [], "expected an object or a list of accounts"

    out = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            return [], "account #%d is not an object" % (index + 1)
        out.append(row)
    if not out:
        return [], "no accounts found in the document"
    return out, ""


def normalise_import_row(row, realm=None):
    """把一行导入数据规整成 Account kwargs；无可用凭证时 raise ValueError。

    运行期字段（cooldownUntil/lastError/credits/lastCheckin/plan）不随导入采信：
    返回值只构造凭证与身份字段，运行期状态一律重置或缺失（白名单构造）。
    """
    auth = row.get("auth") if isinstance(row.get("auth"), dict) else None
    profile = row.get("account") if isinstance(row.get("account"), dict) else None

    def pick(key, default=None):
        for layer in (row, auth, profile):
            if isinstance(layer, dict) and layer.get(key) not in (None, ""):
                return layer.get(key)
        return default

    token = str(pick("accessToken") or "").strip()
    if not token:
        raise ValueError("no accessToken")
    detected = str(realm or pick("realm") or "").strip().lower()
    if detected not in ("cn", "intl"):
        detected = detect_realm_from_domain(pick("domain"))
    cfg = get_realm_config(detected)
    raw_uid = str(pick("uid") or "").strip()
    uid = re.sub(r"[^A-Za-z0-9_-]", "_", raw_uid).strip("_ ")
    if not uid:
        uid = "p-" + uuid.uuid4().hex[:24]
    exp = normalize_epoch(pick("expiresAt"))
    if not exp:
        exp = int(time.time()) + 3600
    return {
        "uid": uid,
        "nickname": str(pick("nickname") or ""),
        "domain": str(pick("domain") or cfg["domain"]),
        "realm": detected,
        "platform": str(pick("platform") or "CLI"),
        "accessToken": token,
        "refreshToken": str(pick("refreshToken") or ""),
        "personalToken": str(pick("personalToken") or ""),
        "expiresAt": exp,
        "source": "import",
        "enabled": True,
        "userType": str(pick("userType") or "") or DEFAULT_USER_TYPE,
        "organizationId": str(pick("organizationId") or ""),
        "organizationName": str(pick("organizationName") or ""),
        "lastError": "",
        "cooldownUntil": 0.0,
    }
