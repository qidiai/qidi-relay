# -*- coding: utf-8 -*-
"""Cline 通道：官方客户端凭证池 + WorkOS token 刷新 + 免费通道风控守卫。

原理（2026-09-24 逆向自社区 cline2api，本机 Cline 桌面版实测验证）：
- Cline 官方客户端（VS Code 插件 / 桌面版）把登录凭证写在
  %USERPROFILE%\\.cline\\data\\settings\\providers.json 的
  providers.cline.settings.auth：accessToken（1 小时有效的 WorkOS JWT，
  文件里带 "workos:" 前缀）/ refreshToken（长期有效，刷新时轮换）/ expiresAt(ms)。
- 官方客户端运行期间自动续期并回写该文件；本模块 mtime 感知加载，
  客户端刷新后下一次请求自动用新 token。token 过期且客户端没在续（未运行）
  时本模块自己刷新，并把轮换后的 refreshToken 原子写回同一文件——
  单一事实源：客户端重启后读到最新凭证，两边不会互踢。
- 上游 https://api.cline.bot/api/v1 是 OpenAI 兼容协议，但有三道风控：
  1) 客户端指纹头：缺 X-CLIENT-TYPE: cline-sdk 等会
     403 "only available via Cline product surfaces"
  2) 免费模型（deepseek/ cline-free/ cline-pass/ 前缀或 :free 后缀）请求体
     带 max_tokens 一律 500 "empty response content" → 构造上游体时剥离
  3) 免费通道非流式被上游限流 → 客户端要非流式时强制上游流式再聚合；
     并发>1 会返回空响应 → 请求发起串行门 + 最小间隔
- 401 "Please make sure you're using the latest version of Cline..."：
  token 失效（典型原因：Authorization 拼了双 workos: 前缀，或 token 被轮换）。
- 429 "Daily free limit reached ... Try again in 2h 51m"：账号日配额，
  解析冷却时长挂起通道（多账号时 pick_channel 自动换下一个）。

config.json 示例（多账号 = 多份 providers.json 副本，label 区分）：
  "providers": {
    "cline": {
      "type": "cline",
      "auth_paths": [
        {"path": "C:/Users/?/.cline/data/settings/providers.json", "label": "主号"}
      ],
      "models": ["cline-free/deepseek-v4.1-flash", "z-ai/glm-5.3-flash"]
    }
  }
向后兼容：单个 "auth_path": "..." 字符串仍有效（= 单账号，label 用通道名）。
"""

import json
import logging
import os
import re
import threading
import time
from pathlib import Path

from .settings import BASE_DIR
from . import cooldown_store

log = logging.getLogger("qidi-relay")

API_BASE = "https://api.cline.bot/api/v1"
AUTH_REFRESH_PATH = "/auth/refresh"

# 客户端指纹（社区 cline2api 2026-09 实测可用版本；上游只认头不看真安装版本）
CLIENT_VERSION = "3.0.47"
CORE_VERSION = "0.0.66"

# 免费通道模型前缀/后缀（决定 剥 max_tokens / 强制流式 / 串行门 生效范围）
FREE_PREFIXES = ("deepseek/", "cline-free/", "cline-pass/")
FREE_SUFFIX = ":free"

# 免费通道风控参数（对齐 cline2api 实测值）
MIN_REQUEST_GAP_S = 0.8     # 两次上游请求发起的最小间隔
TOKEN_EXPIRY_MARGIN_MS = 120_000   # accessToken 提前 2 分钟视为过期
EMPTY_COOLDOWN_S = 30        # 空响应（只有 reasoning 无 content）短冷却
ERROR_COOLDOWN_S = 60        # 5xx empty response content 冷却
DAILY_LIMIT_DEFAULT_S = 300   # 429 无 "Try again in" 提示时的默认冷却
MAX_DAILY_COOLDOWN_S = 6 * 3600


def is_free_model(model: str) -> bool:
    """是否走免费通道（决定 max_tokens 剥离 / 强制流式 / 串行门）。"""
    m = str(model or "")
    return m.startswith(FREE_PREFIXES) or m.endswith(FREE_SUFFIX)


def _default_auth_path() -> str | None:
    p = Path.home() / ".cline" / "data" / "settings" / "providers.json"
    return str(p) if p.exists() else None


