# -*- coding: utf-8 -*-
"""MiniCode（mcode / MiniMax Code CLI）通道：官方客户端凭证 + OAuth 刷新 + Anthropic 协议转换。

原理（2026-10-01 minicode 开源仓库源码分析 + 实测验证）：
- MiniMax 官方 CLI（mcode login）把 OAuth 凭证写到
  %USERPROFILE%\\.minimax\\auth\\prod\\cn\\mcode-public\\auth.json
  （records 下单条记录：accessToken=mmoat_* / refreshToken=mmort_*，Bearer）。
  文件结构：{"schemaVersion":1, "records": {"com.minimax.mcode.oauth.prod.cn\\0<hash>": {...}}}。
- 模型网关走 **Anthropic Messages 协议**：
  POST https://agent.minimax.cn/mavis/api/v1/llm/v1/messages
  头：Authorization: Bearer <accessToken>（网关只认 Bearer；x-api-key 发占位 sk-xxx）、
      anthropic-version: 2023-06-01、User-Agent: MiniMaxAgent、X-Mavis-* 会话头。
  2026-10-01 实测：非流式/流式/工具调用/图像输入/thinking 开关/effort 档位全部 200。
- token 刷新（OAuth2 标准表单）：
  POST https://account.minimax.cn/oauth2/token
  grant_type=refresh_token & refresh_token=... & client_id=mcode-public
  & scope=agent.default & audience=agent-backend
  响应 {access_token, refresh_token?, expires_in}；refresh_token 可能轮换，需立即落盘。
- **与官方客户端共存铁律**（oauth-core/auth-core.ts satisfiesMinValidity 语义）：
  官方客户端判断 freshness 只看 auth.json 里 credential.expiresAtMs，判断登录有效
  只看 auth-state.json 的 generation === credential.generation。因此本模块刷新后：
  1) 只更新 auth.json 的 accessToken/refreshToken/expiresAtMs 三字段；
  2) **generation 与 loginEpoch 等其余字段原样保留**，auth-state.json **完全不动**——
     官方客户端（若在运行）读到的仍是 generation 匹配的新鲜凭证，双方无缝共用。
  3) 刷新前必先重读文件（mtime 感知），官方客户端可能刚刷过，避免拿旧 refresh_token
     撞轮换；刷新单飞（_refresh_lock）防本代理内部双刷。
- 模型目录（packages/config/src/minimax-model-catalog.ts）。
  **计费口径（2026-10-01 官网核实+实测修正）**：走 MiniMax Code token plan 体系
  （非现金预付），但**并非无限免费**——5 小时滚动窗口/活动临时额度均有上限，
  耗尽时上游返回 402 "insufficient balance (1008)"；恢复途径 = 等窗口滚动 /
  MiniMax Code 官网活动领取（额度重置卡、签到 Token）。402 处理见
  routes/proxy._forward_minicode._classify：冷却 600s 透传，**禁止静默换引擎**。
  MiniMax-M3（512K/128K，文本+图像+视频输入，thinking 可关）
  MiniMax-M3.1-Flash-Preview（512K/128K，thinking 强制开，effort 档位）
  MiniMax-M2.7-highspeed / MiniMax-M2.7（200K/128K，纯文本）
- 额度查询端点（/v1/api/openplatform/coding_plan/remains）要求开放平台 audience 的
  token，agent-backend token 实测 401，故本通道 status 只展示凭证有效期。

config.json 示例（默认账号 = 官方客户端登录态；多账号 = auths/ 下拷贝的凭证文件）：
  "providers": {
    "minicode": {
      "type": "minicode",
      "auth_paths": [
        {"path": "C:/Users/?/.minimax/auth/prod/cn/mcode-public/auth.json", "label": "主号"}
      ],
      "models": ["mcode-m3", "mcode-m3.1-flash", "mcode-m2.7-highspeed", "mcode-m2.7"],
      "model_aliases": {
        "mcode-m3": "MiniMax-M3",
        "mcode-m3.1-flash": "MiniMax-M3.1-Flash-Preview",
        "mcode-m2.7-highspeed": "MiniMax-M2.7-highspeed",
        "mcode-m2.7": "MiniMax-M2.7"
      }
    }
  }
简写 "auth_path": "..." 字符串也有效（= 单账号，label 缺省用通道名）。
"""

import json
import logging
import os
import re
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from .settings import BASE_DIR
from . import cooldown_store

log = logging.getLogger("qidi-relay")

# 端点常量（CN prod；官方客户端另有 en 区 agent.minimax.io，本通道按登录 region 可配）
API_BASE_CN = "https://agent.minimax.cn/mavis/api/v1/llm/v1"
API_BASE_EN = "https://agent.minimax.io/mavis/api/v1/llm/v1"
TOKEN_URL_CN = "https://account.minimax.cn/oauth2/token"
TOKEN_URL_EN = "https://account.minimax.io/oauth2/token"
CLIENT_ID = "mcode-public"
SCOPES = "agent.default"
AUDIENCE = "agent-backend"
ANTHROPIC_VERSION = "2023-06-01"
USER_AGENT = "MiniMaxAgent"

