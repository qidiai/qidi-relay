"""管理 API 路由：/api/*。

相对 v3 的增强：
- GET /api/stats/history  小时级用量时间序列（趋势图）
- GET /api/activity       实时请求动态（内存环形缓冲）
"""

import hmac
import json
import logging
import os
import time

import httpx
from flask import Blueprint, jsonify, request

from proxy_core import providers
from proxy_core import cline
from proxy_core import affinity
from proxy_core import minicode
from proxy_core.atomcode import _default_auth_path, all_channels, channels_for, parse_auth_text
from proxy_core.auth import check_admin_auth, hash_password, is_authorized, session_token, verify_password
from proxy_core.keystore import build_litellm_headers

# ─── 登录限速（防爆破）────────────────────────────────────────
import time as _time

_LOGIN_FAILS: dict = {}   # ip -> {"count": int, "until": ts}
_LOGIN_MAX_FAILS = 5      # 连续失败 N 次
_LOGIN_LOCK_SECS = 900    # 锁 15 分钟


def _client_ip() -> str:
    # 取真实客户端 IP（nginx 反代下用 X-Forwarded-For 首跳）
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.remote_addr or "?"


def _login_allowed(ip: str) -> bool:
    rec = _LOGIN_FAILS.get(ip)
    return not (rec and rec.get("until", 0) > _time.time())


def _login_fail(ip: str) -> None:
    rec = _LOGIN_FAILS.get(ip) or {"count": 0, "until": 0}
    rec["count"] += 1
    if rec["count"] >= _LOGIN_MAX_FAILS:
        rec["until"] = _time.time() + _LOGIN_LOCK_SECS
        rec["count"] = 0
    if len(_LOGIN_FAILS) > 10000:  # 防内存膨胀
        _LOGIN_FAILS.clear()
    _LOGIN_FAILS[ip] = rec


def _login_ok(ip: str) -> None:
    _LOGIN_FAILS.pop(ip, None)
from proxy_core.costing import cost_report
from proxy_core.keystore import build_litellm_headers
from proxy_core.router import get_role_map, registry, role_of_model
from proxy_core.water import water
from proxy_core.settings import (
    BASE_DIR,
    LITELLM_API_BASE,
    LOG_FILE,
    load_config,
    save_config,
)
from proxy_core.state import activity, am, api_cache, token_stats

log = logging.getLogger("qidi-relay")

bp = Blueprint("admin", __name__, url_prefix="/api")


# ─── 免费通道串行门（排队语义，方案 v2 §3.2）─────────────────────────

@bp.route("/gate")
@check_admin_auth
def api_gate():
    """免费通道串行门状态：busy=有请求在途（已发起、响应头未到），
    waiting=正在门内排队等待的请求数（等待不计账号失败/冷却，
    契约见 proxy_core.cline.wait_for_slot）。"""
    with cline._gate_cv:
        return jsonify({"free_channel": {
            "busy": cline._gate_busy,
            "waiting": cline._gate_waiters,
        }})


# ─── 会话粘性观测（阶段 B / 方案 v2 §4）──────────────────────────────────────────────

@bp.route("/affinity/stats")
@check_admin_auth
def api_affinity_stats():
    """会话粘性观测（方案 §4）：stuck_sessions=当前粘连
    会话数（内存表锁内快照，24h 过期条目已清出）；
    why_distribution=粘性决策 why 计数分布。"""
    stuck, dist = affinity.snapshot()
    return jsonify({"stuck_sessions": stuck,
                    "why_distribution": dist})


# ─── 账号管理 ────────────────────────────────────────────────────────────────

@bp.route("/atomcode/status")
@check_admin_auth
def api_atomcode_status():
    """AtomCode 通道状态：全部账号的登录态/版本/有效期（不含任何密钥）。"""
    providers.all_providers()  # 确保通道注册表已从 config 构建（懒加载首次触发）
    out = []
    for name, chs in all_channels().items():
        for ch in chs:
            out.append(ch.status())
    return jsonify({"channels": out})