def _resolve_path(p: str) -> str:
    """相对路径相对代理根目录解析（auths/ 下的凭证副本）。"""
    q = Path(p)
    if not q.is_absolute():
        return str((BASE_DIR / q).resolve())
    return p


def _strip_prefix(token: str) -> str:
    """文件里的 accessToken 带 workos: 前缀，内部统一存裸 JWT。"""
    t = str(token or "")
    return t[7:] if t.startswith("workos:") else t


class ClineCredential:
    """providers.json 里 providers.cline.settings.auth 的解析结果。"""

    def __init__(self, access_token: str, refresh_token: str,
                 expires_at_ms: int, email: str = ""):
        self.access_token = access_token        # 裸 JWT（无 workos: 前缀）
        self.refresh_token = refresh_token
        self.expires_at_ms = expires_at_ms      # unix 毫秒；0 = 未知
        self.email = email

    @property
    def expired(self) -> bool:
        if not self.expires_at_ms:
            return False
        return time.time() * 1000 >= self.expires_at_ms - TOKEN_EXPIRY_MARGIN_MS


def parse_providers_json(path: str) -> ClineCredential:
    """解析 Cline 客户端的 providers.json（只认 providers.cline 一支）。"""
    doc = json.load(open(path, encoding="utf-8"))
    node = doc
    for key in ("providers", "cline", "settings", "auth"):
        if not isinstance(node, dict) or key not in node:
            raise ValueError(f"providers.json 结构不符：缺 {key} 节点（{path}）")
        node = node[key]
    at = node.get("accessToken") or ""
    rt = node.get("refreshToken") or ""
    if not at or not rt:
        raise ValueError("providers.json 缺 accessToken/refreshToken（未登录？）")
    exp = node.get("expiresAt")
    try:
        exp = int(exp) if exp is not None else 0
    except (TypeError, ValueError):
        exp = 0
    email = ""
    try:
        email = (node.get("metadata") or {}).get("userInfo", {}).get("email", "")
    except Exception:
        pass
    return ClineCredential(_strip_prefix(at), rt, exp, email or "")