# access token 提前 2 分钟视为过期（官方客户端 30s 边际，这里放宽避免临界竞争）
TOKEN_EXPIRY_MARGIN_MS = 120_000
# 凭证异常（刷新失败等）后的通道冷却
ERROR_COOLDOWN_S = 60
# 429 冷却默认值（无 "Try again in" 提示时）
DAILY_LIMIT_DEFAULT_S = 300
MAX_DAILY_COOLDOWN_S = 6 * 3600
# 客户端未带 max_tokens 时的默认输出上限（Anthropic 协议必填）
# 1001 足预算实测：high 档难题思考 4万~8万字，收敛需 14K~22K token（危大清单
# 14064 / 骑士谜题+证明 21556）——默认 16384 会把这类请求送截断，提到 32768 兜底。
# 注意：输出上限是 128K（MAX_TOKENS_CAP）；1M 是模型上下文窗口（输入），不是输出。
MAX_TOKENS_DEFAULT = 32768
MAX_TOKENS_CAP = 128000
# 支持 output_config.effort 档位的上游模型（M3.1-Flash 实测支持；M3 目录未登记则不发）
EFFORT_MODELS = {"MiniMax-M3.1-Flash-Preview"}
# 允许透传的 effort 档位（与官方 effortOptions 对齐；default/off 不发 output_config）
EFFORT_LEVELS = {"low", "medium", "high", "xhigh", "max"}


def _default_auth_path() -> str | None:
    p = Path.home() / ".minimax" / "auth" / "prod" / "cn" / "mcode-public" / "auth.json"
    return str(p) if p.exists() else None


def _resolve_path(p: str) -> str:
    """相对路径优先从代理根目录解析（auths/ 下的上传凭证等）。"""
    q = Path(p)
    if not q.is_absolute():
        return str((BASE_DIR / q).resolve())
    return p


class MiniCodeCredential:
    """auth.json 单条记录的解析结果（其余字段写回时原样保留，这里不持有）。"""

    def __init__(self, access_token: str, refresh_token: str,
                 expires_at_ms: int, record_key: str = ""):
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.expires_at_ms = expires_at_ms  # unix 毫秒；0 = 未知
        self.record_key = record_key

    @property
    def expired(self) -> bool:
        if not self.expires_at_ms:
            return False
        return time.time() * 1000 >= self.expires_at_ms - TOKEN_EXPIRY_MARGIN_MS


def parse_auth_json(path: str) -> MiniCodeCredential:
    """解析 mcode auth.json：records 取第一条记录（官方客户端单账号单记录）。"""
    doc = json.load(open(path, encoding="utf-8"))
    records = doc.get("records")
    if not isinstance(records, dict) or not records:
        raise ValueError(f"auth.json 缺 records 记录（{path}），请先 mcode login")
    key = next(iter(records))
    rec = records[key]
    if not isinstance(rec, dict):
        raise ValueError(f"auth.json 记录结构异常（{path}）")
    at = str(rec.get("accessToken") or "")
    rt = str(rec.get("refreshToken") or "")
    if not at or not rt:
        raise ValueError("auth.json 缺 accessToken/refreshToken（未登录或结构变更）")
    try:
        exp = int(rec.get("expiresAtMs") or 0)
    except (TypeError, ValueError):
        exp = 0
    return MiniCodeCredential(at, rt, exp, str(key))