@bp.route("/atomcode/upload", methods=["POST"])
@check_admin_auth
def api_atomcode_upload():
    """上传一份 auth.toml 凭证，注册为新的 AtomCode 账号（多账号轮换）。

    body: {"label": "小号", "content": "<auth.toml 全文>"}；label 缺省用 user_id。
    凭证落盘 auths/atomcode-<label>.toml 并写入 config 的 auth_paths，
    热生效（mtime 重载）。同 label 再传 = 更新该账号凭证。
    """
    import re
    data = request.get_json(force=True, silent=True) or {}
    content = data.get("content")
    if not isinstance(content, str) or not content.strip():
        return jsonify({"error": "缺少 content（auth.toml 全文）"}), 400
    try:
        cred = parse_auth_text(content)
    except ValueError as e:
        return jsonify({"error": f"凭证内容无效: {e}"}), 400

    label = str(data.get("label") or "").strip() or cred.user_id
    label = re.sub(r"[^\w\u4e00-\u9fff.-]+", "_", label)[:24]
    if not label:
        return jsonify({"error": "label 无效"}), 400

    # 找 atomcode provider（type == atomcode 的第一个）
    cfg = load_config()
    provs = cfg.get("providers") or {}
    pname = next((n for n, p in provs.items()
                  if isinstance(p, dict) and p.get("type") == "atomcode"), None)
    if not pname:
        return jsonify({"error": "config 未配置 atomcode provider"}), 400
    p = provs[pname]

    auths_dir = BASE_DIR / "auths"
    auths_dir.mkdir(exist_ok=True)
    rel = f"auths/atomcode-{label}.toml"
    (auths_dir / f"atomcode-{label}.toml").write_text(content, encoding="utf-8")

    # 更新 config：替换同 label 条目，否则追加；迁移旧的单 auth_path / 隐式默认路径
    arr = p.get("auth_paths")
    entries = list(arr) if isinstance(arr, list) else []
    if not entries:
        if isinstance(p.get("auth_path"), str) and p["auth_path"].strip():
            old = p.pop("auth_path")
            entries = [{"path": old, "label": "主号"}]
        else:
            # 旧配置没写 auth_path（运行时用默认 ~/.atomcode/auth.toml）→ 显式固化为主号
            dp = _default_auth_path()
            if dp:
                entries = [{"path": dp, "label": "主号"}]
    new_entries, replaced = [], False
    for e in entries:
        if isinstance(e, dict) and e.get("label") == label:
            new_entries.append({"path": rel, "label": label})
            replaced = True
        elif isinstance(e, dict):
            new_entries.append({"path": e.get("path"), "label": e.get("label") or ""})
        elif isinstance(e, str):
            new_entries.append({"path": e})
    if not replaced:
        new_entries.append({"path": rel, "label": label})
    p["auth_paths"] = new_entries
    save_config(cfg)

    providers.all_providers()  # 立即触发通道池重建
    chs = channels_for(pname)
    activity.add("info", f"AtomCode 账号「{label}」已{'更新' if replaced else '添加'}"
                         f"（user={cred.user_id}，共 {len(chs)} 个账号）", model="atomcode")
    return jsonify({
        "label": label,
        "user_id": cred.user_id,
        "expires_at": cred.expires_at,
        "updated": replaced,
        "account_count": len(chs),
    })


@bp.route("/atomcode/remove", methods=["POST"])
@check_admin_auth
def api_atomcode_remove():
    """移除一个 AtomCode 账号通道。

    body: {"label": ...}。只删 auths/ 下代理托管的凭证文件；
    指向官方 ~/.atomcode/auth.toml 的条目只解绑配置、不动原文件。
    """
    data = request.get_json(force=True, silent=True) or {}
    label = str(data.get("label") or "").strip()
    if not label:
        return jsonify({"error": "缺少 label"}), 400
    cfg = load_config()
    provs = cfg.get("providers") or {}
    removed, managed_file = False, None
    for n, p in provs.items():
        if not (isinstance(p, dict) and p.get("type") == "atomcode"):
            continue
        arr = p.get("auth_paths")
        if not isinstance(arr, list):
            continue
        kept = []
        for e in arr:
            if isinstance(e, dict) and e.get("label") == label:
                removed = True
                path = str(e.get("path") or "")
                if "auths/" in path.replace("\\", "/"):
                    managed_file = path
            else:
                kept.append(e)
        p["auth_paths"] = kept
        break
    if not removed:
        return jsonify({"error": f"账号 {label} 不存在"}), 404
    save_config(cfg)
    if managed_file:
        try:
            (BASE_DIR / managed_file).unlink()
        except OSError:
            pass
    providers.all_providers()  # 立即触发通道池重建
    chs = channels_for(next(n for n, p in provs.items()
                            if isinstance(p, dict) and p.get("type") == "atomcode"))
    activity.add("info", f"AtomCode 账号「{label}」已移除（剩 {len(chs)} 个账号）",
                 model="atomcode")
    return jsonify({"removed": label, "account_count": len(chs)})



# ─── 外部 Provider API Key 自助管理（0927）──────────────────────────────────
# 静态 provider（OpenAI 兼容端点，如 agnes）的多 key 增删：密钥只落 config.json
# （先备份再原子写），providers 侧 mtime 热重载；接口响应一律脱敏，绝不回完整 key。
# AtomCode（auth.toml 签名）/ Cline（客户端登录态镜像）通道无静态 key，不适用。