class ClineChannel:
    """一个 Cline 账号通道：一份 providers.json + 独立刷新/冷却。

    多账号 = 一个 provider 下多个 ClineChannel（各自一份凭证文件副本），
    label 用于负载跟踪与用量记账，必须全局唯一。
    """

    def __init__(self, name: str, cfg: dict, label: str = ""):
        self.name = name
        self.label = label or name
        self._lock = threading.Lock()
        # 刷新互斥独立于凭证读写锁：网络刷新期间仍允许 status() 读取旧状态，
        # 但同一通道绝不允许两个线程同时轮换 refreshToken（双刷必有一边 invalid_grant）。
        self._refresh_lock = threading.Lock()
        self._auth_mtime = None
        self._cred: ClineCredential | None = None
        ap = cfg.get("auth_path") or _default_auth_path()
        self.auth_path = _resolve_path(ap) if ap else None
        self.cooldown_until = 0.0
        self.cooldown_note = ""
        self.last_error: str | None = None

    # ─── 凭证 ─────────────────────────────────────────────
    def _load_cred(self) -> ClineCredential:
        """mtime 感知加载凭证文件（客户端续期后自动生效）。"""
        if not self.auth_path or not Path(self.auth_path).exists():
            raise FileNotFoundError(
                f"Cline providers.json 不存在（{self.auth_path}）；"
                f"请先登录 Cline 官方客户端（VS Code 插件或桌面版）")
        st = os.stat(self.auth_path)
        if self._cred and self._auth_mtime == st.st_mtime:
            return self._cred
        cred = parse_providers_json(self.auth_path)
        with self._lock:
            self._auth_mtime = st.st_mtime
            self._cred = cred
        log.info("[cline] 凭证已加载/更新: %s email=%s (mtime=%s)",
                 self.label, cred.email,
                 time.strftime("%m-%d %H:%M:%S", time.localtime(st.st_mtime)))
        return cred

    def _write_back(self, cred: ClineCredential):
        """把刷新后的凭证原子写回 providers.json（保留其余字段与文件格式约定：
        accessToken 带 workos: 前缀，与官方客户端写盘格式一致）。

        丢失更新守卫（0924 审计 L-2）：官方客户端与代理共享此文件，
        读-改-写期间客户端可能恰好回写续期。写盘前比对 mtime，不一致则放弃
        本次写回（文件里是客户端刚续的新凭证，以文件为准），并作废缓存
        强制下次重读。os.replace 本身原子 ✓。
        """
        if not self.auth_path:
            return
        tmp = self.auth_path + ".relay-tmp"
        try:
            st0 = os.stat(self.auth_path)
            doc = json.load(open(self.auth_path, encoding="utf-8"))
            node = doc
            for key in ("providers", "cline", "settings", "auth"):
                if not isinstance(node, dict) or key not in node:
                    raise ValueError(f"写回失败：缺 {key} 节点")
                node = node[key]
            node["accessToken"] = f"workos:{cred.access_token}"
            node["refreshToken"] = cred.refresh_token
            if cred.expires_at_ms:
                node["expiresAt"] = cred.expires_at_ms
            with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                json.dump(doc, f, ensure_ascii=False, indent=2)
            if os.stat(self.auth_path).st_mtime != st0.st_mtime:
                log.warning("[cline] %s 写回期间文件被官方客户端更新，放弃写回（以文件为准）",
                            self.label)
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                with self._lock:
                    self._auth_mtime = None   # 强制下次重读
                    self._cred = None
                return
            os.replace(tmp, self.auth_path)
            with self._lock:
                self._auth_mtime = os.stat(self.auth_path).st_mtime
                self._cred = cred
            log.info("[cline] %s 刷新后的凭证已写回 %s", self.label, self.auth_path)
        except Exception as e:
            log.error("[cline] %s 写回凭证失败: %s", self.label, e)

    def _refresh(self, old_rt: str) -> ClineCredential:
        """用 refreshToken 换新 accessToken（Cline 刷新时会轮换 refreshToken）。

        通道级互斥放在网络请求外层：并发请求同时发现 token 过期时，只允许
        一个线程真正刷新；其余线程等待后直接复用新凭证，避免 refreshToken
        被二次轮换导致 invalid_grant。
        """
        with self._refresh_lock:
            return self._refresh_locked(old_rt)

    def _refresh_locked(self, old_rt: str) -> ClineCredential:
        """刷新互斥内的实际实现（新 token 必须落盘，否则下次刷新 invalid_grant）。"""
        # 并发刷新去重（0924 审计 L-5，对齐 atomcode 先例）：别的线程刚刷新完
        # （refreshToken 已轮换成新的）→ 直接复用，避免双刷撞 invalid_grant
        with self._lock:
            cur = self._cred
        if cur is not None and cur.refresh_token and cur.refresh_token != old_rt \
                and not cur.expired:
            return cur
        import httpx
        r = httpx.post(API_BASE + AUTH_REFRESH_PATH,
                       json={"refreshToken": old_rt, "grantType": "refresh_token"},
                       headers={"Content-Type": "application/json",
                                "User-Agent": "Mozilla/5.0"},
                       timeout=15)
        if r.status_code != 200:
            # 官方客户端可能并发刷新把 refreshToken 轮换了：
            # 作废缓存重读文件，用文件里的新 refreshToken 再试一次
            self._auth_mtime = None
            self._cred = None
            try:
                cred = self._load_cred()
                if cred.refresh_token and cred.refresh_token != old_rt:
                    r = httpx.post(
                        API_BASE + AUTH_REFRESH_PATH,
                        json={"refreshToken": cred.refresh_token,
                              "grantType": "refresh_token"},
                        headers={"Content-Type": "application/json",
                                 "User-Agent": "Mozilla/5.0"},
                        timeout=15)
                    old_rt = cred.refresh_token
            except Exception:
                pass
        if r.status_code != 200:
            raise RuntimeError(
                f"Cline token 刷新失败 HTTP {r.status_code}: {r.text[:200]}")
        d = (r.json() or {}).get("data") or {}
        at = str(d.get("accessToken") or "")
        rt = str(d.get("refreshToken") or "").strip() or old_rt
        exp = d.get("expiresAt")
        try:
            exp = int(exp) if exp is not None else 0
        except (TypeError, ValueError):
            exp = 0
        if not at:
            raise RuntimeError("Cline 刷新响应缺 accessToken")
        cred = ClineCredential(_strip_prefix(at), rt,
                               exp or int(time.time() * 1000) + 600_000,
                               self._cred.email if self._cred else "")
        self._write_back(cred)
        return cred

    def get_token(self) -> str:
        """取有效 accessToken（裸 JWT）；过期自动刷新（失败抛异常由调用方处理）。"""
        cred = self._load_cred()
        if cred.expired:
            log.info("[cline] %s accessToken 已过期，自动刷新...", self.label)
            cred = self._refresh(cred.refresh_token)
        return cred.access_token

    def invalidate(self):
        """401 后作废内存缓存：下次 get_token 强制重读文件/刷新。"""
        with self._lock:
            self._auth_mtime = None
            self._cred = None

    # ─── 冷却 ─────────────────────────────────────────────
    def set_cooldown(self, seconds: float, note: str = ""):
        self.cooldown_until = time.time() + max(0, seconds)
        self.cooldown_note = (note or "")[:160]
        # R-A：通道级冷却持久化（键= auth 路径，跨重启稳定；
        # 重载/重启后按同一稳定键回灌）
        cooldown_store.set_channel(self.name, self.label, self.cooldown_until,
                                  auth_path=self.auth_path)
        log.warning("[cline] %s 冷却 %ss: %s", self.label,
                    round(seconds), self.cooldown_note)

    @property
    def available(self) -> bool:
        return time.time() >= self.cooldown_until

    # ─── 请求头 ───────────────────────────────────────────
    def request_headers(self, token: str, session_id: str) -> dict:
        """一次请求的完整指纹头（缺头会被上游 403 拒识别为非 Cline 客户端）。"""
        return {
            "Authorization": f"Bearer workos:{token}",
            "Content-Type": "application/json",
            "User-Agent": f"Cline/{CLIENT_VERSION}",
            "HTTP-Referer": "https://cline.bot",
            "X-Title": "Cline",
            "X-IS-MULTIROOT": "false",
            "X-CLIENT-TYPE": "cline-sdk",
            "X-CLIENT-VERSION": CLIENT_VERSION,
            "X-PLATFORM": "terminal",
            "X-PLATFORM-VERSION": CLIENT_VERSION,
            "X-CORE-VERSION": CORE_VERSION,
            "X-Task-ID": session_id,
        }

    # ─── 状态（供管理面板） ────────────────────────────────
    def status(self) -> dict:
        try:
            cred = self._load_cred()
            err = None
        except Exception as e:
            cred = None
            err = str(e)
        now = time.time()
        exp_min = round((cred.expires_at_ms / 1000 - now) / 60, 1) if cred else 0
        cd_left = max(0, round((self.cooldown_until - now) / 60, 1))
        return {
            "label": self.label,
            "auth_path": self.auth_path,
            "user_id": cred.email if cred else None,   # 面板兼容字段：显示邮箱
            "logged_in": cred is not None,
            "ok": cred is not None,
            "expires_at_min": exp_min,
            "cooldown_min_left": cd_left if cd_left > 0 else 0,
            "cooldown_note": self.cooldown_note,
            "error": err or self.cooldown_note or None,
        }