class MiniCodeChannel:
    """一个 mcode 账号通道 = 一个 auth.json + 刷新/写回/冷却。

    多账号 = 一个 provider 下多个 MiniCodeChannel（每个绑定一份凭证文件）。
    label 用于统计/换号集合，务必全局唯一。
    """

    def __init__(self, name: str, cfg: dict, label: str = ""):
        self.name = name
        self.label = label or name
        self._lock = threading.Lock()
        # 刷新单飞锁：同一通道并发线程同时遇过期时只放一个去刷新，其余等结果
        self._refresh_lock = threading.Lock()
        self._auth_mtime = None
        self._cred: MiniCodeCredential | None = None
        ap = cfg.get("auth_path") or _default_auth_path()
        self.auth_path = _resolve_path(ap) if ap else None
        region = str(cfg.get("region") or "cn").lower()
        self.api_base = (API_BASE_EN if region == "en" else API_BASE_CN).rstrip("/")
        self.token_url = TOKEN_URL_EN if region == "en" else TOKEN_URL_CN
        try:
            self.max_tokens_default = int(cfg.get("max_tokens_default") or MAX_TOKENS_DEFAULT)
        except (TypeError, ValueError):
            self.max_tokens_default = MAX_TOKENS_DEFAULT
        self.cooldown_until = 0.0
        self.cooldown_note = ""
        self.last_error: str | None = None

    # ─── 凭证（mtime 感知加载 / 刷新 / 写回） ────────────────────────────────

    def _load_cred(self) -> MiniCodeCredential:
        """mtime 感知重载：官方客户端刷新过则自动跟进（幂等）。"""
        if not self.auth_path or not Path(self.auth_path).exists():
            raise FileNotFoundError(
                f"mcode auth.json 不存在：{self.auth_path}（请先运行 mcode login）")
        st = os.stat(self.auth_path)
        if self._cred and self._auth_mtime == st.st_mtime:
            return self._cred
        cred = parse_auth_json(self.auth_path)
        with self._lock:
            self._auth_mtime = st.st_mtime
            self._cred = cred
        log.info("[minicode] 凭证已加载/更新: %s (mtime=%s, 有效期至 %s)", self.label,
                 time.strftime("%m-%d %H:%M:%S", time.localtime(st.st_mtime)),
                 datetime.fromtimestamp(cred.expires_at_ms / 1000).strftime("%m-%d %H:%M")
                 if cred.expires_at_ms else "未知")
        return cred

    def _write_back(self, cred: MiniCodeCredential):
        """刷新后的凭证写回 auth.json —— 只动三个字段，其余（generation/loginEpoch/
        subject 等）与 auth-state.json 一概不动，官方客户端读到的仍是匹配的新鲜凭证。

        写回前复核 mtime：若官方客户端在加载后已更新文件（自己也刷过），放弃写回，
        以磁盘文件为准强制重载 —— 双方谁后刷新谁的凭证都会被对方跟进，无需抢写。
        """
        if not self.auth_path:
            return
        tmp = self.auth_path + ".relay-tmp"
        try:
            st0 = os.stat(self.auth_path)
            doc = json.load(open(self.auth_path, encoding="utf-8"))
            records = doc.get("records")
            if not isinstance(records, dict) or not records:
                raise ValueError("写回失败：records 结构缺失")
            key = cred.record_key if cred.record_key in records else next(iter(records))
            rec = records[key]
            if not isinstance(rec, dict):
                raise ValueError("写回失败：记录不是对象")
            rec["accessToken"] = cred.access_token
            rec["refreshToken"] = cred.refresh_token
            rec["expiresAtMs"] = int(cred.expires_at_ms)
            with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                json.dump(doc, f, ensure_ascii=False, indent=2)
                f.write("\n")
            if os.stat(self.auth_path).st_mtime != st0.st_mtime:
                log.warning("[minicode] %s 写回期间文件被官方客户端更新，放弃写回（以文件为准）",
                            self.label)
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                with self._lock:
                    self._auth_mtime = None   # 强制下次重载
                    self._cred = None
                return
            os.replace(tmp, self.auth_path)
            with self._lock:
                self._auth_mtime = os.stat(self.auth_path).st_mtime
                self._cred = cred
            log.info("[minicode] %s 刷新后的凭证已写回 %s", self.label, self.auth_path)
        except Exception as e:
            log.error("[minicode] %s 写回凭证失败: %s", self.label, e)
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass

    def _refresh(self, old_rt: str) -> MiniCodeCredential:
        """用 refresh_token 换新 access_token（OAuth2 表单，官方 client_id/scope/audience）。

        单飞：同一通道并发刷新只放行一个；等待者醒来后先重读磁盘——若 refresh_token
        已被先到者轮换，直接复用新凭证，避免拿旧 refresh_token 二次请求撞 invalid_grant。
        """
        with self._refresh_lock:
            return self._refresh_locked(old_rt)

    def _refresh_locked(self, old_rt: str) -> MiniCodeCredential:
        # 先重读：等锁期间官方客户端 / 先到线程可能已刷新（含 refresh_token 轮换），
        # 磁盘凭证已新鲜就直接复用，绝不拿旧 refresh_token 二次请求。
        cred = self._load_cred()
        if not cred.expired:
            return cred
        import httpx
        r = httpx.post(self.token_url,
                       data={"grant_type": "refresh_token",
                             "refresh_token": old_rt,
                             "client_id": CLIENT_ID,
                             "scope": SCOPES,
                             "audience": AUDIENCE},
                       headers={"Accept": "application/json",
                                "Content-Type": "application/x-www-form-urlencoded",
                                "User-Agent": "mcode-relay"},
                       timeout=15)
        if r.status_code != 200:
            raise RuntimeError(f"token 刷新失败 HTTP {r.status_code}: {r.text[:200]}")
        data = r.json()
        new_at = str(data.get("access_token") or "")
        if not new_at:
            raise RuntimeError(f"token 刷新响应缺 access_token: {str(data)[:200]}")
        new_rt = str(data.get("refresh_token") or old_rt)  # 未轮换则沿用旧值
        exp_ms = 0
        try:
            if data.get("expires_in"):
                exp_ms = int(time.time() * 1000) + int(data["expires_in"]) * 1000
            elif data.get("expiresAtMs"):
                exp_ms = int(data["expiresAtMs"])
        except (TypeError, ValueError):
            exp_ms = 0
        new = MiniCodeCredential(new_at, new_rt, exp_ms or cred.expires_at_ms,
                                 cred.record_key)
        if new_rt != old_rt:
            log.info("[minicode] %s refresh_token 已轮换，立即落盘", self.label)
        self._write_back(new)
        return new

    def get_token(self) -> str:
        """取有效 accessToken（过期自动刷新；刷新失败抛异常由调用方换通道/冷却）。"""
        cred = self._load_cred()
        if cred.expired:
            log.info("[minicode] %s token 已过期（含 2 分钟边际），自动刷新...", self.label)
            cred = self._refresh(cred.refresh_token)
        return cred.access_token

    # ─── 冷却/失效（换号重试用） ─────────────────────────────────────────────

    def set_cooldown(self, seconds: float, note: str = ""):
        self.cooldown_until = time.time() + max(1, seconds)
        self.cooldown_note = str(note or "")[:120]
        # R-A：通道级冷却持久化（重载/重启后按 prov|label 回灌）
        cooldown_store.set_channel(self.name, self.label, self.cooldown_until)

    def invalidate(self):
        """401 等凭证失效：丢弃内存缓存强制重读（官方客户端/刷新后可再试）。"""
        with self._lock:
            self._auth_mtime = None
            self._cred = None

    @property
    def available(self) -> bool:
        return time.time() >= self.cooldown_until

    def request_headers(self, token: str, session_id: str | None = None) -> dict:
        """上游请求头（对齐官方客户端 MiniMaxAgent UA + X-Mavis 会话头 + Bearer）。"""
        try:
            tz_off = int(datetime.now().astimezone().utcoffset().total_seconds())
        except Exception:
            tz_off = 28800
        return {
            "Authorization": f"Bearer {token}",
            "x-api-key": "sk-xxx",  # 官方占位值，网关只认 Bearer
            "Content-Type": "application/json",
            "Accept": "application/json",
            "anthropic-version": ANTHROPIC_VERSION,
            "anthropic-dangerous-direct-browser-access": "true",
            "User-Agent": USER_AGENT,
            "X-Mavis-Session-Id": session_id or f"relay-{uuid.uuid4().hex[:16]}",
            "X-Mavis-Agent-Id": "main",
            "X-Mavis-Timezone-Offset": str(tz_off),
        }

    def status(self) -> dict:
        try:
            cred = self._load_cred()
            err = None
        except Exception as e:
            cred = None
            err = str(e)
        now = time.time()
        return {
            "name": self.name,
            "label": self.label,
            "auth_path": self.auth_path,
            "base_url": self.api_base,
            "logged_in": cred is not None,
            "ok": err is None and self.available,
            "expires_at_min": round((cred.expires_at_ms / 1000 - now) / 60, 1)
            if cred and cred.expires_at_ms else None,
            "cooldown_min_left": round((self.cooldown_until - now) / 60, 1)
            if not self.available else 0,
            "cooldown_note": self.cooldown_note if not self.available else "",
            "error": err,
        }