def _mask_key(k: str) -> str:
    """key 脱敏预览（与 settings.get_api_key_entries 同口径）。"""
    return k[:8] + "…" + k[-4:] if len(k) > 14 else k


def _provider_key_entries(name: str, p: dict) -> list:
    """provider 的 api_key/api_keys 归一化为 [{"key","label"}]（复用 providers 解析规则）。"""
    return providers._parse_keys(name, p)


def _write_provider_keys(cfg: dict, p: dict, entries: list, tag: str) -> None:
    """key 列表写回 provider 配置：备份 + 原子写 + 保存（避免半程写坏 config.json）。"""
    import shutil
    from proxy_core.settings import CONFIG_FILE
    bak = CONFIG_FILE.with_name(f"{CONFIG_FILE.name}.bak-{tag}")
    shutil.copy2(CONFIG_FILE, bak)
    p.pop("api_key", None)  # 旧单 key 字段折叠进 api_keys，防止与数组重复计数
    p["api_keys"] = entries
    tmp = CONFIG_FILE.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_FILE)


@bp.route("/provider/key/add", methods=["POST"])
@check_admin_auth
def api_provider_key_add():
    """给静态 provider 增加一个 API Key（多 key 轮换，热生效）。

    body: {"provider": "agnes", "key": "sk-...", "label": "备注（可选）"}
    label 缺省自动编号；同 key 重复添加返回 409；备注撞名返回 409。
    """
    import re
    data = request.get_json(force=True, silent=True) or {}
    name = str(data.get("provider") or "").strip()
    key = str(data.get("key") or "").strip()
    if not name or not key:
        return jsonify({"error": "缺少 provider 或 key"}), 400
    if len(key) > 512:
        return jsonify({"error": "key 长度异常（>512）"}), 400
    cfg = load_config()
    provs = cfg.get("providers") or {}
    p = provs.get(name)
    if not isinstance(p, dict):
        return jsonify({"error": f"provider「{name}」不存在"}), 404
    ptype = p.get("type")
    if ptype in ("atomcode", "cline", "minicode"):
        kind = {"atomcode": "AtomCode 签名", "cline": "Cline 官方",
                "minicode": "MiniCode（mcode）官方"}.get(ptype, ptype)
        return jsonify({"error": f"provider「{name}」是{kind}通道，无静态 key"}), 400
    entries = _provider_key_entries(name, p)
    if any(e["key"] == key for e in entries):
        return jsonify({"error": "该 key 已存在"}), 409
    label = re.sub(r"[^\w\u4e00-\u9fff.-]+", "_", str(data.get("label") or "").strip())[:24]
    used = {e["label"] for e in entries}
    if not label:
        n = len(entries) + 1
        label = f"{name}·{n}"
        while label in used:
            n += 1
            label = f"{name}·{n}"
    elif label in used:
        return jsonify({"error": f"备注「{label}」已被占用，换一个"}), 409
    entries.append({"key": key, "label": label})
    _write_provider_keys(cfg, p, entries, f"keyadd-{time.strftime('%m%d-%H%M%S')}")
    providers.all_providers()  # 立即触发 key 池重建（mtime 热生效）
    _provider_cache.update(ts=0, data=None)  # 击穿面板缓存，卡片即时反映新 key
    activity.add("info", f"Provider「{name}」新增 key「{label}」（共 {len(entries)} 个）",
                 model=name)
    return jsonify({"provider": name, "label": label, "preview": _mask_key(key),
                    "key_count": len(entries)})


@bp.route("/provider/key/remove", methods=["POST"])
@check_admin_auth
def api_provider_key_remove():
    """移除静态 provider 的一个 API Key（按备注 label，热生效）。

    body: {"provider": "agnes", "label": "..."}
    允许移除最后一个 key（provider 将暂停服务直至重新添加），响应带 warning。
    """
    data = request.get_json(force=True, silent=True) or {}
    name = str(data.get("provider") or "").strip()
    label = str(data.get("label") or "").strip()
    if not name or not label:
        return jsonify({"error": "缺少 provider 或 label"}), 400
    cfg = load_config()
    provs = cfg.get("providers") or {}
    p = provs.get(name)
    if not isinstance(p, dict):
        return jsonify({"error": f"provider「{name}」不存在"}), 404
    entries = _provider_key_entries(name, p)
    kept = [e for e in entries if e["label"] != label]
    if len(kept) == len(entries):
        return jsonify({"error": f"key「{label}」不存在"}), 404
    _write_provider_keys(cfg, p, kept, f"keydel-{time.strftime('%m%d-%H%M%S')}")
    providers.all_providers()
    _provider_cache.update(ts=0, data=None)  # 击穿面板缓存，卡片即时反映移除
    activity.add("info", f"Provider「{name}」移除 key「{label}」（剩 {len(kept)} 个）",
                 model=name)
    resp = {"provider": name, "removed": label, "key_count": len(kept)}
    if not kept:
        resp["warning"] = "已移除最后一个 key，该 provider 暂停服务，请重新添加"
    return jsonify(resp)