# ─── 通道注册表（config.json 驱动，mtime 缓存） ─────────────────────────────

_channels: dict[str, list] = {}
_channel_rr: dict[str, int] = {}


def _parse_auth_paths(name: str, cfg: dict) -> list:
    """auth_path / auth_paths 归一化为 [{"path": ..., "label": ...}]（同 atomcode 规则）。"""
    raw = []
    arr = cfg.get("auth_paths")
    if isinstance(arr, list):
        for a in arr:
            if isinstance(a, str) and a.strip():
                raw.append((a.strip(), ""))
            elif isinstance(a, dict) and isinstance(a.get("path"), str) and a.get("path").strip():
                raw.append((a["path"].strip(), str(a.get("label") or "").strip()))
    single = cfg.get("auth_path")
    if isinstance(single, str) and single.strip():
        raw.append((single.strip(), ""))
    by_path = {}
    for path, label in raw:
        if path not in by_path:
            by_path[path] = [path, ""]
        if label:
            by_path[path][1] = label
    out = [{"path": p, "label": l} for p, l in by_path.values()]
    used = {o["label"] for o in out if o["label"]}
    n = 1
    for o in out:
        if not o["label"]:
            if len(out) == 1:
                o["label"] = name
            else:
                d = f"{name}·{n}"
                while d in used:
                    n += 1
                    d = f"{name}·{n}"
                o["label"] = d
                used.add(d)
                n += 1
    return out


