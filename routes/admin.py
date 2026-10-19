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
    why_distribution=粘性决策 why 计数分布。

    §R-D 补 rebind_distribution：rebind 分级决策计数
    （quota 档下的 transient_keep / hard_clear / hard_clear_miss /
    record_skipped；默认档恒为空——默认档零动作、不计数的语义
    本身就是"现状零改动"的证据）。
    """
    stuck, dist = affinity.snapshot()
    return jsonify({"stuck_sessions": stuck,
                    "why_distribution": dist,
                    "rebind_distribution": affinity.rebind_counts()})


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

# ─── Provider 独立启停（T10）────────────────────────────────────────
# openrouter 等池做成独立可开关：config.json 的 provider 节
# "enabled": false 即停用（缺省启用）。写盘走文本级结构
# 保留式——config.json pricing 节存在大小写折叠重复键
# （deepseek-v4-flash / DeepSeek-V4-Flash），json.load→
# json.dump 整文件回写会静默消除重复键改变语义，故只在
# 原文上做外科手术式编辑（锚点定位 + 全文件解析校验）。

def _brace_match(text: str, open_idx: int) -> int:
    """花括号配对（跳过字符串字面量），返回与 text[open_idx]=='{'
    匹配的闭括号下标；不配对抛 ValueError。"""
    depth = 0
    i = open_idx
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == '"':
            i += 1
            while i < n:
                if text[i] == "\\":
                    i += 2
                    continue
                if text[i] == '"':
                    break
                i += 1
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise ValueError("花括号不配对")


def _member_value_span(text: str, start: int, end: int, key: str):
    """在 [start, end) 内找直辖成员 "key": 的值区间（跳过嵌套
    对象/数组与字符串字面量，只认本层直辖键）。返回
    (vstart, vend)（已跳过前导空白、不含尾随逗号）；
    未找到返回 None。"""
    i = start
    depth = 0
    n = end
    while i < n:
        ch = text[i]
        if ch == '"':
            j = i + 1
            while j < n:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == '"':
                    break
                j += 1
            if depth == 0:
                k = j + 1
                while k < n and text[k] in " \t\r\n":
                    k += 1
                if k < n and text[k] == ":" and json.loads(text[i:j + 1]) == key:
                    v = k + 1
                    while v < n and text[v] in " \t\r\n":
                        v += 1
                    ve = v
                    d2 = 0
                    while ve < n:
                        c2 = text[ve]
                        if c2 == '"':
                            ve += 1
                            while ve < n:
                                if text[ve] == "\\":
                                    ve += 2
                                    continue
                                if text[ve] == '"':
                                    break
                                ve += 1
                        elif c2 in "{[":
                            d2 += 1
                        elif c2 in "}]":
                            if d2 == 0:
                                break
                            d2 -= 1
                        elif c2 == "," and d2 == 0:
                            break
                        ve += 1
                    return (v, ve)
            i = j + 1
            continue
        if ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
        i += 1
    return None


def _toggle_provider_in_text(text: str, name: str, enabled: bool) -> str:
    """文本级结构保留式改写 config.json 全文：把 providers 节内
    name 块的 enabled 设为 enabled。算法：

    1. 写前全文件 json.loads 校验（坏文件直接拒绝，不落盘）
    2. 定位 "providers" 节：首次出现 "providers": {，
       花括号配对得节界（_brace_match 跳过字符串字面量）
    3. 节内首次出现 "<name>": { 即该 provider 块起点
       （同名键重复的病理配置由第 5 步复读校验兜底拒绝）
    4. 块内直辖成员扫描（_member_value_span）：
       - 有 "enabled" 成员 → 原地替换其值为 true/false
       - 无 → 在块开头插入 "enabled": true/false,
         （沿用块内首成员缩进；空块 {} 直接填入）
    5. 写后复读校验：json.loads(new) 成功且 providers[name]
       .enabled 确为 enabled——任一失败抛 ValueError，
       调用方据此 500，原文一字不动，绝不半截
    """
    import re
    cfg = json.loads(text)  # 写前全文件校验
    # N-1：文本手术前确认目标 provider 是对象（病理配置
    # 确定性拒绝——非 dict 条目直接 ValueError，原文不动）
    provs = cfg.get("providers") if isinstance(cfg, dict) else None
    if not isinstance(provs, dict) or not isinstance(provs.get(name), dict):
        raise ValueError(
            'providers 节内无 provider「%s」（或条目非对象）' % name)
    m = re.search(r'"providers"\s*:\s*\{', text)
    if not m:
        raise ValueError('config.json 无 "providers" 节')
    sec_open = m.end() - 1
    sec_end = _brace_match(text, sec_open)
    pat = re.compile('"' + re.escape(name) + r'"\s*:\s*\{')
    pm = pat.search(text, m.end(), sec_end)
    if not pm:
        raise ValueError('providers 节内无 provider「%s」' % name)
    block_open = pm.end() - 1
    block_end = _brace_match(text, block_open)
    val = "true" if enabled else "false"
    span = _member_value_span(text, block_open + 1, block_end, "enabled")
    if span:
        new_text = text[:span[0]] + val + text[span[1]:]
    else:
        after_open = block_open + 1
        j = after_open
        while j < block_end and text[j] in " \t\r\n":
            j += 1
        if j >= block_end:
            ins = '"enabled": ' + val  # 空块 {}
        else:
            ws = text[after_open:j]
            if "\n" in ws:
                indent = ws[ws.rfind("\n") + 1:]
                ins = "\n" + indent + '"enabled": ' + val + ","
            else:
                ins = '"enabled": ' + val + ", "
        new_text = text[:after_open] + ins + text[after_open:]
    # 写后复读校验：解析成功且值确实落地
    check = json.loads(new_text)
    got = ((check.get("providers") or {}).get(name) or {}).get("enabled")
    if got is not enabled:
        raise ValueError("复读校验失败：providers.%s.enabled=%r" % (name, got))
    return new_text


def _set_provider_enabled(name: str, enabled: bool) -> None:
    """enabled 开关落盘：文本级改写 + 备份 + 原子写（避免半程写坏）。"""
    import shutil
    from proxy_core.settings import CONFIG_FILE
    new_text = _toggle_provider_in_text(
        CONFIG_FILE.read_text(encoding="utf-8"), name, enabled)
    bak = CONFIG_FILE.with_name(
        f"{CONFIG_FILE.name}.bak-provtoggle-{time.strftime('%m%d-%H%M%S')}")
    shutil.copy2(CONFIG_FILE, bak)
    tmp = CONFIG_FILE.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(new_text)
    os.replace(tmp, CONFIG_FILE)


@bp.route("/providers/<name>/enabled", methods=["POST"])
@check_admin_auth
def api_provider_enabled(name):
    """独立启停一个 provider 池（如 openrouter），热生效。

    body: {"enabled": true|false}。停用后：该 provider 模型从
    /v1/models 消失，命中其模型的请求走模型守卫 404（干净的
    "池已关闭"语义，不报 500）；key 选择/计账不可达。写
    config.json 用文本级结构保留式（pricing 大小写折叠重复键
    不因回写丢失）。
    """
    data = request.get_json(force=True, silent=True) or {}
    enabled = data.get("enabled")
    if not isinstance(enabled, bool):
        return jsonify({"error": "enabled 必须为布尔值（true/false）"}), 400
    name = str(name or "").strip()
    if not isinstance((load_config().get("providers") or {}).get(name), dict):
        return jsonify({"error": f"provider「{name}」不存在"}), 404
    try:
        _set_provider_enabled(name, enabled)
    except (ValueError, OSError) as e:
        log.error("provider「%s」enabled 写盘失败: %s", name, e)
        return jsonify({"error": f"写 config.json 失败: {e}"}), 500
    providers.all_providers()  # 立即触发重载（mtime 热生效）
    _provider_cache.update(ts=0, data=None)  # 击穿面板缓存，卡片即时反映
    activity.add("info",
                 f"Provider「{name}」已{'启用' if enabled else '停用'}",
                 model=name)
    return jsonify({"provider": name, "enabled": enabled})

# ─── 页面添加新渠道（F1）────────────────────────────────────────
# 面板「+ 添加渠道」：config.json providers 节块首插入新
# provider 块。文本级结构保留式（T10 同款纪律——pricing
# 节大小写折叠重复键不因回写丢失）。


def _provider_name_ok(name: str) -> bool:
    import re
    return bool(re.match(r"^[A-Za-z0-9_-]+$", name))


def _add_provider_in_text(text: str, name: str, base_url: str,
                          api_key: str, enabled: bool) -> str:
    """文本级结构保留式改写 config.json 全文：在 providers
    节块首插入新 provider 块。算法：

    1. 写前全文件 json.loads 校验（坏文件直接拒绝）
    2. 锚点 "providers": { 定位节（首次出现），
       _brace_match 花括号配对（跳过字符串字面量）得节界
    3. 节块首插入新块（沿用节内首成员缩进；空节取
       "providers" 键行缩进 +2），块结构：
       {"base_url", "api_keys":[{key,label:"主key"}],
       "models":[], "enabled"}——models 空初始（F2 探测填写）
    4. 写后复读校验：json.loads(new) 成功且新块字段确已落地
    """
    import re
    json.loads(text)  # 写前全文件校验
    m = re.search(r'"providers"\s*:\s*\{', text)
    if not m:
        raise ValueError('config.json 无 "providers" 节')
    sec_open = m.end() - 1
    sec_end = _brace_match(text, sec_open)
    after_open = sec_open + 1
    j = after_open
    while j < sec_end and text[j] in " \t\r\n":
        j += 1
    # 节成员缩进：优先沿用节内首成员缩进；空节取
    # "providers" 键行缩进 + 2（标准 2 空格 JSON）
    if j < sec_end:
        ws = text[after_open:j]
        member_indent = ws[ws.rfind("\n") + 1:] if "\n" in ws else ""
    else:
        ls = text.rfind("\n", 0, m.start())
        ls = ls + 1 if ls >= 0 else 0
        member_indent = text[ls:m.start()] + "  "
    inner = member_indent + "  "
    val = "true" if enabled else "false"
    block = (
        '%s: {\n'
        '%s"base_url": %s,\n'
        '%s"api_keys": [{"key": %s, "label": "主key"}],\n'
        '%s"models": [],\n'
        '%s"enabled": %s\n'
        '%s}'
    ) % (json.dumps(name, ensure_ascii=False),
         inner, json.dumps(base_url, ensure_ascii=False),
         inner, json.dumps(api_key, ensure_ascii=False),
         inner,
         inner, val,
         member_indent)
    if j >= sec_end:
        # 空节 {}：填入并补节闭括号缩进
        ls = text.rfind("\n", 0, m.start())
        ls = ls + 1 if ls >= 0 else 0
        sec_indent = text[ls:m.start()]
        ins = "\n" + member_indent + block + "\n" + sec_indent
    else:
        ins = "\n" + member_indent + block + ","
    new_text = text[:after_open] + ins + text[after_open:]
    # 写后复读校验：解析成功且新块字段确已落地
    check = json.loads(new_text)
    got = (check.get("providers") or {}).get(name)
    if (not isinstance(got, dict)
            or got.get("base_url") != base_url
            or got.get("enabled") is not enabled
            or got.get("models") != []
            or not isinstance(got.get("api_keys"), list)
            or not got["api_keys"]
            or got["api_keys"][0].get("key") != api_key
            or got["api_keys"][0].get("label") != "主key"):
        raise ValueError("复读校验失败：providers.%s 字段未正确落地" % name)
    return new_text


def _set_provider_add(name: str, base_url: str, api_key: str,
                      enabled: bool) -> None:
    """新 provider 落盘：文本级改写 + 备份 + 原子写（避免半程写坏）。"""
    import shutil
    from proxy_core.settings import CONFIG_FILE
    new_text = _add_provider_in_text(
        CONFIG_FILE.read_text(encoding="utf-8"),
        name, base_url, api_key, enabled)
    bak = CONFIG_FILE.with_name(
        f"{CONFIG_FILE.name}.bak-provadd-{time.strftime('%m%d-%H%M%S')}")
    shutil.copy2(CONFIG_FILE, bak)
    tmp = CONFIG_FILE.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(new_text)
    os.replace(tmp, CONFIG_FILE)


@bp.route("/providers/add", methods=["POST"])
@check_admin_auth
def api_provider_add():
    """面板「+ 添加渠道」：新增一个外部 provider 池。

    body: {"name": "xxx", "base_url": "https://...",
    "api_key": "sk-...", "enabled": true（可缺省）}
    校验：name 匹配 ^[A-Za-z0-9_-]+$ 且在 providers 节
    唯一、base_url 以 https:// 开头、api_key 非空——
    任一不满足 400 且 config.json 逐字节不动。通过则
    文本级结构保留式插入 providers 节块首（models 空
    初始，由「探测模型」填写），热生效。
    """
    data = request.get_json(force=True, silent=True) or {}
    name = str(data.get("name") or "").strip()
    base_url = str(data.get("base_url") or "").strip()
    api_key = str(data.get("api_key") or "").strip()
    enabled = data.get("enabled", True)
    if not isinstance(enabled, bool):
        enabled = True
    if not name or not _provider_name_ok(name):
        return jsonify({"error": "渠道名仅允许字母/数字/下划线/连字符"}), 400
    if not base_url.startswith("https://"):
        return jsonify({"error": "base_url 必须以 https:// 开头"}), 400
    if not api_key:
        return jsonify({"error": "api_key 不能为空"}), 400
    if name in (load_config().get("providers") or {}):
        return jsonify({"error": f"渠道「{name}」已存在"}), 400
    try:
        _set_provider_add(name, base_url, api_key, enabled)
    except (ValueError, OSError) as e:
        log.error("provider「%s」添加写盘失败: %s", name, e)
        return jsonify({"error": f"写 config.json 失败: {e}"}), 500
    providers.all_providers()  # 立即触发重载（mtime 热生效）
    _provider_cache.update(ts=0, data=None)  # 击穿面板缓存，卡片即时反映
    activity.add("info", f"Provider「{name}」已添加（{base_url}）", model=name)
    return jsonify({"provider": name, "base_url": base_url, "enabled": enabled})


# ─── Provider 渠道删除（F6）──────────────────────────────────
# 面板「删除渠道」：providers 节整块移除（渠道配置与
# 全部 key 一并消失，模型即刻从路由消失）。文本级结构
# 保留式（与 T10/F1 同纪律：pricing 节大小写折叠重复
# 键不因回写丢失），删块时按首/中/末/唯一成员四种
# 位置正确处理配套逗号。
# 删除边界（比云端更严）：仅静态 key 型渠道可删——
# 带 type 字段的渠道（atomcode/cline/minicode 等本
# 地工具型通道）一律 400 拒绝，防误删核心通道。


def _find_member_block(text: str, sec_open: int, sec_end: int, name: str):
    """在 [sec_open, sec_end] 节内深度感知扫描直辖成员
    "name": {。跳过字符串字面量（_comment 等文本内容里
    的假锚点天然免疫），只认本层（depth==0）键；遇嵌套
    同名键继续扫描。返回 (member_start, block_end)——
    member_start 是成员键的开引号下标（删除时从此处
    连键带块一起删），block_end 是块闭括号下标；
    未找到返回 None。"""
    i = sec_open + 1
    depth = 0
    while i < sec_end:
        ch = text[i]
        if ch == '"':
            j = i + 1
            while j < sec_end:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == '"':
                    break
                j += 1
            if depth == 0:
                # 本层键：命中目标且值为对象 → 成员块定位
                k = j + 1
                while k < sec_end and text[k] in " \t\r\n":
                    k += 1
                if (k < sec_end and text[k] == ":"
                        and json.loads(text[i:j + 1]) == name):
                    v = k + 1
                    while v < sec_end and text[v] in " \t\r\n":
                        v += 1
                    if v < sec_end and text[v] == "{":
                        return (i, _brace_match(text, v))
                    # 直辖成员值非对象（病理配置）：跳过继续扫
                    i = k
                    continue
            i = j + 1
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        i += 1
    return None


def _remove_provider_in_text(text: str, name: str) -> str:
    """文本级结构保留式改写 config.json 全文：从 providers
    节整体删除 name 成员块（含配套逗号）。算法：

    1. 写前全文件 json.loads 校验（坏文件直接拒绝，不落盘）
    2. N-1 前置校验：目标 provider 是对象（病理配置
       确定性拒绝——非 dict 条目直接 ValueError，原文不动）
    3. 定位 "providers" 节（首次出现 "providers": {，
       花括号配对得节界）
    4. 节内深度感知扫描定位直辖成员块
       （_find_member_block：跳过字符串字面量与嵌套同名键）
    5. 删块 + 配套逗号，三种位置语义统一：
       - 块后有逗号（首/中间成员）：删块至尾逗号（含），
         再吞其后空白——前后成员以原空白衔接
       - 块前有逗号（末成员，无尾逗号）：删前导逗号
         （含）至块尾——前成员直接衔接节闭括号前空白
       - 无逗号（唯一成员）：删块；节内仅剩空白 → 清空 {}
    6. 写后复读校验：json.loads(new) 成功、providers 节
       目标确已消失、其余 provider 节点逐字段不变、顶层
       其他键不变——任一失败抛 ValueError，原文一字不动
    """
    import re
    cfg = json.loads(text)  # 写前全文件校验
    # N-1：文本手术前确认目标 provider 是对象（病理配置
    # 确定性拒绝——非 dict 条目直接 ValueError，原文不动）
    provs = cfg.get("providers") if isinstance(cfg, dict) else None
    if not isinstance(provs, dict) or not isinstance(provs.get(name), dict):
        raise ValueError(
            'providers 节内无 provider「%s」（或条目非对象）' % name)
    m = re.search(r'"providers"\s*:\s*\{', text)
    if not m:
        raise ValueError('config.json 无 "providers" 节')
    sec_open = m.end() - 1
    sec_end = _brace_match(text, sec_open)
    blk = _find_member_block(text, sec_open, sec_end, name)
    if blk is None:
        raise ValueError('providers 节内无 provider「%s」' % name)
    member_open, block_end = blk
    # 尾逗号分支：块后跳过空白若是逗号 → 删块至尾逗号
    k = block_end + 1
    while k < sec_end and text[k] in " \t\r\n":
        k += 1
    if k < sec_end and text[k] == ",":
        e = k + 1  # 吞掉逗号后空白，衔接下一成员
        while e < sec_end and text[e] in " \t\r\n":
            e += 1
        new_text = text[:member_open] + text[e:]
    else:
        # 前导逗号分支：块前跳过空白若是逗号 → 删逗号至块尾
        b = member_open - 1
        while b > sec_open and text[b] in " \t\r\n":
            b -= 1
        if b > sec_open and text[b] == ",":
            new_text = text[:b] + text[block_end + 1:]
        else:
            # 唯一成员：删块；节内仅剩空白时清空为 {}
            inner = text[sec_open + 1:member_open] + text[block_end + 1:sec_end]
            if not inner.strip():
                new_text = text[:sec_open + 1] + text[sec_end:]
            else:
                new_text = text[:member_open] + text[block_end + 1:]
    # 写后复读校验：解析成功、目标确已消失、其余节点逐字段不变
    check = json.loads(new_text)
    cprovs = check.get("providers")
    if not isinstance(cprovs, dict) or name in cprovs:
        raise ValueError("复读校验失败：providers.%s 未正确移除" % name)
    if len(cprovs) != len(provs) - 1:
        raise ValueError("复读校验失败：providers 节成员数异常")
    for k2, v2 in provs.items():
        if k2 == name:
            continue
        if cprovs.get(k2) != v2:
            raise ValueError("复读校验失败：providers.%s 被改动" % k2)
    for k2, v2 in cfg.items():
        if k2 == "providers":
            continue
        if check.get(k2) != v2:
            raise ValueError("复读校验失败：顶层键 %s 被改动" % k2)
    return new_text


def _set_provider_remove(name: str) -> None:
    """provider 块整体移除落盘：文本级改写 + 备份 + 原子写（避免半程写坏）。"""
    import shutil
    from proxy_core.settings import CONFIG_FILE
    new_text = _remove_provider_in_text(
        CONFIG_FILE.read_text(encoding="utf-8"), name)
    bak = CONFIG_FILE.with_name(
        f"{CONFIG_FILE.name}.bak-provdel-{time.strftime('%m%d-%H%M%S')}")
    shutil.copy2(CONFIG_FILE, bak)
    tmp = CONFIG_FILE.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(new_text)
    os.replace(tmp, CONFIG_FILE)


@bp.route("/providers/<name>/remove", methods=["POST"])
@check_admin_auth
def api_provider_remove(name):
    """面板「删除渠道」：从 config.json providers 节整体
    移除一个 provider（渠道配置与全部 key 一并消失，
    热生效）。

    name 不存在 404；带 type 字段的特殊通道（atomcode/
    cline/minicode 等本地工具型）400 拒绝——仅静态
    key 型渠道支持面板删除（防误删核心通道）。写
    config.json 用文本级结构保留式（pricing 大小写折叠
    重复键不因回写丢失），删前备份
    config.json.bak-provdel-MMDD-HHMMSS 可恢复。响应
    key_count 为删除前统计（_parse_keys 归一化口径，
    与 key 清单同口径），给用户确认参考。
    """
    name = str(name or "").strip()
    provs = load_config().get("providers") or {}
    p = provs.get(name)
    if not isinstance(p, dict):
        return jsonify({"error": f"provider「{name}」不存在"}), 404
    if "type" in p:
        ptype = p.get("type")
        kind = {"atomcode": "AtomCode 签名", "cline": "Cline 官方",
                "minicode": "MiniCode（mcode）官方"}.get(ptype, f"type={ptype}")
        return jsonify({"error": f"provider「{name}」是{kind}通道，"
                        f"仅静态 key 型渠道支持面板删除"}), 400
    # 删除前统计 key 数（给用户确认参考）
    key_count = len(providers._parse_keys(name, p))
    try:
        _set_provider_remove(name)
    except (ValueError, OSError) as e:
        log.error("provider「%s」删除写盘失败: %s", name, e)
        return jsonify({"error": f"写 config.json 失败: {e}"}), 500
    providers.all_providers()  # 立即触发重载（mtime 热生效）
    _provider_cache.update(ts=0, data=None)  # 击穿面板缓存，卡片即时消失
    activity.add("info",
                 f"Provider「{name}」已删除（{key_count} 个 key 一并移除）",
                 model=name)
    return jsonify({"provider": name, "key_count": key_count})


# ─── F14 per-key 启用 / 停用（额度耗尽先停用，重置后再启用）──────────────
# 文本级结构保留式改写（与 T10/F1/F6 同纪律：config.json pricing 节大小写
# 折叠重复键不因回写丢失）——定位 provider 块 api_keys 数组中 label 对应
# 条目，str 条目升级为 dict 文本、dict 条目插/删 "disabled" 成员。


def _member_span(text: str, start: int, end: int, key: str):
    """在 [start, end) 内找直辖成员 "key": 的 span（跳过嵌套对象/数组与
    字符串字面量，只认本层直辖键）。返回 (key_start, key_end, value_start,
    value_end)：key_start/key_end 为键字符串起/闭引号下标，value_end 为值
    末字符后一位（不含尾随逗号）；未找到返回 None。"""
    i = start
    depth = 0
    while i < end:
        ch = text[i]
        if ch == '"':
            ks = i
            j = i + 1
            while j < end:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == '"':
                    break
                j += 1
            ke = j
            if depth == 0:
                k = j + 1
                while k < end and text[k] in " \t\r\n":
                    k += 1
                if k < end and text[k] == ":" and json.loads(text[ks:ke + 1]) == key:
                    v = k + 1
                    while v < end and text[v] in " \t\r\n":
                        v += 1
                    ve = v
                    d2 = 0
                    while ve < end:
                        c2 = text[ve]
                        if c2 == '"':
                            ve += 1
                            while ve < end:
                                if text[ve] == "\\":
                                    ve += 2
                                    continue
                                if text[ve] == '"':
                                    break
                                ve += 1
                        elif c2 in "{[":
                            d2 += 1
                        elif c2 in "}]":
                            if d2 == 0:
                                break
                            d2 -= 1
                        elif c2 == "," and d2 == 0:
                            break
                        ve += 1
                    return (ks, ke, v, ve)
            i = j + 1
            continue
        if ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
        i += 1
    return None


def _array_end(text: str, open_idx: int) -> int:
    """方括号配对（跳过字符串字面量），返回与 text[open_idx]=='['
    匹配的闭括号下标；不配对抛 ValueError。"""
    depth = 0
    i = open_idx
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == '"':
            i += 1
            while i < n:
                if text[i] == "\\":
                    i += 2
                    continue
                if text[i] == '"':
                    break
                i += 1
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise ValueError("方括号不配对")


def _array_element_spans(text: str, arr_open: int, arr_close: int) -> list:
    """顶层数组元素 [arr_open+1, arr_close) 的文本区间（已 trim 首尾空白）：
    [(start, end), ...]。元素为字符串或对象，按顶层逗号切分。"""
    out = []
    i = arr_open + 1
    d = 0
    seg = None
    while i <= arr_close:
        ch = text[i] if i < arr_close else None
        if ch == '"':
            if seg is None:
                seg = i
            j = i + 1
            while j < arr_close:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == '"':
                    break
                j += 1
            i = j + 1
            continue
        if ch is not None and ch in "{[":
            if seg is None:
                seg = i
            d += 1
        elif ch is not None and ch in "}]":
            d -= 1
        elif (ch == "," and d == 0) or ch is None:
            if seg is not None:
                e = i
                while e > seg and text[e - 1] in " \t\r\n":
                    e -= 1
                out.append((seg, e))
            seg = None
        elif ch is not None and not ch.isspace():
            if seg is None:
                seg = i
        i += 1
    return out


def _kept_key_indices(raw_arr: list) -> list:
    """api_keys 数组中经 providers._parse_keys 去重后仍保留的原始下标（保持
    顺序）。去重口径与 _parse_keys 第一段一致：带 label 的重复保留，无 label
    的重复只留首个。"""
    kept = []
    present = set()
    for i, k in enumerate(raw_arr):
        if isinstance(k, str) and k.strip():
            key, label = k.strip(), ""
        elif isinstance(k, dict) and isinstance(k.get("key"), str) and k.get("key").strip():
            key, label = k["key"].strip(), str(k.get("label") or "").strip()
        else:
            continue  # _parse_keys 同样跳过非法条目
        if label or key not in present:
            kept.append(i)
            present.add(key)
    return kept


def _remove_member_in_text(text: str, obj_open: int, obj_close: int, span) -> str:
    """删除对象块内某成员 span（ks,ke,vs,ve），按首/中/末/唯一成员
    正确处理配套逗号。"""
    ks, ke, vs, ve = span
    # 尾逗号分支：成员后（跳空白）是逗号 → 删成员至逗号后空白
    k = ve
    while k < obj_close and text[k] in " \t\r\n":
        k += 1
    if k < obj_close and text[k] == ",":
        e = k + 1
        while e < obj_close and text[e] in " \t\r\n":
            e += 1
        return text[:ks] + text[e:]
    # 前导逗号分支：成员前（跳空白回溯）是逗号 → 删逗号至成员尾
    b = ks - 1
    while b > obj_open and text[b] in " \t\r\n":
        b -= 1
    if b > obj_open and text[b] == ",":
        return text[:b] + text[ve:]
    # 唯一成员：删成员；块内仅剩空白 → 清空 {}
    inner = text[obj_open + 1:ks] + text[ve:obj_close]
    if not inner.strip():
        return text[:obj_open + 1] + text[obj_close:]
    return text[:ks] + text[ve:]


def _set_dict_disabled(text: str, d_open: int, d_close: int, disabled: bool) -> str:
    """在对象块 [d_open, d_close]（text[d_open]=='{'）内 置/清 "disabled"
    成员：置时已有成员则改值为 true、无则块首插入 "disabled": true,（沿用
    块内首成员缩进，与 T10 enabled 同纪律）；清时删除成员（禁用→启用，
    保持 config 干净）。"""
    span = _member_span(text, d_open + 1, d_close, "disabled")
    if disabled:
        if span is not None:
            return text[:span[2]] + "true" + text[span[3]:]
        after_open = d_open + 1
        j = after_open
        while j < d_close and text[j] in " \t\r\n":
            j += 1
        if j >= d_close:
            ins = '"disabled": true'  # 空块 {}
        else:
            ws = text[after_open:j]
            if "\n" in ws:
                indent = ws[ws.rfind("\n") + 1:]
                ins = "\n" + indent + '"disabled": true,'
            else:
                ins = '"disabled": true, '
        return text[:after_open] + ins + text[after_open:]
    if span is None:
        return text  # 无 disabled 成员，本就启用态
    return _remove_member_in_text(text, d_open, d_close, span)


def _upgrade_legacy_api_key(text: str, block_open: int, block_close: int,
                            label: str, disabled: bool) -> str:
    """旧单 api_key 字段 → 折叠进 api_keys 数组（仅当无 api_keys 数组；
    两者并存属病理配置，确定性拒绝）。"""
    if _member_value_span(text, block_open + 1, block_close, "api_keys") is not None:
        raise ValueError("provider 同时含 api_key 与 api_keys 字段，请先在面板清理")
    ak = _member_span(text, block_open + 1, block_close, "api_key")
    if ak is None:
        raise ValueError("provider 无 api_key 字段")
    ks, ke, vs, ve = ak
    entry = {"key": json.loads(text[vs:ve]), "label": label}
    if disabled:
        entry["disabled"] = True
    member = '"api_keys": ' + json.dumps([entry], ensure_ascii=False)
    return text[:ks] + member + text[ve:]


def _toggle_provider_key_in_text(text: str, name: str, label: str,
                                 disabled: bool) -> str:
    """文本级结构保留式改写 config.json 全文：把 providers 节内 name 块
    api_keys 数组中 label 对应条目的 disabled 置 / 清。算法：

    1. 写前全文件 json.loads 校验（坏 JSON 直接拒绝，不落盘）
    2. N-1 前置校验：目标 provider 是对象（病理配置确定性拒绝）
    3. 定位 "providers" 节（_brace_match）→ 目标 provider 块
       （_member_value_span）→ 块内 api_keys 数组（_array_end）
    4. 归一化 key 清单（providers._parse_keys）按 label 定目标条目，经
       _kept_key_indices 映射回 api_keys 原始下标（旧单 api_key 为 -1）
    5. 定位该元素文本（_array_element_spans）：str 条目 → 升级为 dict
       文本（带 disabled）；dict 条目 → 插/删 "disabled" 成员
    6. 写后复读校验：json.loads(new) 成功、该条目 disabled 确为目标值、
       其余 provider 逐字段不变、顶层其他键不变、pricing 节逐字节保留
       ——任一失败抛 ValueError，原文一字不动
    """
    import re
    cfg = json.loads(text)  # 写前全文件校验
    provs = cfg.get("providers") if isinstance(cfg, dict) else None
    if not isinstance(provs, dict) or not isinstance(provs.get(name), dict):
        raise ValueError(
            'providers 节内无 provider「%s」（或条目非对象）' % name)
    p = provs[name]
    entries = providers._parse_keys(name, p)
    if not any(e["label"] == label for e in entries):
        raise ValueError('provider「%s」无 label「%s」的 key' % (name, label))
    raw_arr = p.get("api_keys") if isinstance(p.get("api_keys"), list) else []
    kept = _kept_key_indices(raw_arr)
    ki = next(i for i, e in enumerate(entries) if e["label"] == label)
    raw_index = kept[ki] if ki < len(kept) else -1
    target_key = entries[ki]["key"]

    m = re.search(r'"providers"\s*:\s*\{', text)
    if not m:
        raise ValueError('config.json 无 "providers" 节')
    sec_open = m.end() - 1
    sec_end = _brace_match(text, sec_open)
    pblk = _member_value_span(text, sec_open + 1, sec_end, name)
    if pblk is None:
        raise ValueError('providers 节内无 provider「%s」' % name)
    block_open = pblk[0]
    if text[block_open] != "{":
        raise ValueError('provider「%s」块非对象' % name)
    block_close = _brace_match(text, block_open)

    if raw_index == -1:
        new_text = _upgrade_legacy_api_key(text, block_open, block_close,
                                           label, disabled)
    else:
        ak = _member_value_span(text, block_open + 1, block_close, "api_keys")
        if ak is None or text[ak[0]] != "[":
            raise ValueError('provider「%s」无 api_keys 数组' % name)
        arr_open = ak[0]
        arr_close = _array_end(text, arr_open)
        spans = _array_element_spans(text, arr_open, arr_close)
        if raw_index >= len(spans):
            raise ValueError('provider「%s」api_keys 下标越界' % name)
        es, ee = spans[raw_index]
        elem = json.loads(text[es:ee])
        got_key = elem if isinstance(elem, str) else (
            elem.get("key") if isinstance(elem, dict) else None)
        if got_key != target_key:
            raise ValueError('provider「%s」api_keys[%d] 与目标 key 不一致'
                             % (name, raw_index))
        if isinstance(elem, str):
            # str 条目恒为启用态：停用 → 升级为 dict 文本；启用 → 空操作
            if disabled:
                new_elem = json.dumps(
                    {"key": elem, "label": label, "disabled": True},
                    ensure_ascii=False)
                new_text = text[:es] + new_elem + text[ee:]
            else:
                new_text = text
        elif isinstance(elem, dict):
            d_close = _brace_match(text, es)
            new_text = _set_dict_disabled(text, es, d_close, disabled)
        else:
            raise ValueError('api_keys[%d] 元素形态非法' % raw_index)

    # 写后复读校验
    check = json.loads(new_text)
    np = (check.get("providers") or {}).get(name)
    if not isinstance(np, dict):
        raise ValueError("复读校验失败：provider 丢失")
    got = next((e for e in providers._parse_keys(name, np)
                if e["label"] == label), None)
    if got is None:
        raise ValueError("复读校验失败：key「%s」丢失" % label)
    if bool(got.get("disabled")) != bool(disabled):
        raise ValueError("复读校验失败：disabled=%r（期望 %r）"
                         % (got.get("disabled"), disabled))
    if '"pricing"' in text and '"providers"' in text:
        sl = text[text.index('"pricing"'):text.index('"providers"')]
        sn = new_text[new_text.index('"pricing"'):new_text.index('"providers"')]
        if sl != sn:
            raise ValueError("复读校验失败：pricing 节被改动")
    for k2, v2 in (cfg.get("providers") or {}).items():
        if k2 == name:
            continue
        if (check.get("providers") or {}).get(k2) != v2:
            raise ValueError("复读校验失败：providers.%s 被改动" % k2)
    for k2, v2 in cfg.items():
        if k2 == "providers":
            continue
        if check.get(k2) != v2:
            raise ValueError("复读校验失败：顶层键 %s 被改动" % k2)
    return new_text


def _set_provider_key_disabled(name: str, label: str, disabled: bool) -> None:
    """key 启停落盘：文本级改写 + 备份 + 原子写（避免半程写坏）。"""
    import shutil
    from proxy_core.settings import CONFIG_FILE
    new_text = _toggle_provider_key_in_text(
        CONFIG_FILE.read_text(encoding="utf-8"), name, label, disabled)
    bak = CONFIG_FILE.with_name(
        f"{CONFIG_FILE.name}.bak-keytoggle-{time.strftime('%m%d-%H%M%S')}")
    shutil.copy2(CONFIG_FILE, bak)
    tmp = CONFIG_FILE.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(new_text)
    os.replace(tmp, CONFIG_FILE)


@bp.route("/provider/key/toggle", methods=["POST"])
@check_admin_auth
def api_provider_key_toggle():
    """启用 / 停用静态 provider 的一个 API Key（按备注 label，热生效，F14）。

    body: {"provider": "agnes", "label": "..."}（必填）+ 可选
    "disabled": bool（缺省视为 True=停用；显式 false=启用）。停用的 key
    保留在 config.json（不清除 key 本体）——额度耗尽的 key 先停用，待额度
    重置再启用；停用期间不参与路由轮换（providers.pick_key 跳过），也不
    计入面板 key_count。label 匹配口径与 key/remove 端点一致
    （providers._parse_keys 归一化，含缺省 label）。provider 不存在 404；
    atomcode/cline/minicode 等本地工具型通道 400 拒绝；label 不存在 404。
    写 config.json 用文本级结构保留式（pricing 大小写折叠重复键不因回写
    丢失），备份 config.json.bak-keytoggle-MMDD-HHMMSS 可恢复。
    """
    data = request.get_json(force=True, silent=True) or {}
    name = str(data.get("provider") or "").strip()
    label = str(data.get("label") or "").strip()
    if not name or not label:
        return jsonify({"error": "缺少 provider 或 label"}), 400
    if "disabled" in data:
        raw = data.get("disabled")
        if isinstance(raw, bool):
            disabled = raw
        elif isinstance(raw, str):
            disabled = raw.strip().lower() not in ("false", "0", "no", "")
        else:
            disabled = bool(raw)
    else:
        disabled = True  # 缺省视为停用
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
    if not any(e["label"] == label for e in providers._parse_keys(name, p)):
        return jsonify({"error": f"key「{label}」不存在"}), 404
    try:
        _set_provider_key_disabled(name, label, disabled)
    except (ValueError, OSError) as e:
        log.error("provider「%s」key「%s」启停写盘失败: %s", name, label, e)
        return jsonify({"error": f"写 config.json 失败: {e}"}), 500
    providers.all_providers()  # 立即触发重载（mtime 热生效）
    _provider_cache.update(ts=0, data=None)  # 击穿面板缓存，卡片即时反映
    ents = providers._parse_keys(
        name, (load_config().get("providers") or {}).get(name) or {})
    kc = sum(1 for e in ents if not e.get("disabled"))
    dc = sum(1 for e in ents if e.get("disabled"))
    activity.add("info",
                 f"Provider「{name}」key「{label}」"
                 f"{'停用' if disabled else '启用'}（启用 {kc} / 停用 {dc}）",
                 model=name)
    return jsonify({"provider": name, "label": label,
                    "disabled": bool(disabled),
                    "key_count": kc, "key_disabled_count": dc})


# ─── 一键探测上游可用模型（F2）────────────────────────────────
# 卡片「探测模型」：GET {base_url}/models（带第一个 key）
# 拿候选 → keyword 子串过滤（大小写不敏感）→ 最多探 40
# 个、并发 4（每个 1-token chat，单次超时 20s）→ 分类
# 汇总；勾选后经 /providers/<name>/models 文本级写回。

_PROBE_MAX = 40          # 最多探测模型数
_PROBE_CONCURRENCY = 4   # 并发上限
_PROBE_TIMEOUT_S = 20    # 单次探测超时（秒）


def _provider_first_key(name: str, p: dict):
    """provider 的第一个 key（探测鉴权用；无 key 返回 None）。"""
    entries = providers._parse_keys(name, p)
    return entries[0]["key"] if entries else None


def _provider_first_enabled_key(name: str, p: dict):
    """provider 的第一个**启用中**的 key（刷新模型鉴权用）。

    与 _provider_first_key 的差别：跳过 F14 停用的 key（额度耗尽停用
    的凭证不应再拿去访问上游）；全部停用返回 None。
    """
    for entry in providers._parse_keys(name, p):
        if not entry.get("disabled"):
            return entry["key"]
    return None


def _upstream_model_ids(payload) -> list:
    """从 GET /models 响应体提取模型 id 列表（兼容
    OpenAI {"data":[...]} / 纯对象数组 / 纯字符串数组
    三种形态）。"""
    items = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        return []
    out = []
    for it in items:
        if isinstance(it, str) and it.strip():
            out.append(it.strip())
        elif (isinstance(it, dict) and isinstance(it.get("id"), str)
                and it["id"].strip()):
            out.append(it["id"].strip())
    return out


class _UpstreamModelsError(Exception):
    """上游模型列表拉取失败（异常文本即可读错误文案）。"""


_UPSTREAM_MODELS_TIMEOUT_S = 15   # 只拉列表不测活，比探测宽松即可
_UPSTREAM_HTTP_HINTS = {
    400: "请求无效",
    401: "API Key 无效或无权限",
    403: "API Key 无权访问模型列表",
    404: "上游无 /models 端点",
    429: "上游限流，请稍后重试",
}


def _fetch_upstream_models(p: dict, base_url: str, key: str,
                           timeout: int = _UPSTREAM_MODELS_TIMEOUT_S) -> list:
    """GET {base_url}/models（带 provider 的 key）→ 上游模型 id 列表。

    探测（T11）与「刷新模型」共用的唯一拉取实现：per-provider 代理
    出口 + trust_env=False（R-B）；非 200 / 超时 / 连接失败一律归一为
    _UpstreamModelsError（带可读中文文案，探测端点转 502、刷新端点
    转 ok=false）。
    """
    headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}
    try:
        with providers.make_client(p, timeout) as client:
            r = client.get(base_url + "/models", headers=headers)
    except httpx.TimeoutException:
        raise _UpstreamModelsError(f"连接 {base_url} 超时（>{timeout}s）")
    except Exception as e:
        raise _UpstreamModelsError(f"连接 {base_url} 失败: {str(e)[:120]}")
    if r.status_code != 200:
        hint = _UPSTREAM_HTTP_HINTS.get(r.status_code) or "上游返回异常"
        raise _UpstreamModelsError(
            f"上游 GET /models 返回 {r.status_code}（{hint}）")
    try:
        return _upstream_model_ids(r.json())
    except Exception as e:
        raise _UpstreamModelsError(f"/models 响应解析失败: {e}")


def _probe_one(client, base_url: str, key: str, model_id: str):
    """单模型可用性探测：1-token chat（max_tokens=1），
    单次超时 _PROBE_TIMEOUT_S。返回 (ok_item, fail_item)
    恰有一个非 None。"""
    t0 = time.perf_counter()
    try:
        r = client.post(
            base_url + "/chat/completions",
            headers={"Authorization": f"Bearer {key}",
                     "Accept": "application/json"},
            json={"model": model_id,
                  "messages": [{"role": "user", "content": "hi"}],
                  "max_tokens": 1})
        ms = int((time.perf_counter() - t0) * 1000)
        if 200 <= r.status_code < 300:
            return {"id": model_id, "latency_ms": ms}, None
        why = ""
        try:
            err = r.json().get("error")
            why = str(err)[:120] if err else ""
        except Exception:
            why = (r.text or "")[:120]
        return None, {"id": model_id, "status": r.status_code,
                      "why": why or f"HTTP {r.status_code}"}
    except httpx.TimeoutException:
        return None, {"id": model_id, "status": "timeout",
                      "why": f"超时（>{_PROBE_TIMEOUT_S}s）"}
    except Exception as e:
        return None, {"id": model_id, "status": "error",
                      "why": str(e)[:120]}


@bp.route("/providers/<name>/probe", methods=["POST"])
@check_admin_auth
def api_provider_probe(name):
    """一键探测上游可用模型。

    body: {"keyword": ""}（可空）。流程：GET {base_url}/
    models（带该 provider 第一个 key）拿候选 id → keyword
    子串过滤（大小写不敏感）→ 最多探 40 个、并发 4（每个
    1-token chat，单次超时 20s）→ 分类汇总。base_url 不通
    / 无 /models 端点 → 502 + 可读错误。
    """
    from concurrent.futures import ThreadPoolExecutor
    data = request.get_json(force=True, silent=True) or {}
    keyword = str(data.get("keyword") or "").strip()
    name = str(name or "").strip()
    provs = load_config().get("providers") or {}
    p = provs.get(name)
    if not isinstance(p, dict):
        return jsonify({"error": f"provider「{name}」不存在"}), 404
    base_url = str(p.get("base_url") or "").rstrip("/")
    key = _provider_first_key(name, p)
    if not base_url or not key:
        return jsonify({"error": f"provider「{name}」配置不完整（缺 base_url/api_keys）"}), 400
    try:
        # R-B：探针同样走 per-provider 代理出口（面板探测 403 而转发
        # 成功的自相矛盾 UX 必须避免）；trust_env=False + 一次性 client。
        candidates = _fetch_upstream_models(p, base_url, key, timeout=10)
    except _UpstreamModelsError as e:
        return jsonify({"error": str(e)}), 502
    if keyword:
        kl = keyword.lower()
        candidates = [m for m in candidates if kl in m.lower()]
    probe_set = candidates[:_PROBE_MAX]
    ok, fail = [], []
    if probe_set:
        # R-B：探测 client 同样走 per-provider 代理出口
        with providers.make_client(p, _PROBE_TIMEOUT_S) as client:
            with ThreadPoolExecutor(max_workers=_PROBE_CONCURRENCY) as pool:
                futs = [pool.submit(_probe_one, client, base_url, key, m)
                        for m in probe_set]
                for fut in futs:
                    ok_item, fail_item = fut.result()
                    if ok_item:
                        ok.append(ok_item)
                    else:
                        fail.append(fail_item)
    return jsonify({"total": len(candidates), "probed": len(probe_set),
                    "ok": ok, "fail": fail})


# ─── 刷新上游模型列表（「刷新模型」按钮，轻量、不测活）─────────────
# GET /providers/<name>/models/upstream：只拉上游 GET /models 的 id
# 列表（带第一个启用中的 key），不逐个 1-token 测活——上游新增模型
# 后可一键勾选写回（POST /providers/<name>/models），免手动输入 ID。
# 响应契约：除 provider 不存在的本地校验外一律 HTTP 200，由前端看
# ok 字段判断（上游 401/404/超时都进 ok=false + 可读 error 文案）。


@bp.route("/providers/<name>/models/upstream", methods=["GET"])
@check_admin_auth
def api_provider_models_upstream(name):
    """拉取上游最新模型列表（刷新模型，不测活）。

    仅对标准外部 provider 开放：带 type 字段的特殊通道
    （atomcode/cline/minicode/opencode-serve 等——无静态 key 或
    上游协议/结构与 OpenAI /models 不同）返回明确错误，不发起
    任何上游请求。
    """
    name = str(name or "").strip()
    p = (load_config().get("providers") or {}).get(name)
    if not isinstance(p, dict):
        return jsonify({"ok": False,
                        "error": f"provider「{name}」不存在"}), 404
    if p.get("type"):
        return jsonify({"ok": False,
                        "error": "该渠道类型不支持列表刷新"})
    base_url = str(p.get("base_url") or "").rstrip("/")
    if not base_url:
        return jsonify({"ok": False,
                        "error": f"provider「{name}」配置不完整（缺 base_url）"})
    key = _provider_first_enabled_key(name, p)
    if not key:
        return jsonify({"ok": False,
                        "error": f"provider「{name}」无启用中的 API Key"})
    try:
        models = _fetch_upstream_models(p, base_url, key)
    except _UpstreamModelsError as e:
        return jsonify({"ok": False, "error": str(e)})
    return jsonify({"ok": True, "models": models})


def _set_provider_models(name: str, models: list) -> None:
    """models 列表写回 provider 配置：文本级结构保留式
    （复用 _member_value_span 定位直辖 models 数组原地
    替换；无 models 键则块首插入），备份 + 原子写。"""
    import re
    import shutil
    from proxy_core.settings import CONFIG_FILE
    text = CONFIG_FILE.read_text(encoding="utf-8")
    cfg = json.loads(text)  # 写前全文件校验
    # N-1：文本手术前确认目标 provider 是对象（病理配置
    # 确定性拒绝——非 dict 条目直接 ValueError，原文不动）
    provs = cfg.get("providers") if isinstance(cfg, dict) else None
    if not isinstance(provs, dict) or not isinstance(provs.get(name), dict):
        raise ValueError(
            'providers 节内无 provider「%s」（或条目非对象）' % name)
    m = re.search(r'"providers"\s*:\s*\{', text)
    if not m:
        raise ValueError('config.json 无 "providers" 节')
    sec_open = m.end() - 1
    sec_end = _brace_match(text, sec_open)
    pat = re.compile('"' + re.escape(name) + r'"\s*:\s*\{')
    pm = pat.search(text, m.end(), sec_end)
    if not pm:
        raise ValueError('providers 节内无 provider「%s」' % name)
    block_open = pm.end() - 1
    block_end = _brace_match(text, block_open)
    new_val = json.dumps(models, ensure_ascii=False)
    span = _member_value_span(text, block_open + 1, block_end, "models")
    if span:
        new_text = text[:span[0]] + new_val + text[span[1]:]
    else:
        # 无 models 键：块首插入（沿用 T10 enabled 插入同款缩进逻辑）
        after_open = block_open + 1
        j = after_open
        while j < block_end and text[j] in " \t\r\n":
            j += 1
        if j >= block_end:
            ins = '"models": ' + new_val
        else:
            ws = text[after_open:j]
            if "\n" in ws:
                indent = ws[ws.rfind("\n") + 1:]
                ins = "\n" + indent + '"models": ' + new_val + ","
            else:
                ins = '"models": ' + new_val + ", "
        new_text = text[:after_open] + ins + text[after_open:]
    # 写后复读校验：解析成功且 models 确已落地
    check = json.loads(new_text)
    got = ((check.get("providers") or {}).get(name) or {}).get("models")
    if got != models:
        raise ValueError("复读校验失败：providers.%s.models 未正确落地" % name)
    bak = CONFIG_FILE.with_name(
        f"{CONFIG_FILE.name}.bak-provmodels-{time.strftime('%m%d-%H%M%S')}")
    shutil.copy2(CONFIG_FILE, bak)
    tmp = CONFIG_FILE.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(new_text)
    os.replace(tmp, CONFIG_FILE)


@bp.route("/providers/<name>/models", methods=["POST"])
@check_admin_auth
def api_provider_models(name):
    """把探测结果选中的模型写回 provider 配置（F2 收尾）。

    body: {"models": ["id1", ...]}。文本级结构保留式替换
    该 provider 块的 models 数组（pricing 重复键不丢失）；
    写后该 provider 模型立即进 /v1/models（守卫白名单每
    请求读 config，天然生效）。
    """
    data = request.get_json(force=True, silent=True) or {}
    models = data.get("models")
    if (not isinstance(models, list)
            or not all(isinstance(m, str) and m.strip() for m in models)):
        return jsonify({"error": "models 必须为字符串数组"}), 400
    name = str(name or "").strip()
    if not isinstance((load_config().get("providers") or {}).get(name), dict):
        return jsonify({"error": f"provider「{name}」不存在"}), 404
    models = [m.strip() for m in models]
    try:
        _set_provider_models(name, models)
    except (ValueError, OSError) as e:
        log.error("provider「%s」models 写盘失败: %s", name, e)
        return jsonify({"error": f"写 config.json 失败: {e}"}), 500
    providers.all_providers()  # 立即触发重载（mtime 热生效）
    _provider_cache.update(ts=0, data=None)  # 击穿面板缓存，卡片即时反映
    activity.add("info",
                 f"Provider「{name}」模型列表已更新（{len(models)} 个）",
                 model=name)
    return jsonify({"provider": name, "models": models})


# ─── 账号分组标签（F3）────────────────────────────────────────
# 用户原需求：到期订阅的账号自己改成 free。accounts.json
# 侧无现成 helper——新建：条目 span 编辑（"user_id": "X"
# 到下一条 "user_id" 或文本尾为边界），直属 "group" 值
# 替换；无 group 键则在条目内末尾前插入。文本级保留式
# （不重排、不丢字段），与 T10 config 手术同纪律。

_ACCOUNT_GROUP_VALUES = ("", "free", "pro")


def _set_account_group(uid: str, group: str) -> None:
    """accounts.json 条目 span 编辑：把 uid 账号的 group
    设为 group。算法：

    1. 写前全文件 json.loads 校验
    2. 定位条目：首次出现 "user_id": "<uid>"（键与值间
       空白任意）；条目 span = 该处到下一条 "user_id"
       或文本尾
    3. span 内直辖成员扫描（_member_value_span，深度跟
       踪只认本层键）：
       - 有 "group" 成员 → 原地替换其值为新 group
       - 无 → 条目闭括号前插入 "group": <值>（沿用成员
         缩进；逗号补在 head 末尾作前成员与新成员的分
         隔——新成员是末成员，自身不带尾逗号；紧凑/
         多行两分支同此）
    4. 写后复读校验：json.loads(new) 成功、目标账号
       group 确为 group、且其余账号逐字段不变
    """
    import re
    import shutil
    from proxy_core.settings import ACCOUNTS_FILE
    from proxy_core.state import am

    def _accs(txt):
        d = json.loads(txt)
        return (d.get("accounts") or []) if isinstance(d, dict) else d

    text = ACCOUNTS_FILE.read_text(encoding="utf-8")
    json.loads(text)  # 写前全文件校验
    anchor = re.compile(
        r'"user_id"\s*:\s*' + re.escape(json.dumps(uid, ensure_ascii=False)))
    mm = anchor.search(text)
    if not mm:
        raise ValueError(f"accounts.json 中无账号「{uid}」")
    entry_start = mm.start()
    nxt = re.compile(r'"user_id"\s*:')
    nm = nxt.search(text, mm.end())
    entry_end = nm.start() if nm else len(text)
    val = json.dumps(group, ensure_ascii=False)
    span = _member_value_span(text, entry_start, entry_end, "group")
    if span:
        new_text = text[:span[0]] + val + text[span[1]:]
    else:
        # 无 group 键：条目闭括号前插入（user_id 是首成员，
        # 其前最近的 { 即条目开头）
        open_idx = text.rfind("{", 0, entry_start)
        close_idx = _brace_match(text, open_idx)
        k = close_idx
        while k > 0 and text[k - 1] in " \t\r\n":
            k -= 1
        ws = text[k:close_idx]
        if "\n" in ws:
            close_indent = ws[ws.rfind("\n") + 1:]
            member_indent = close_indent + "  "
            head = text[:k]
            if not head.rstrip().endswith(","):
                head += ","  # 新成员前补逗号（原末成员无尾逗号）
            ins = "\n" + member_indent + '"group": ' + val + "\n" + close_indent
            new_text = head + ins + text[close_idx:]
        else:
            # 紧凑分支：逗号补在 head 末尾（分隔前成员与
            # 新成员），ins 不含尾逗号——新成员是末成员，
            # 尾逗号落在 } 前是 JSON 语法错
            head = text[:k]
            if head.rstrip() and not head.rstrip().endswith("{"):
                head += ","
            ins = '"group": ' + val
            new_text = head + ins + text[close_idx:]
    # 写后复读校验：解析成功、目标 group 落地、其余账号不变
    before = _accs(text)
    after = _accs(new_text)
    if len(before) != len(after):
        raise ValueError("复读校验失败：账号数变化")
    tgt = next((a for a in after if a.get("user_id") == uid), None)
    if tgt is None or tgt.get("group") != group:
        raise ValueError("复读校验失败：账号 group 未正确落地")
    for ob, oa in zip(before, after):
        if ob.get("user_id") == uid:
            continue  # 目标账号（已单独校验）
        if ob != oa:
            raise ValueError("复读校验失败：其他账号被改动")
    bak = ACCOUNTS_FILE.with_name(
        f"{ACCOUNTS_FILE.name}.bak-acctgroup-{time.strftime('%m%d-%H%M%S')}")
    shutil.copy2(ACCOUNTS_FILE, bak)
    tmp = ACCOUNTS_FILE.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(new_text)
    os.replace(tmp, ACCOUNTS_FILE)
    am._load()  # AccountManager 单例内存热重载


@bp.route("/accounts/<uid>/group", methods=["POST"])
@check_admin_auth
def api_account_group(uid):
    """账号分组标签选择器（F3）。

    body: {"group": ""|"free"|"pro"}——严格白名单三值
    （""=订阅池 / free=免费组 / pro=专业组），其他值
    400。accounts.json 条目 span 文本级编辑（不碰其他
    账号），备份 + 原子写，AccountManager 热重载生效。
    """
    import re
    from proxy_core.settings import ACCOUNTS_FILE
    data = request.get_json(force=True, silent=True) or {}
    group = data.get("group")
    if group not in _ACCOUNT_GROUP_VALUES:
        return jsonify({"error": "group 仅允许 \"\"/free/pro 三值"}), 400
    uid = str(uid or "").strip()
    if not re.search(
            r'"user_id"\s*:\s*' + re.escape(json.dumps(uid, ensure_ascii=False)),
            ACCOUNTS_FILE.read_text(encoding="utf-8")):
        return jsonify({"error": f"账号「{uid}」不存在"}), 404
    try:
        _set_account_group(uid, group)
    except (ValueError, OSError) as e:
        log.error("账号「%s」分组写盘失败: %s", uid, e)
        return jsonify({"error": f"写 accounts.json 失败: {e}"}), 500
    return jsonify({"user_id": uid, "group": group})



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
    if not am.add_account(user_id, token, data.get("username", ""), data.get("org_id", ""),
                          data.get("weight", 1)):
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
                "enabled": prov.get("enabled", True),
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
                "enabled": prov.get("enabled", True),
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
                "enabled": prov.get("enabled", True),
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
            "enabled": prov.get("enabled", True),
            "base_url": prov["base_url"],
            "models": prov["models"] + ["{} ⇒ {}".format(a, t) for a, t in _amap.items() if a not in prov["models"]],
            "ok": False,
        }
        if prov.get("keys"):
            # F14: key_count 只数参与路由的（未停用）key——路由真相；
            # key_disabled_count 为停用数（额度耗尽先停用，面板显示"N 停用"）。
            enabled_keys = [k for k in prov["keys"] if not k.get("disabled")]
            item["key_count"] = len(enabled_keys)
            item["key_disabled_count"] = len(prov["keys"]) - len(enabled_keys)
            item["key_entries"] = [
                {"label": k["label"], "preview": _mask_key(k["key"]),
                 "disabled": bool(k.get("disabled")),
                 # R-E 权重（面板徽章：有 weight 且 ≠1 时显示）
                 "weight": k.get("weight", 1)}
                for k in prov["keys"]
            ]
        headers = {"Authorization": f"Bearer {prov['api_key']}", "Accept": "application/json"}
        try:
            # R-B：面板探针走 per-provider 代理出口（与转发路径一致性）。
            # prov 是已解析 dict（带 proxy_resolved），effective_proxy_url
            # 直接取有效值。
            with providers.make_client(prov) as client:
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
    from proxy_core import budget as budget_mod
    from proxy_core.settings import get_api_key_entries
    usage = {e["label"]: e for e in token_stats.get_key_stats()}
    out = []
    for e in get_api_key_entries():
        u = usage.get(e["label"], {})
        row = {
            "label": e["label"],
            "key_preview": e["key_preview"],
            "daily_token_limit": e["limit"],
            "today_tokens": u.get("total_tokens", 0),
            "today_requests": u.get("requests", 0),
            "today_models": u.get("models", {}),
        }
        cfg = e.get("budget")
        if cfg:
            # 客户端预算（阶段 C / 方案 §5.4）：配置了
            # limit 块的 key 才曝光预算快照（无 limit
            # 的 key 不加 budget 键）。
            row["budget"] = budget_mod.snapshot(e["label"], cfg)
        out.append(row)
    out.sort(key=lambda x: x["today_tokens"], reverse=True)
    return jsonify({"keys": out})


# ─── 注水检测 ────────────────────────────────────────────────────────────────

@bp.route("/watercheck")
@check_admin_auth
def api_watercheck():
    """注水检测面板数据：各目标最近一次探针结果 + 被动捕获的可疑事件。"""
    return jsonify(water.status())


@bp.route("/stats/quality")
@check_admin_auth
def api_stats_quality():
    """账号资格评分 v0（方案 §6）：按 (账号, 模型) 聚合流式
    双口径 TTFT p50 / tokens_per_sec 均值 / 样本数 / 注水
    嫌疑命中。

    数据源 = water_check.json perf 结构（water 单例，与
    /api/watercheck 同源读写，不另起解析）。is_stream 分桶：
    非流式样本直接排除（见响应 bucketing 字段）；water 不记
    成功/失败计数，success_rate 不可用，以 samples 代替
    （见 success_rate_note）。
    """
    try:
        return jsonify(water.quality_rows())
    except Exception as e:
        log.error("质量聚合生成失败: %s", e)
        return jsonify({"error": str(e)[:200]}), 500


@bp.route("/corpus/stats")
@check_admin_auth
def api_corpus_stats():
    """语料采集面板数据（阶段 E / 方案 §7.6）：
    今日/本月条数与 token 量、按 model/role 分布、
    water_suspect 数、redact_hits 总数、丢弃计数、
    队列积压。"""
    try:
        from proxy_core.corpus import worker as _corpus_worker
        return jsonify(_corpus_worker.stats())
    except Exception as e:
        log.error("语料统计生成失败: %s", e)
        return jsonify({"error": str(e)[:200]}), 500


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