@bp.route("/roles")
@check_admin_auth
def api_roles():
    """角色别名映射表（含反查：每个角色当前的模型）。"""
    roles = get_role_map()
    return jsonify({
        "roles": roles,
        "detail": [{"role": r, "model": m, "models": [m]} for r, m in roles.items()],
    })


@bp.route("/accounts")
@check_admin_auth
def api_accounts():
    return jsonify(am.get_all_status())


@bp.route("/account/toggle", methods=["POST"])
@check_admin_auth
def api_toggle():
    data = request.get_json(force=True)
    ok = am.toggle_account(data.get("user_id"), data.get("enabled"))
    return jsonify({"ok": ok})


@bp.route("/account/add", methods=["POST"])
@check_admin_auth
def api_add():
    data = request.get_json(force=True)
    user_id = data.get("user_id", "")
    token = data.get("token", "")
    if not user_id or not token:
        return jsonify({"error": "user_id 和 token 不能为空"}), 400
    if not am.add_account(user_id, token, data.get("username", ""), data.get("org_id", "")):
        return jsonify({"error": "账号已存在"}), 400
    return jsonify({"ok": True})


@bp.route("/account/remove", methods=["POST"])
@check_admin_auth
def api_remove():
    data = request.get_json(force=True)
    return jsonify({"ok": am.remove_account(data.get("user_id", ""))})


@bp.route("/models/check", methods=["POST"])
@check_admin_auth
def api_models_check():
    """手动检查所有账号的 GLM-5.3 状态。"""
    for acc in am.accounts:
        key = api_cache.get(acc)
        if not key:
            continue
        headers = build_litellm_headers(key)
        try:
            with httpx.Client() as client:
                r = client.get(f"{LITELLM_API_BASE}/v1/models", headers=headers, timeout=10)
                model_ids = [m.get('id', '') for m in r.json().get('data', [])]
                has_glm53 = 'GLM-5.3' in model_ids
                am.update_glm_status(acc['user_id'], has_glm53)
                log.info("账号 %s 有 GLM-5.3: %s", acc['user_id'], has_glm53)
        except Exception as e:
            log.error("检查账号 %s 模型失败: %s", acc['user_id'], e)
    return jsonify({"ok": True})


@bp.route("/account/refresh", methods=["POST"])
@check_admin_auth
def api_refresh():
    api_cache.clear()
    am._load()
    return jsonify(am.get_all_status())


# ─── 配置与登录 ──────────────────────────────────────────────────────────────

@bp.route("/config", methods=["GET", "POST"])
@check_admin_auth
def api_config():
    if request.method == "POST":
        data = request.get_json(force=True)
        cfg = load_config()
        if "admin_password" in data:
            raw_pwd = data["admin_password"]
            cfg["admin_password"] = hash_password(raw_pwd) if raw_pwd else None
        if "allowed_ips" in data:
            cfg["allowed_ips"] = data["allowed_ips"]
        save_config(cfg)
        return jsonify({"ok": True})
    cfg = load_config()
    return jsonify({k: (v if k != "admin_password" else bool(v)) for k, v in cfg.items()})


@bp.route("/auth-check", methods=["GET"])
def api_auth_check():
    """前端页面加载时调用，判断是否已授权（用于恢复登录态）。"""
    return jsonify({"authorized": is_authorized()})


@bp.route("/login", methods=["POST"])
def api_login():
    cfg = load_config()
    pwd_hash = cfg.get("admin_password")
    ip = _client_ip()
    if not _login_allowed(ip):
        return jsonify({"ok": False, "error": "尝试次数过多，请 15 分钟后再试"}), 429
    submitted = (request.get_json(force=True, silent=True) or {}).get("password", "")
    if not pwd_hash:
        resp = jsonify({"ok": True})
        resp.set_cookie("admin_pwd", "", max_age=86400, httponly=True, samesite="Strict")
        return resp
    if verify_password(submitted, pwd_hash):
        _login_ok(ip)
        resp = jsonify({"ok": True})
        # cookie 存 HMAC 会话令牌（不存明文密码）；HTTPS 下加 Secure
        secure = request.headers.get("X-Forwarded-Proto", "") == "https"
        resp.set_cookie("admin_pwd", session_token(pwd_hash), max_age=86400,
                        httponly=True, samesite="Strict", secure=secure)
        return resp
    _login_fail(ip)
    return jsonify({"ok": False, "error": "密码错误"}), 401


@bp.route("/logout", methods=["POST"])
def api_logout():
    resp = jsonify({"ok": True})
    resp.delete_cookie("admin_pwd")
    return resp