def reload_channels_from_config(cfg: dict) -> None:
    """按 config providers 段重建 Cline 通道池（providers._load_providers 调用）。"""
    provs = (cfg.get("providers") or {})
    new = {}
    for name, p in provs.items():
        if isinstance(p, dict) and p.get("type") == "cline":
            entries = _parse_auth_paths(name, p)
            chs = [ClineChannel(name, {**p, "auth_path": e["path"]},
                                label=e["label"]) for e in entries]
            if not chs:
                chs = [ClineChannel(name, p)]  # 占位通道（status 报未登录）
            new[name] = chs
    _channels.clear()
    _channels.update(new)
    _restore_cooldowns(new)


def _restore_cooldowns(new: dict) -> None:
    """R-A：通道实例重建后按稳定键（auth 路径）回灌持久化冷却（晚于
    实例构造、早于首个请求）。label 只用于展示/排除集，不参与键——
    账号顺序变化或自动拼装 label 变化不再导致冷却失配。无 auth 路径
    的占位通道回退 `prov|label` 键；恢复失败仅 warning 不阻断重载。"""
    for name, chs in new.items():
        for ch in chs:
            try:
                until = cooldown_store.get_channel(name, ch.label,
                                                   auth_path=ch.auth_path)
            except Exception as e:
                log.warning("[cline] 冷却恢复失败 %s|%s: %s", name, ch.label, e)
                continue
            if until:
                ch.cooldown_until = until


def channels_for(name: str) -> list:
    return _channels.get(name) or []


def pick_channel(name: str, exclude: set = None) -> ClineChannel | None:
    """选一个可用通道：冷却中的跳过；最少进行中请求优先，同负载轮转。

    exclude：重试时排除已失败通道的 label 集合。无可用通道返回 None。
    """
    from .router import registry
    chs = [c for c in channels_for(name) if c.available]
    if exclude:
        chs = [c for c in chs if c.label not in exclude]
    if not chs:
        return None
    if len(chs) == 1:
        return chs[0]
    loads = {c.label: registry.load_of(f"cl:{name}|{c.label}") for c in chs}
    start = _channel_rr.get(name, 0) % len(chs)
    best = None
    for i in range(len(chs)):
        c = chs[(start + i) % len(chs)]
        if best is None or loads[c.label] < loads[best.label]:
            best = c
    _channel_rr[name] = (start + 1) % len(chs)
    return best


# ─── 免费通道串行门（0924 审计 L-3 改造） ────────────────────────────────────
# 语义对齐 cline2api 的 enqueue：下一个请求必须等上一个请求"拿到响应头"后
# 再隔 MIN_REQUEST_GAP_S 才能发起。时间戳打在响应头到达之后（而非发起前），
# 慢上游下防止"在途重叠"触发并发>1 空响应风控。付费模型不过门。

_gate_cv = threading.Condition()
_gate_busy = False          # 有请求"已发起、响应头未到"
_gate_last_done_ts = 0.0    # 上一次响应头到达的时刻
_gate_waiters = 0           # 正在门内排队等待的请求数（/api/gate 面板用）