# ─── 通道注册（config.json 驱动，mtime 缓存） ────────────────────────────────────

_channels: dict[str, list] = {}
_channel_rr: dict[str, int] = {}


def _parse_auth_paths(name: str, cfg: dict) -> list:
    """auth_path / auth_paths 归一为 [{"path":..., "label":...}]（与 atomcode/cline 同一套约定）。"""
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
    """按 config providers 重建 MiniCode 通道池（providers._load_providers 调用）。"""
    provs = (cfg.get("providers") or {})
    new = {}
    for name, p in provs.items():
        if isinstance(p, dict) and p.get("type") == "minicode":
            entries = _parse_auth_paths(name, p)
            chs = [MiniCodeChannel(name, {**p, "auth_path": e["path"]},
                                   label=e["label"]) for e in entries]
            if not chs:
                chs = [MiniCodeChannel(name, p)]  # 占位通道（status 显示未登录）
            new[name] = chs
    _channels.clear()
    _channels.update(new)
    _restore_cooldowns(new)


def _restore_cooldowns(new: dict) -> None:
    """R-A：通道实例重建后按 prov|label 回灌持久化冷却（晚于实例
    构造、早于首个请求）。label 不稳定（重命名/删除）的键过期自然
    失效，不跨实例错挂；恢复失败仅 warning 不阻断重载。"""
    for name, chs in new.items():
        for ch in chs:
            try:
                until = cooldown_store.get_channel(name, ch.label)
            except Exception as e:
                log.warning("[minicode] 冷却恢复失败 %s|%s: %s", name, ch.label, e)
                continue
            if until:
                ch.cooldown_until = until


def channels_for(name: str) -> list:
    return _channels.get(name) or []


def all_channels() -> dict:
    return {k: list(v) for k, v in _channels.items()}