# ─── 日志与统计 ──────────────────────────────────────────────────────────────

@bp.route("/logs")
@check_admin_auth
def api_logs():
    if not LOG_FILE.exists():
        return jsonify({"logs": []})
    with open(LOG_FILE, "r", encoding="utf-8") as f:
        lines = f.readlines()[-200:]
    return jsonify({"logs": lines})


@bp.route("/token-stats")
@check_admin_auth
def api_token_stats():
    return jsonify({
        "summary": token_stats.get_summary(),
        "details": token_stats.get_all(),
    })


@bp.route("/token-stats/reset", methods=["POST"])
@check_admin_auth
def api_token_stats_reset():
    token_stats.reset()
    return jsonify({"ok": True})


@bp.route("/stats/history")
@check_admin_auth
def api_stats_history():
    """小时级用量时间序列，?hours=24（1~168）。"""
    try:
        hours = int(request.args.get("hours", 24))
    except (TypeError, ValueError):
        hours = 24
    return jsonify({"history": token_stats.get_history(hours)})


@bp.route("/stats/cost")
@check_admin_auth
def api_stats_cost():
    """成本看板：按角色聚合的 token 用量与参考成本（价格表在 config.json pricing 节）。"""
    try:
        return jsonify(cost_report())
    except Exception as e:
        log.error("成本报表生成失败: %s", e)
        return jsonify({"error": str(e)[:200]}), 500


@bp.route("/activity")
@check_admin_auth
def api_activity():
    """实时请求动态（最近 100 条）。"""
    return jsonify({"events": activity.list(100)})


@bp.route("/inflight")
@check_admin_auth
def api_inflight():
    """当前进行中的请求（智能路由面板数据源）。"""
    reqs = registry.inflight()
    return jsonify({
        "requests": reqs,
        "total": len(reqs),
        # 按账号聚合的负载（选号参考）
        "loads": {a["user_id"]: registry.load_of(a["user_id"]) for a in am.accounts},
    })


# ─── 上游账号信息 ────────────────────────────────────────────────────────────

_UPSTREAM_TTL = 120  # 上游信息变化慢，缓存 120s 避免高频打上游接口
_upstream_cache = {"ts": 0.0, "data": None}
_provider_cache = {"ts": 0.0, "data": None}


@bp.route("/accounts/upstream")
@check_admin_auth
def api_accounts_upstream():
    """每个池内账号的上游真实信息（LiteLLM /key/info：累计花费、预算等）。

    用于在账号 402 被自动禁用之前看到上游消耗量。不含任何 token/key 本体。
    """
    now = time.time()
    if _upstream_cache["data"] is not None and now - _upstream_cache["ts"] < _UPSTREAM_TTL:
        return jsonify(_upstream_cache["data"])
    out = []
    for acc in am.accounts:
        uid = acc.get("user_id", "?")
        item = {"user_id": uid, "ok": False}
        key = api_cache.get(acc)
        if not key:
            item["error"] = "获取 API Key 失败"
        else:
            try:
                # 0831 修复：上游 /key/info 等接口已强制校验 X-Codely-Signature
                # （不带签名一律 403，邮箱 key_alias 拿不到 → 前端不显示邮箱）。
                # 签名按请求路径计算，三个接口分别签名。
                auth_headers = build_litellm_headers(key, "/key/info")
                user_headers = build_litellm_headers(key, "/user/info")
                token_headers = {"Authorization": f"Bearer {acc['access_token']}", "Accept": "application/json"}
                with httpx.Client() as client:
                    r = client.get(f"{LITELLM_API_BASE}/key/info", headers=auth_headers, timeout=8)
                    r2 = client.get(f"{LITELLM_API_BASE}/user/info", headers=user_headers, timeout=8)
                    # team info
                    team_id = ""
                    try:
                        tr = client.get("https://codely.tuanjie.cn/api/teams", headers=token_headers, timeout=8)
                        if tr.status_code == 200:
                            teams = tr.json().get("teams", [])
                            if teams:
                                team_id = teams[0].get("team_id", "")
                    except Exception:
                        pass
                    r3 = None
                    if team_id:
                        r3 = client.get(f"{LITELLM_API_BASE}/team/info",
                                        headers=build_litellm_headers(key, "/team/info"), timeout=8,
                                        params={"team_id": team_id})
                if r.status_code == 200:
                    info = r.json().get("info", {})
                    item.update({
                        "ok": True,
                        "spend": info.get("spend"),
                        "max_budget": info.get("max_budget"),
                        "budget_duration": info.get("budget_duration"),
                        "budget_reset_at": info.get("budget_reset_at"),
                        "key_alias": info.get("key_alias"),
                        "blocked": info.get("blocked"),
                        "created_at": info.get("created_at"),
                        "expires": info.get("expires"),
                    })
                else:
                    item["error"] = f"key/info 返回 {r.status_code}"
                if r2.status_code == 200:
                    uinfo = r2.json().get("user_info", {})
                    item["user_spend"] = uinfo.get("spend")
                    item["user_max_budget"] = uinfo.get("max_budget")
                # team info：套餐类型、消费、重置时间
                if r3 and r3.status_code == 200:
                    tinfo = r3.json().get("team_info", {})
                    meta = tinfo.get("metadata", {})
                    plan = meta.get("coding_plan", {}) if isinstance(meta, dict) else {}
                    item["team_spend"] = tinfo.get("spend")
                    item["team_max_budget"] = tinfo.get("max_budget")
                    item["team_budget_reset_at"] = tinfo.get("budget_reset_at")
                    item["plan_kind"] = plan.get("kind")
                    item["plan_product"] = plan.get("product_code")
                    item["plan_valid_to"] = plan.get("valid_to")
                    item["team_blocked"] = tinfo.get("blocked")
                # 402 = 额度耗尽
                if r.status_code == 402 or r2.status_code == 402 or (r3 and r3.status_code == 402):
                    item["ok"] = True
                    item["error"] = "额度耗尽 (402)"
                    item["spend"] = item.get("spend") or 0
            except Exception as e:
                item["error"] = str(e)[:120]
        out.append(item)
    data = {"accounts": out, "cached_seconds": _UPSTREAM_TTL}
    _upstream_cache.update(ts=now, data=data)
    return jsonify(data)