def wait_for_slot(model: str) -> None:
    """免费通道请求发起前等待轮到本请求；付费模型直接放行。

    检查与占位（_gate_busy=True）在条件变量锁内完成，无竞态；
    等待期间锁自动释放，不阻塞 mark_slot_done。

    排队语义契约（方案 v2 §3.2，0924 审计 L-3 语义固化）：
    等待不计账号失败、不触发任何冷却——请求尚在本地门内
    排队时还没有向上游发起任何请求，上游侧的一切判负/
    冷却都与排队中的请求无关。
    """
    global _gate_busy, _gate_waiters
    if not is_free_model(model):
        return
    with _gate_cv:
        waiting = False      # 本请求是否已进入排队计数
        try:
            while True:
                if _gate_busy:
                    if not waiting:
                        waiting = True
                        _gate_waiters += 1     # 等待中 +1（锁内）
                    _gate_cv.wait(timeout=5.0)      # 等在途请求拿到响应头
                    continue
                wait = _gate_last_done_ts + MIN_REQUEST_GAP_S - time.time()
                if wait > 0:
                    if not waiting:
                        waiting = True
                        _gate_waiters += 1     # 间隔排队同样计入等待
                    _gate_cv.wait(wait)            # 等最小间隔
                    continue
                _gate_busy = True
                return                      # 拿到位：finally 内 -1
        finally:
            # 拿到位（return）/ 超时退出（未来扩展）：
            # 仍在排队计数中则 -1（锁内）
            if waiting:
                _gate_waiters -= 1


def mark_slot_done(model: str) -> None:
    """响应头已到达（或请求异常终结）：释放串行门并重置间隔起点。

    ⚠️ 必须在 send 返回/异常的 finally 中调用，否则门会永久卡死。
    """
    global _gate_busy, _gate_last_done_ts
    if not is_free_model(model):
        return
    with _gate_cv:
        _gate_busy = False
        _gate_last_done_ts = time.time()
        _gate_cv.notify_all()


# ─── 协议工具（上游体构造 / 剥壳 / 冷却解析 / SSE 处理） ────────────────────

def build_upstream_body(data: dict, model: str):
    """客户端 OpenAI 请求 → Cline 上游请求体。

    返回 (body, force_stream)：
    - 绝不带 max_tokens：免费模型带该字段上游一律 500 "empty response content"
    - 免费模型非流式被上游限流 → force_stream=True（上游走流式，代理聚合）
    - session_id / reasoning_effort 是 Cline 客户端的固定载荷
    """
    body = {
        "model": model,
        "session_id": f"sess_{int(time.time() * 1000)}",
        "reasoning_effort": data.get("reasoning_effort")
        or data.get("reasoningEffort") or "high",
        "messages": data.get("messages") or [],
    }
    for k in ("temperature", "top_p", "tools", "tool_choice", "stop",
              "presence_penalty", "frequency_penalty", "response_format",
              "user", "n", "seed"):
        if data.get(k) is not None:
            body[k] = data[k]
    force_stream = not bool(data.get("stream")) and is_free_model(model)
    if data.get("stream") or force_stream:
        body["stream"] = True
    return body, force_stream


def unwrap_data(obj):
    """剥掉上游 {data:{...}} 包装（上游有时包一层；多数时候没有）。"""
    if isinstance(obj, dict):
        d = obj.get("data")
        if isinstance(d, dict) and (d.get("choices") or d.get("id") or "usage" in d):
            return d
    return obj


def parse_cooldown_seconds(text: str, status: int = 429) -> int:
    """解析上游限流提示："Try again in 2h 51m" → 秒；无提示按状态给默认值。"""
    m = re.search(r"try again in\s+(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?\s*(?:(\d+)\s*s)?",
                  text or "", re.I)
    if m:
        total = (int(m.group(1) or 0) * 3600 + int(m.group(2) or 0) * 60
                 + int(m.group(3) or 0))
        if total > 0:
            return min(total, MAX_DAILY_COOLDOWN_S)
    return DAILY_LIMIT_DEFAULT_S if status == 429 else ERROR_COOLDOWN_S


def _merge_tool_call_delta(tool_calls: dict, delta_tools) -> None:
    """把 OpenAI 流式 tool_calls 增量合并成完整调用列表。

    上游按 index 分片返回：首个分片通常带 id/name，后续分片只带
    function.arguments 片段。免费通道非流式请求会被强制转成上游流式，
    若这里不合并，客户端的工具调用能力会在聚合时整体丢失。
    """
    if not isinstance(delta_tools, list):
        return
    for item in delta_tools:
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item.get("index", len(tool_calls)))
        except (TypeError, ValueError):
            idx = len(tool_calls)
        slot = tool_calls.setdefault(idx, {
            "id": "",
            "type": "function",
            "function": {"name": "", "arguments": ""},
        })
        if item.get("id"):
            slot["id"] = str(item["id"])
        if item.get("type"):
            slot["type"] = str(item["type"])
        fn = item.get("function") or {}
        if not isinstance(fn, dict):
            continue
        if fn.get("name"):
            slot["function"]["name"] = str(fn["name"])
        if fn.get("arguments"):
            slot["function"]["arguments"] += str(fn["arguments"])