def pick_channel(name: str, exclude: set = None) -> MiniCodeChannel | None:
    """选一个可用通道（冷却中的跳过；并发负载感知 + 同量轮转，与其他通道族一致）。"""
    from .router import registry
    chs = [c for c in channels_for(name) if c.available]
    if exclude:
        chs = [c for c in chs if c.label not in exclude]
    if not chs:
        return None
    if len(chs) == 1:
        return chs[0]
    loads = {c.label: registry.load_of(f"mc:{name}|{c.label}") for c in chs}
    start = _channel_rr.get(name, 0) % len(chs)
    best = None
    for i in range(len(chs)):
        c = chs[(start + i) % len(chs)]
        if best is None or loads[c.label] < loads[best.label]:
            best = c
    _channel_rr[name] = (start + 1) % len(chs)
    return best


# ─── 协议转换：OpenAI chat/completions → Anthropic Messages ─────────────────────

def _image_block_from_url_part(part: dict) -> dict | None:
    """OpenAI image_url part → Anthropic image block（data: 转 base64，http(s) 透传 url）。"""
    url = str((part.get("image_url") or {}).get("url") or "")
    if not url:
        return None
    m = re.match(r"data:([^;]+);base64,(.+)", url, re.S)
    if m:
        return {"type": "image",
                "source": {"type": "base64", "media_type": m.group(1), "data": m.group(2)}}
    if url.startswith("http://") or url.startswith("https://"):
        return {"type": "image", "source": {"type": "url", "url": url}}
    return None