@bp.route("/providers/info")
@check_admin_auth
def api_providers_info():
    """外部 provider（如 Agnes）的账号信息：订阅 + 用量（OpenAI 计费风格接口）。

    不是所有 provider 都实现这两个端点；不可用时如实返回状态码，前端降级展示。
    响应绝不包含 api_key。
    """
    now = time.time()
    if _provider_cache["data"] is not None and now - _provider_cache["ts"] < _UPSTREAM_TTL:
        return jsonify(_provider_cache["data"])
    out = []
    for prov in providers.all_providers().values():
        if providers.is_atomcode(prov):
            chs = channels_for(prov["name"])
            accounts = []
            for ch in chs:
                st = ch.status()
                accounts.append({
                    "label": st.get("label"),
                    "user_id": st.get("user_id"),
                    "logged_in": st.get("logged_in"),
                    "token_valid_days": st.get("token_valid_days"),
                    "ok": st.get("ok"),
                    "error": st.get("error"),
                })
            any_ok = any(a["ok"] and a["logged_in"] for a in accounts)
            first_ok = next((a for a in accounts if a["ok"] and a["logged_in"]), None)
            out.append({
                "name": prov["name"],
                "base_url": prov["base_url"],
                "models": prov["models"],
                "ok": any_ok,
                "atomcode": True,
                "accounts": accounts,
                "account_count": len(accounts),
                "user_id": first_ok["user_id"] if first_ok else None,
                "logged_in": bool(first_ok),
                "token_valid_days": first_ok["token_valid_days"] if first_ok else 0,
            })
            continue
        if providers.is_cline(prov):
            chs = cline.channels_for(prov["name"])
            accounts = []
            for ch in chs:
                st = ch.status()
                # 字段白名单（0924 审计 L-1）：与 atomcode 分支同纪律，
                # 不透传 auth_path 等本机路径信息
                accounts.append({k: st.get(k) for k in (
                    "label", "user_id", "logged_in", "ok",
                    "expires_at_min", "cooldown_min_left", "cooldown_note", "error")})
            any_ok = any(a["ok"] and a["logged_in"] for a in accounts)
            out.append({
                "name": prov["name"],
                "base_url": prov["base_url"],
                "models": prov["models"],
                "ok": any_ok,
                "cline": True,
                "accounts": accounts,
                "account_count": len(accounts),
                "user_id": next((a["user_id"] for a in accounts if a.get("user_id")), None),
                "logged_in": any_ok,
            })
            continue
        if providers.is_minicode(prov):
            chs = minicode.channels_for(prov["name"])
            accounts = []
            for ch in chs:
                st = ch.status()
                # 字段白名单（与 atomcode/cline 分支同纪律，不透传本机路径）
                accounts.append({k: st.get(k) for k in (
                    "label", "logged_in", "ok",
                    "expires_at_min", "cooldown_min_left", "cooldown_note", "error")})
            any_ok = any(a["ok"] and a["logged_in"] for a in accounts)
            out.append({
                "name": prov["name"],
                "base_url": prov["base_url"],
                "models": prov["models"] + [
                    "{} ⇒ {}".format(a, t)
                    for a, t in (prov.get("model_aliases") or {}).items()
                    if a not in prov["models"]],
                "ok": any_ok,
                "minicode": True,
                "accounts": accounts,
                "account_count": len(accounts),
                "logged_in": any_ok,
            })
            continue
        # 0928: model_aliases 别名并入徽标列表（intern-glm-5.3 ⇒ glm-5.3 等），
        # 仅展示层变化，路由/转发逻辑不变。
        _amap = prov.get("model_aliases") or {}
        item = {
            "name": prov["name"],
            "base_url": prov["base_url"],
            "models": prov["models"] + ["{} ⇒ {}".format(a, t) for a, t in _amap.items() if a not in prov["models"]],
            "ok": False,
        }
        if prov.get("keys"):
            item["key_count"] = len(prov["keys"])
            item["key_entries"] = [
                {"label": k["label"], "preview": _mask_key(k["key"])}
                for k in prov["keys"]
            ]
        headers = {"Authorization": f"Bearer {prov['api_key']}", "Accept": "application/json"}
        try:
            with httpx.Client() as client:
                r = client.get(prov["base_url"] + "/dashboard/billing/subscription",
                               headers=headers, timeout=8)
                item["subscription_status"] = r.status_code
                item["subscription"] = r.json() if r.status_code == 200 else None
                r2 = client.get(prov["base_url"] + "/dashboard/billing/usage",
                                headers=headers, timeout=8)
                item["usage_status"] = r2.status_code
                item["usage"] = r2.json() if r2.status_code == 200 else None
            item["ok"] = True
        except Exception as e:
            item["error"] = str(e)[:120]
        out.append(item)
    data = {"providers": out, "cached_seconds": _UPSTREAM_TTL}
    _provider_cache.update(ts=now, data=data)
    return jsonify(data)