def aggregate_stream(resp, display_model: str) -> dict:
    """把上游 SSE 流聚合成 OpenAI 非流式响应（免费通道强制流式的配套）。

    免费通道会概率性返回「HTTP 200 但 content 全程为空（只有 reasoning）」
    的流：此时把 reasoning 兜底拼进 content 并置 reasoning_used_as_content=True，
    调用方（routes/proxy.py）据此判定坏响应换号重试。
    """
    content, reasoning = [], []
    tool_calls = {}
    finish, cid, usage = None, None, None
    for line in resp.iter_lines():
        s = (line or "").strip()
        if not s.startswith("data:"):
            continue
        p = s[5:].strip()
        if not p or p == "[DONE]":
            continue
        try:
            o = unwrap_data(json.loads(p))
            ch = (o.get("choices") or [{}])[0]
            d = ch.get("delta") or {}
            if d.get("content"):
                content.append(d["content"])
            if d.get("reasoning"):
                reasoning.append(d["reasoning"])
            if d.get("reasoning_content"):
                reasoning.append(d["reasoning_content"])
            _merge_tool_call_delta(tool_calls, d.get("tool_calls"))
            if ch.get("finish_reason"):
                finish = ch["finish_reason"]
            if o.get("id"):
                cid = o["id"]
            if o.get("usage"):
                usage = o["usage"]
        except Exception:
            continue
    text, rtext = "".join(content), "".join(reasoning)
    merged_tools = [tool_calls[i] for i in sorted(tool_calls)]
    for i, tc in enumerate(merged_tools):
        if not tc.get("id"):
            tc["id"] = f"call_{i + 1}"
    msg = {"role": "assistant", "content": text}
    if merged_tools:
        msg["tool_calls"] = merged_tools
    if rtext:
        msg["reasoning"] = rtext
        msg["reasoning_content"] = rtext
    used_fallback = False
    if not text and rtext and not merged_tools:
        msg["content"] = rtext
        used_fallback = True
    return {
        "id": cid or f"gen_{int(time.time() * 1000)}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": display_model,
        "choices": [{"index": 0, "message": msg,
                     "finish_reason": finish or ("tool_calls" if merged_tools else "stop"),
                     "logprobs": None}],
        "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0,
                           "total_tokens": 0},
        "reasoning_used_as_content": used_fallback,
    }


class ClineStreamWrap:
    """上游 SSE 字节流的剥壳+改名包装（喂给 routes/proxy._stream_body）。

    - 剥 {data:{...}} 包装（防御性：上游偶尔包一层）
    - model 字段改回客户端请求名：上游会把 cline-free/deepseek-v4.1-flash
      解析成 deepseek/deepseek-v4.1-flash 返回，不改名会被注水检测误报
    - 按完整行缓冲（字节级切分，杜绝多字节字符被 chunk 撕裂）
    """

    def __init__(self, resp, display_model: str):
        self._resp = resp
        self._model = display_model

    def iter_bytes(self):
        buf = b""
        for chunk in self._resp.iter_bytes():
            buf += chunk
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                yield self._transform_line(raw.decode("utf-8", errors="ignore"))
        if buf:
            yield self._transform_line(buf.decode("utf-8", errors="ignore"))

    def _transform_line(self, line: str) -> bytes:
        if not line:
            return b"\n"
        s = line.strip()
        if not s.startswith("data:"):
            return (line + "\n").encode("utf-8")
        payload = s[5:].strip()
        if not payload or payload == "[DONE]":
            return (line + "\n").encode("utf-8")
        try:
            norm = unwrap_data(json.loads(payload))
            if isinstance(norm, dict):
                norm["model"] = self._model
                return ("data: " + json.dumps(norm, ensure_ascii=False)
                        + "\n").encode("utf-8")
        except Exception:
            pass
        return (line + "\n").encode("utf-8")

    def close(self):
        try:
            self._resp.close()
        except Exception:
            pass