def _content_text(content) -> str:
    """OpenAI content（str / parts 列表）取纯文本（tool 结果等兜底用）。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict) and p.get("type") == "text":
                parts.append(str(p.get("text") or ""))
            elif isinstance(p, str):
                parts.append(p)
        return "\n".join(x for x in parts if x)
    if content is None:
        return ""
    return json.dumps(content, ensure_ascii=False)


def _openai_tool_calls_to_blocks(msg: dict) -> list:
    """assistant.tool_calls → Anthropic tool_use blocks。

    容错（1001 实测踩坑）：个别客户端/脚本把单个 tool_call 对象直传为 dict
    而非标准列表——原样迭代 dict 会拿到字符串键、tool_use 块整个丢失，
    上游回传 tool_result 时报 "tool id not found (2013)"。这里统一包装成列表。
    """
    blocks = []
    tcs = msg.get("tool_calls")
    if isinstance(tcs, dict):
        tcs = [tcs]
    for tc in tcs or []:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or {}
        raw = fn.get("arguments")
        try:
            args = json.loads(raw) if isinstance(raw, str) else (raw if raw is not None else {})
        except Exception:
            args = {}
        if not isinstance(args, dict):
            args = {"value": args}
        blocks.append({
            "type": "tool_use",
            "id": str(tc.get("id") or f"call_{uuid.uuid4().hex[:16]}"),
            "name": str(fn.get("name") or ""),
            "input": args,
        })
    return blocks


def build_upstream_body(data: dict, upstream_model: str,
                        max_tokens_default: int = MAX_TOKENS_DEFAULT) -> dict:
    """OpenAI chat 请求体 → Anthropic Messages 请求体。

    - system/developer 角色消息合并为顶层 system（字符串）；
    - 连续 tool 角色消息合并为单条 user 的多个 tool_result block（Anthropic 语义）；
    - 转换后做相邻同角色合并，保证 user/assistant 严格交替、首条为 user；
    - reasoning_effort: off/none → thinking disabled（M3 实测可关）；
      M3.1-Flash 等档位模型发 output_config.effort（low 实测 200）；
      其余情况不带 thinking 字段走服务端默认（自适应思考）。
    历史 assistant 消息里回传的 reasoning/reasoning_content 一律丢弃（无签名，
      Anthropic 协议不接受裸 thinking 块回传）。
    """
    system_parts = []
    converted: list[dict] = []       # [{"role": "user"|"assistant", "blocks": [...]}]
    for m in data.get("messages") or []:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if role in ("system", "developer"):
            t = _content_text(m.get("content"))
            if t:
                system_parts.append(t)
            continue
        if role == "tool":
            blocks = [{
                "type": "tool_result",
                "tool_use_id": str(m.get("tool_call_id") or ""),
                "content": _content_text(m.get("content")),
            }]
            if converted and converted[-1]["role"] == "user":
                converted[-1]["blocks"].extend(blocks)
            else:
                converted.append({"role": "user", "blocks": blocks})
            continue
        if role == "assistant":
            blocks = []
            t = m.get("content")
            text = _content_text(t)
            if isinstance(t, str) and t:
                blocks.append({"type": "text", "text": t})
            elif isinstance(t, list):
                for p in t:
                    if isinstance(p, dict) and p.get("type") == "text":
                        blocks.append({"type": "text", "text": str(p.get("text") or "")})
            blocks.extend(_openai_tool_calls_to_blocks(m))
            if not blocks:
                blocks.append({"type": "text", "text": " "})
            converted.append({"role": "assistant", "blocks": blocks})
            continue
        # user
        blocks = []
        c = m.get("content")
        if isinstance(c, str):
            blocks.append({"type": "text", "text": c})
        elif isinstance(c, list):
            for p in c:
                if not isinstance(p, dict):
                    continue
                ptype = p.get("type")
                if ptype == "text" or ptype is None:
                    blocks.append({"type": "text", "text": str(p.get("text") or "")})
                elif ptype == "image_url":
                    img = _image_block_from_url_part(p)
                    if img:
                        blocks.append(img)
                else:
                    blocks.append({"type": "text", "text": json.dumps(p, ensure_ascii=False)})
        else:
            blocks.append({"type": "text", "text": "" if c is None else str(c)})
        converted.append({"role": "user", "blocks": blocks})

    # 相邻同角色合并
    merged: list[dict] = []
    for item in converted:
        if merged and merged[-1]["role"] == item["role"]:
            merged[-1]["blocks"].extend(item["blocks"])
        else:
            merged.append(dict(item))
    # 首条必须 user；不以 user 开头的会话补一条占位（如续聊历史以 assistant 开头）
    if merged and merged[0]["role"] != "user":
        merged.insert(0, {"role": "user", "blocks": [{"type": "text", "text": " "}]})

    body: dict = {
        "model": upstream_model,
        "messages": [{"role": it["role"], "content": it["blocks"]} for it in merged],
        "max_tokens": data.get("max_tokens") or max_tokens_default,
        "stream": bool(data.get("stream")),
    }
    if system_parts:
        body["system"] = "\n\n".join(system_parts)
    try:
        body["max_tokens"] = min(int(body["max_tokens"]) or max_tokens_default, MAX_TOKENS_CAP)
    except (TypeError, ValueError):
        body["max_tokens"] = max_tokens_default
    if data.get("temperature") is not None:
        body["temperature"] = data["temperature"]
    if data.get("top_p") is not None:
        body["top_p"] = data["top_p"]
    if isinstance(data.get("stop"), list) and data["stop"]:
        body["stop_sequences"] = [str(s) for s in data["stop"]][:4]
    elif isinstance(data.get("stop"), str) and data["stop"]:
        body["stop_sequences"] = [data["stop"]]

    tools = []
    for t in data.get("tools") or []:
        if not isinstance(t, dict):
            continue
        if t.get("type") == "function" and isinstance(t.get("function"), dict):
            fn = t["function"]
        else:
            fn = t  # 少数客户端直接给 {name, parameters}
        if not fn.get("name"):
            continue
        tools.append({
            "name": str(fn["name"]),
            "description": str(fn.get("description") or ""),
            "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
        })
    if tools:
        body["tools"] = tools

    tc = data.get("tool_choice")
    if isinstance(tc, str):
        if tc == "none":
            body["tool_choice"] = {"type": "none"}
        elif tc == "required":
            body["tool_choice"] = {"type": "any"}
        else:  # auto
            body["tool_choice"] = {"type": "auto"}
    elif isinstance(tc, dict) and isinstance(tc.get("function"), dict) and tc["function"].get("name"):
        body["tool_choice"] = {"type": "tool", "name": str(tc["function"]["name"])}

    effort = str(data.get("reasoning_effort") or data.get("reasoningEffort") or "").lower()
    if effort in ("off", "none", "disabled"):
        body["thinking"] = {"type": "disabled"}
    elif effort in EFFORT_LEVELS and upstream_model in EFFORT_MODELS:
        body["output_config"] = {"effort": effort}
    return body


# ─── 协议转换：Anthropic 响应 → OpenAI chat/completions ──────────────────────────

_STOP_MAP = {"end_turn": "stop", "stop_sequence": "stop", "tool_use": "tool_calls",
             "max_tokens": "length", "refusal": "content_filter"}


def message_to_chat(msg: dict, display_model: str) -> dict:
    """Anthropic message → OpenAI chat completion（thinking→reasoning_content，tool_use→tool_calls）。"""
    content_parts, reasoning_parts, tool_calls = [], [], []
    for b in (msg.get("content") or []):
        if not isinstance(b, dict):
            continue
        btype = b.get("type")
        if btype == "text":
            content_parts.append(str(b.get("text") or ""))
        elif btype == "thinking":
            reasoning_parts.append(str(b.get("thinking") or ""))
        elif btype == "tool_use":
            tool_calls.append({
                "id": str(b.get("id") or f"call_{uuid.uuid4().hex[:12]}"),
                "type": "function",
                "function": {
                    "name": str(b.get("name") or ""),
                    "arguments": json.dumps(b.get("input") if b.get("input") is not None
                                            else {}, ensure_ascii=False),
                },
            })
    text = "".join(content_parts)
    rtext = "".join(reasoning_parts)
    m = {"role": "assistant", "content": text or None}
    if tool_calls:
        m["tool_calls"] = tool_calls
    elif m["content"] is None:
        m["content"] = rtext or ""   # 只有 thinking 的兜底
    if rtext:
        m["reasoning_content"] = rtext
        m["reasoning"] = rtext
    usage_in = msg.get("usage") or {}
    prompt_t = int(usage_in.get("input_tokens") or 0)
    completion_t = int(usage_in.get("output_tokens") or 0)
    return {
        "id": f"chatm-{msg.get('id') or uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": display_model,
        "choices": [{
            "index": 0,
            "message": m,
            "finish_reason": "tool_calls" if tool_calls
            else _STOP_MAP.get(str(msg.get("stop_reason") or ""), "stop"),
            "logprobs": None,
        }],
        "usage": {
            "prompt_tokens": prompt_t,
            "completion_tokens": completion_t,
            "total_tokens": prompt_t + completion_t,
        },
        "reasoning_used_as_content": bool(not text and not tool_calls and rtext),
    }


def parse_cooldown_seconds(text: str, status: int = 429) -> int:
    """解析 429 提示的 "Try again in 2h 51m" 类等待秒数；无提示按状态给默认值。"""
    m = re.search(r"try again in\s+(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?\s*(?:(\d+)\s*s)?",
                  text or "", re.I)
    if m:
        total = (int(m.group(1) or 0) * 3600 + int(m.group(2) or 0) * 60
                 + int(m.group(3) or 0))
        if total > 0:
            return min(total, MAX_DAILY_COOLDOWN_S)
    m2 = re.search(r"(\d+)\s*(?:小时|个小时)\s*(\d+)?\s*分?", text or "")
    if m2:
        total = int(m2.group(1)) * 3600 + int(m2.group(2) or 0) * 60
        if total > 0:
            return min(total, MAX_DAILY_COOLDOWN_S)
    return DAILY_LIMIT_DEFAULT_S if status == 429 else ERROR_COOLDOWN_S


class MiniCodeStreamWrap:
    """Anthropic SSE → OpenAI SSE 字节流包装（喂 routes/proxy._stream_body）。

    - message_start            → 首块（role=assistant，含 id/model）
    - thinking_delta           → delta.reasoning_content
    - text_delta               → delta.content
    - content_block_start(tool_use) → delta.tool_calls[i]（id/name 就位）
    - input_json_delta         → delta.tool_calls[i].function.arguments 增量
    - message_delta            → 末块（finish_reason + OpenAI usage）
    - message_stop             → data: [DONE]；error 事件 → OpenAI 错误块
    模型名一律替换为客户端请求名（mcode-* 别名），防注水检测误报。
    """

    def __init__(self, resp, display_model: str):
        self._resp = resp
        self._model = display_model
        self._id = f"chatm-{uuid.uuid4().hex[:24]}"
        self._created = int(time.time())
        self._input_tokens = 0
        self._output_tokens = 0
        self._finish = None
        self._tool_idx = {}          # Anthropic block index -> OpenAI tool_calls index
        self._done_sent = False

    @staticmethod
    def _chunk(model: str, cid: str, created: int, delta: dict,
               finish=None, usage=None) -> bytes:
        obj = {"id": cid, "object": "chat.completion.chunk", "created": created,
               "model": model, "choices": [{"index": 0, "delta": delta,
                                           "finish_reason": finish}]}
        if usage:
            obj["usage"] = usage
        return ("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode("utf-8")

    def _handle_event(self, event: str, payload: str) -> list:
        out = []
        try:
            o = json.loads(payload)
        except Exception:
            return out
        etype = o.get("type") or event
        if etype == "message_start":
            msg = o.get("message") or {}
            self._id = f"chatm-{msg.get('id') or uuid.uuid4().hex[:24]}"
            u = msg.get("usage") or {}
            self._input_tokens = int(u.get("input_tokens") or 0)
            out.append(self._chunk(self._model, self._id, self._created,
                                   {"role": "assistant"}))
        elif etype == "content_block_start":
            b = o.get("content_block") or {}
            if b.get("type") == "tool_use":
                idx = int(o.get("index") or 0)
                tci = len(self._tool_idx)
                self._tool_idx[idx] = tci
                out.append(self._chunk(self._model, self._id, self._created, {"tool_calls": [{
                    "index": tci,
                    "id": str(b.get("id") or f"call_{uuid.uuid4().hex[:12]}"),
                    "type": "function",
                    "function": {"name": str(b.get("name") or ""), "arguments": ""},
                }]}))
        elif etype == "content_block_delta":
            d = o.get("delta") or {}
            dtype = d.get("type")
            if dtype == "text_delta":
                out.append(self._chunk(self._model, self._id, self._created,
                                       {"content": str(d.get("text") or "")}))
            elif dtype in ("thinking_delta", "reasoning_delta"):
                out.append(self._chunk(self._model, self._id, self._created,
                                       {"reasoning_content": str(d.get("thinking")
                                                                 or d.get("reasoning") or "")}))
            elif dtype == "input_json_delta":
                idx = int(o.get("index") or 0)
                tci = self._tool_idx.get(idx, 0)
                out.append(self._chunk(self._model, self._id, self._created, {"tool_calls": [{
                    "index": tci,
                    "function": {"arguments": str(d.get("partial_json") or "")},
                }]}))
            # signature_delta / citation_delta 等无 OpenAI 对应，忽略
        elif etype == "message_delta":
            d = o.get("delta") or {}
            if d.get("stop_reason"):
                self._finish = _STOP_MAP.get(str(d["stop_reason"]), "stop")
            u = o.get("usage") or {}
            if u.get("output_tokens") is not None:
                self._output_tokens = int(u["output_tokens"])
            if u.get("input_tokens"):
                self._input_tokens = int(u["input_tokens"])
        elif etype == "error":
            err = o.get("error") or {}
            out.append(("data: " + json.dumps({
                "error": {"message": str(err.get("message") or "minicode upstream error"),
                          "type": str(err.get("type") or "upstream_error")},
            }, ensure_ascii=False) + "\n\n").encode("utf-8"))
        return out

    def iter_bytes(self):
        """按行解析上游 SSE，转换后逐块吐 OpenAI SSE 字节（跨 chunk 行缓冲）。"""
        buf = b""
        event = ""
        for chunk in self._resp.iter_bytes():
            buf += chunk
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                line = raw.decode("utf-8", errors="ignore").strip()
                if not line:
                    continue
                if line.startswith("event:"):
                    event = line[6:].strip()
                    continue
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if not payload:
                    continue
                for piece in self._handle_event(event, payload):
                    yield piece
        if buf:
            line = buf.decode("utf-8", errors="ignore").strip()
            if line.startswith("data:"):
                for piece in self._handle_event(event, line[5:].strip()):
                    yield piece
        # 流结束：补末块（finish_reason + usage）+ [DONE]
        usage = {"prompt_tokens": self._input_tokens,
                 "completion_tokens": self._output_tokens,
                 "total_tokens": self._input_tokens + self._output_tokens}
        yield self._chunk(self._model, self._id, self._created, {},
                          finish=self._finish or "stop", usage=usage)
        self._done_sent = True
        yield b"data: [DONE]\n\n"

    def close(self):
        try:
            self._resp.close()
        except Exception:
            pass


def aggregate_stream(resp, display_model: str) -> dict:
    """聚合 Anthropic SSE 为单个 OpenAI 响应（非流式请求的兜底聚合用）。"""
    wrap = MiniCodeStreamWrap(resp, display_model)
    content, reasoning, tool_args = [], [], {}
    tool_meta = {}
    finish, cid = None, None
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    for piece in wrap.iter_bytes():
        for raw in piece.decode("utf-8", errors="ignore").split("\n"):
            s = raw.strip()
            if not s.startswith("data:"):
                continue
            p = s[5:].strip()
            if not p or p == "[DONE]":
                continue
            try:
                o = json.loads(p)
            except Exception:
                continue
            if isinstance(o.get("error"), dict):
                raise RuntimeError(str(o["error"].get("message") or "upstream error"))
            if o.get("id"):
                cid = o["id"]
            if o.get("usage"):
                usage = o["usage"]
            ch = (o.get("choices") or [{}])[0]
            d = ch.get("delta") or {}
            if d.get("content"):
                content.append(str(d["content"]))
            if d.get("reasoning_content"):
                reasoning.append(str(d["reasoning_content"]))
            for tc in d.get("tool_calls") or []:
                idx = int(tc.get("index", 0))
                slot = tool_meta.setdefault(idx, {"id": "", "name": ""})
                fn = tc.get("function") or {}
                if tc.get("id"):
                    slot["id"] = str(tc["id"])
                if fn.get("name"):
                    slot["name"] = str(fn["name"])
                tool_args[idx] = tool_args.get(idx, "") + str(fn.get("arguments") or "")
            if ch.get("finish_reason"):
                finish = ch["finish_reason"]
    text, rtext = "".join(content), "".join(reasoning)
    merged_tools = [{
        "id": tool_meta[i].get("id") or f"call_{i + 1}",
        "type": "function",
        "function": {"name": tool_meta[i].get("name") or "", "arguments": tool_args.get(i, "")},
    } for i in sorted(tool_args)]
    msg = {"role": "assistant", "content": text or None}
    if merged_tools:
        msg["tool_calls"] = merged_tools
    elif msg["content"] is None:
        msg["content"] = rtext or ""
    if rtext:
        msg["reasoning_content"] = rtext
        msg["reasoning"] = rtext
    return {
        "id": cid or f"chatm-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": display_model,
        "choices": [{"index": 0, "message": msg,
                     "finish_reason": finish or ("tool_calls" if merged_tools else "stop"),
                     "logprobs": None}],
        "usage": usage,
        "reasoning_used_as_content": bool(not text and not merged_tools and rtext),
    }