# ─── API Key（朋友共享）─────────────────────────────────────────────────────

@bp.route("/keys/info")
@check_admin_auth
def api_keys_info():
    """API key 明细 + 今日用量（按 key 记账，供共享/限额面板）。"""
    from proxy_core.settings import get_api_key_entries
    usage = {e["label"]: e for e in token_stats.get_key_stats()}
    out = []
    for e in get_api_key_entries():
        u = usage.get(e["label"], {})
        out.append({
            "label": e["label"],
            "key_preview": e["key_preview"],
            "daily_token_limit": e["limit"],
            "today_tokens": u.get("total_tokens", 0),
            "today_requests": u.get("requests", 0),
            "today_models": u.get("models", {}),
        })
    out.sort(key=lambda x: x["today_tokens"], reverse=True)
    return jsonify({"keys": out})


# ─── 注水检测 ────────────────────────────────────────────────────────────────

@bp.route("/watercheck")
@check_admin_auth
def api_watercheck():
    """注水检测面板数据：各目标最近一次探针结果 + 被动捕获的可疑事件。"""
    return jsonify(water.status())


# 注水检测模型清单缓存：上游 /v1/models 拉一次用 10 分钟，避免管理面板轮询打上游
_models_cache = {"ts": 0.0, "ids": []}
_MODELS_TTL = 600


def _pool_models() -> list:
    """主账号池可探测模型：上游 /v1/models（带缓存），失败回退静态默认清单。"""
    now = time.time()
    if _models_cache["ids"] and now - _models_cache["ts"] < _MODELS_TTL:
        return list(_models_cache["ids"])
    ids = []
    if am.accounts:
        acc = am.accounts[0]
        key = api_cache.get(acc)
        if key:
            try:
                headers = build_litellm_headers(key, "/v1/models")
                with httpx.Client() as client:
                    r = client.get(f"{LITELLM_API_BASE}/v1/models", headers=headers, timeout=10)
                    if r.status_code == 200:
                        ids = sorted({m.get("id") for m in r.json().get("data", [])
                                      if isinstance(m, dict) and m.get("id")})
            except Exception as e:
                log.warning("watercheck/models 拉取上游模型列表失败: %s", e)
    if ids:
        _models_cache.update(ts=now, ids=ids)
        return list(ids)
    return ["GLM-5.3", "codely-core", "codely-flash"]


@bp.route("/watercheck/models")
@check_admin_auth
def api_watercheck_models():
    """注水检测可选模型清单：主账号池模型 + 外部 Provider 模型（带分组）。

    主账号池清单取自上游 /v1/models（10 分钟缓存，失败回退静态默认）；
    外部 Provider 清单来自本地 config.json，不访问其上游。
    """
    provs = []
    ext_models = set()
    for name, p in providers.all_providers().items():
        models = list(p.get("models", []))
        ext_models.update(models)
        if providers.is_cline(p):
            # 0924 审计 M-2：Cline 探针不适用——通用金丝雀探针没有客户端指纹头
            # 与动态凭证，对 api.cline.bot 必 403 误报，不进可选清单
            continue
        provs.append({
            "name": name,
            "atomcode": providers.is_atomcode(p),
            "models": models,
        })
    roles = set(get_role_map().keys())
    pool = [m for m in _pool_models() if m not in ext_models]
    # 统计里真实跑过的池模型也补进来（本地数据，零成本）；角色别名不可探测，跳过
    for k in token_stats.stats:
        parts = k.split("|", 1)
        if (len(parts) == 2 and parts[1] and parts[1] not in ext_models
                and parts[1] not in pool and parts[1] not in roles):
            pool.append(parts[1])
    pool.sort()
    return jsonify({"pool": pool, "providers": provs})


@bp.route("/watercheck/run", methods=["POST"])
@check_admin_auth
def api_watercheck_run():
    """对指定模型逐账号发金丝雀探针（小成本：3 题 × 账号数）。

    body: {"model": "GLM-5.3"}；未指定时默认 GLM-5.3。
    """
    data = request.get_json(force=True, silent=True) or {}
    model = (data.get("model") or "").strip() or "GLM-5.3"
    providers_map = {}
    for pn, p in providers.all_providers().items():
        if providers.is_atomcode(p):
            providers_map[pn] = p
        elif providers.is_cline(p):
            # 0924 审计 M-2：Cline 通道探针不适用（无指纹头必 403），跳过
            continue
        else:
            # key 池探针用当前选中的 key（单 key 时与旧行为一致）
            entry = providers.pick_key(p)
            q = dict(p)
            if entry:
                q["api_key"] = entry["key"]
            providers_map[pn] = q
    try:
        result = water.run_probe(model, am.accounts, api_cache.get, providers_map)
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 429
    except Exception as e:
        log.error("注水探针失败: %s", e)
        return jsonify({"error": str(e)[:200]}), 500
    activity.add("info", f"注水检测完成：{model}（{len(result['results'])} 个目标）", model=model)
    return jsonify(result)


# ─── 种子卫兵（0905 吸收自 ProxyDeck seedcheck 思路）──────────────────────────
@bp.route("/seed-alert")
@check_admin_auth
def seed_alert():
    """官方 CLI 签名特征核对状态（本地 bundle + npm 在线双线）。"""
    try:
        from proxy_core.seedguard import state
        return jsonify(state())
    except Exception as e:
        return jsonify({"error": str(e)})


# ─── 知识库（0905）：SQLite 结构化请求日志 ────────────────────────────────────
@bp.route("/db/summary")
@check_admin_auth
def db_summary():
    """知识库概要：总请求数/错误数/模型数/DB大小/24h模型用量TOP10。"""
    # 防呆：未设管理密码时知识库端点禁用（含上游敏感数据）
    if not load_config().get("admin_password"):
        return jsonify({"error": "知识库端点要求先设置管理密码（config admin_password）"}), 403
    try:
        from proxy_core.db import kdb
        return jsonify(kdb.stats_summary())
    except Exception as e:
        return jsonify({"error": str(e)})

@bp.route("/db/export")
@check_admin_auth
def db_export():
    """导出知识库数据（CSV，供下载/知识库导入）。"""
    if not load_config().get("admin_password"):
        return jsonify({"error": "知识库端点要求先设置管理密码"}), 403
    try:
        from proxy_core.db import kdb
        table = request.args.get("table", "requests")
        limit = int(request.args.get("limit", 10000))
        csv_data = kdb.export_csv(table, limit)
        from flask import Response
        return Response(
            csv_data, mimetype="text/csv",
            headers={"Content-Disposition":
                     f"attachment; filename=relay_{table}_{int(time.time())}.csv"})
    except Exception as e:
        return jsonify({"error": str(e)})

@bp.route("/db/query")
@check_admin_auth
def db_query():
    """只读 SQL 查询（支持 ORDER BY / GROUP BY / LIMIT，禁写操作）。"""
    if not load_config().get("admin_password"):
        return jsonify({"error": "知识库端点要求先设置管理密码"}), 403
    try:
        from proxy_core.db import kdb
        sql = request.args.get("sql", "").strip().rstrip(";")
        if not sql:
            return jsonify({"error": "SQL 不能为空"})
        if not sql.upper().startswith("SELECT"):
            return jsonify({"error": "只允许 SELECT 查询"})
        for w in ["INSERT", "DELETE", "UPDATE", "DROP", "CREATE", "ALTER",
                  "PRAGMA", "ATTACH", "DETACH"]:
            if w in sql.upper():
                return jsonify({"error": f"禁止 {w} 操作"})
        rows, truncated = kdb.query(sql)
        return jsonify({"count": len(rows), "truncated": truncated, "data": rows})
    except Exception as e:
        return jsonify({"error": str(e)})
