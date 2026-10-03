# -*- coding: utf-8 -*-
"""AtomCode 通道：多账号凭证池 + sign_v1 请求签名 + token 刷新。

原理（2026-08-23 逆向并线上验证）：
- 官方客户端把登录凭证写在 ~/.atomcode/auth.toml（Windows:
  C:\\Users\\<user>\\.atomcode\\auth.toml），并定期自动续期。
- 本模块按 config.json 的 auth_paths 列表加载多份凭证文件（多账号池），
  每份 mtime 独立感知——文件更新后下一次请求自动用上新 token。
- 新账号可通过管理接口 POST /api/atomcode/upload 上传凭证文本
  （落盘 auths/atomcode-<label>.toml 并写入 config），无需手动拷文件。
- 请求需带 sign_v1 签名（服务端强制，缺签名返回 403 ATOMCODE_SIG_MISSING）：
    master_key = exe 内 64 字节 blob 前后半 XOR
    salt  = user_id || 0x01 || le64(ts//3600) || sha256(token) || sha256(ver)
    OKM   = HKDF-SHA256(master_key, salt, info="atomcode-signing-v1")
    sig   = HMAC-SHA256(OKM, "v1\\n"+METHOD+"\\n"+path+"\\n"+ts+"\\n"+nonce_hex+"\\n"+sha256(body)_hex)
- master_key 绑定客户端版本号；config.json 的 atomcode 段可指定 exe 路径与版本，
  未指定时按版本号猜常见安装路径。多账号共享同一 exe / master_key。

config.json 示例（多账号）：
  "providers": {
    "atomcode": {
      "type": "atomcode",
      "version": "5.0.8",
      "exe_path": "C:/Users/?/AppData/Local/AtomCode/atomcode.exe",
      "auth_paths": [
        {"path": "C:/Users/?/.atomcode/auth.toml", "label": "主号"},
        {"path": "auths/atomcode-小号.toml", "label": "小号"}
      ],
      "models": ["deepseek-v4-flash"]
    }
  }
向后兼容：单个 "auth_path": "..." 字符串仍有效（= 单账号，label 用通道名）。
"""

import hashlib
import hmac
import logging
import os
import struct
import threading
import time
from pathlib import Path

from .settings import BASE_DIR

log = logging.getLogger("qidi-relay")

PLATFORM_REFRESH_URL = "https://acs.atomgit.com/oauth/refresh"
DEFAULT_LLM_BASE = "https://llm-api.atomgit.com/v1"
# v5.0.9 实测（2026-08-31）：key blob 紧跟 "atomcode-signing-v1" 字符串之后 64 字节
# （.rdata 中同模块常量相邻布局）。优先自动定位（客户端升级免维护），
# 失败时回退硬编码偏移（当前对应 v5.0.9 = 0xB01523）。
KEY_ANCHOR = b"atomcode-signing-v1"
MASTER_KEY_OFFESET = 0xB01523  # v5.0.9 binary；自动定位失败时的回退值


def _locate_key_offset(exe_path: str) -> int | None:
    """在 exe 里搜锚点字符串定位 key blob（其后 64 字节）。

    返回 None 表示定位失败（调用方回退 MASTER_KEY_OFFESET 硬编码偏移）。
    """
    try:
        with open(exe_path, "rb") as f:
            data = f.read()
        i = data.find(KEY_ANCHOR)
        if i < 0:
            return None
        off = i + len(KEY_ANCHOR)
        if data[off:off + 64] == b"\x00" * 64:  # 全零 = 偏移错位特征
            return None
        return off
    except Exception:
        return None


def _sha256(b: bytes) -> bytes:
    return hashlib.sha256(b).digest()


def _hmac(key: bytes, msg: bytes) -> bytes:
    return hmac.new(key, msg, hashlib.sha256).digest()


def _find_exe(version: str) -> str | None:
    """按版本猜官方客户端 exe 位置（LOCALAPPDATA\\AtomCode\\atomcode.exe）。"""
    la = os.environ.get("LOCALAPPDATA")
    if la:
        p = Path(la) / "AtomCode" / "atomcode.exe"
        if p.exists():
            return str(p)
    return None


def _default_auth_path() -> str | None:
    home = Path.home()
    p = home / ".atomcode" / "auth.toml"
    return str(p) if p.exists() else None


def _resolve_path(p: str) -> str:
    """相对路径相对代理根目录解析（auths/ 下的上传凭证）。"""
    q = Path(p)
    if not q.is_absolute():
        return str((BASE_DIR / q).resolve())
    return p


class AtomCodeCredential:
    """auth.toml 解析结果（只有需要的字段）。"""

    def __init__(self, access_token, user_id, refresh_token, expires_at):
        self.access_token = access_token
        self.user_id = user_id
        self.refresh_token = refresh_token
        self.expires_at = expires_at  # unix 秒；0 = 未知

    @property
    def expired(self) -> bool:
        if not self.expires_at:
            return False
        return time.time() >= self.expires_at - 300  # 官方同款 5 分钟安全边距


def _parse_auth_text(txt: str) -> AtomCodeCredential:
    """解析 auth.toml 文本（结构固定，无需依赖 tomllib）。"""

    def grab(key: str) -> str | None:
        marker = f'{key} = "'
        i = txt.find(marker)
        if i < 0:
            return None
        j = txt.find('"', i + len(marker))
        return txt[i + len(marker):j] if j > 0 else None

    token = grab("access_token")
    uid = grab("id")  # [user] 表下的 id
    rtok = grab("refresh_token")
    try:
        created = int(txt.split("created_at = ")[1].split("\n")[0].strip())
        expires_in = int(txt.split("expires_in = ")[1].split("\n")[0].strip())
        expires_at = created + expires_in
    except (ValueError, IndexError):
        expires_at = 0
    if not token or not uid:
        raise ValueError("auth.toml 缺 access_token 或 user.id")
    return AtomCodeCredential(token, uid, rtok, expires_at)


def _parse_auth_toml(path: str) -> AtomCodeCredential:
    return _parse_auth_text(open(path, encoding="utf-8").read())


# 公开别名（管理接口解析上传文本用）
parse_auth_text = _parse_auth_text


class AtomCodeChannel:
    """一个 AtomCode 账号通道：一份凭证文件 + 独立刷新/签名。

    多账号 = 一个 provider 下多个 AtomCodeChannel，标签（label）用于
    负载跟踪与用量记账，必须全局唯一。
    """

    def __init__(self, name: str, cfg: dict, label: str = ""):
        self.name = name          # provider 名（atomcode）
        self.label = label or name  # 账号标签（统计/负载 key）
        self._lock = threading.Lock()
        self._auth_mtime = None
        self._cred: AtomCodeCredential | None = None
        self._master_key: bytes | None = None
        self.version = str(cfg.get("version") or "5.0.8")
        self.base_url = (cfg.get("base_url") or DEFAULT_LLM_BASE).rstrip("/")
        self.exe_path = cfg.get("exe_path") or _find_exe(self.version)
        self.master_key_hex = cfg.get("master_key_hex") or ""
        ap = cfg.get("auth_path") or _default_auth_path()
        self.auth_path = _resolve_path(ap) if ap else None
        self.last_error: str | None = None

    # ─── 凭证 ─────────────────────────────────────────────
    def _load_cred(self) -> AtomCodeCredential:
        """mtime 感知加载凭证文件（文件更新后自动生效）。"""
        if not self.auth_path or not Path(self.auth_path).exists():
            raise FileNotFoundError(
                f"AtomCode auth.toml 不存在（{self.auth_path}）；请先登录官方客户端或上传凭证")
        st = os.stat(self.auth_path)
        if self._cred and self._auth_mtime == st.st_mtime:
            return self._cred
        cred = _parse_auth_toml(self.auth_path)
        with self._lock:
            self._auth_mtime = st.st_mtime
            self._cred = cred
        log.info("[atomcode] 凭证已加载/更新: %s user=%s (mtime=%s)",
                 self.label, cred.user_id,
                 time.strftime("%m-%d %H:%M:%S", time.localtime(st.st_mtime)))
        return cred

    def _write_back(self, cred: AtomCodeCredential):
        """把刷新后的 token 写回自己的凭证文件（多账号各自独立续期）。"""
        try:
            src = open(self.auth_path, encoding="utf-8").read()
            import re
            src = re.sub(r'access_token = "[^"]*"',
                         f'access_token = "{cred.access_token}"', src)
            if cred.refresh_token:
                src = re.sub(r'refresh_token = "[^"]*"',
                             f'refresh_token = "{cred.refresh_token}"', src)
            src = re.sub(r'created_at = \d+',
                         f'created_at = {int(time.time())}', src)
            with open(self.auth_path, "w", encoding="utf-8", newline="\n") as f:
                f.write(src)
            with self._lock:
                self._auth_mtime = os.stat(self.auth_path).st_mtime
                self._cred = cred
            log.info("[atomcode] %s 刷新后的 token 已写回凭证文件", self.label)
        except Exception as e:
            log.error("[atomcode] %s 写回凭证失败: %s", self.label, e)

    def _refresh(self, old_token: str) -> AtomCodeCredential:
        """用 refresh_token 换新 access_token。"""
        cred = self._load_cred()
        if cred.access_token != old_token:
            return cred  # 别的线程已刷新
        if not cred.refresh_token:
            raise RuntimeError("无 refresh_token，请重新登录官方 AtomCode 客户端")
        import httpx
        r = httpx.post(PLATFORM_REFRESH_URL,
                       json={"refresh_token": cred.refresh_token},
                       headers={"User-Agent": f"atomcode/{self.version}",
                                "Content-Type": "application/json"},
                       timeout=15)
        if r.status_code != 200:
            raise RuntimeError(f"token 刷新失败 HTTP {r.status_code}: {r.text[:200]}")
        data = r.json()
        new = AtomCodeCredential(
            data["access_token"],
            data.get("user", {}).get("id") or cred.user_id,
            data.get("refresh_token") or cred.refresh_token,
            int(time.time()) + int(data.get("expires_in") or 604800),
        )
        self._write_back(new)
        return new

    def get_cred(self) -> AtomCodeCredential:
        """取有效凭证：过期自动刷新（刷新失败抛异常，由调用方处理）。"""
        cred = self._load_cred()
        if cred.expired:
            log.info("[atomcode] %s token 已过期，自动刷新...", self.label)
            cred = self._refresh(cred.access_token)
        return cred

    # ─── master key ───────────────────────────────────────
    def load_master_key(self) -> bytes:
        """取签名主密钥。优先 config 的 master_key_hex（Linux/服务器免 exe），
        其次从官方 exe 的固定偏移提取（64B blob XOR 半区）。"""
        if self._master_key is not None:
            return self._master_key
        # 1) config.json providers.atomcode.master_key_hex（64 个 hex 字符 = 32B）
        hex_str = (self.master_key_hex or "").strip()
        if hex_str:
            try:
                master = bytes.fromhex(hex_str)
            except ValueError:
                raise ValueError("master_key_hex 不是合法 hex（需 64 个字符）")
            if len(master) != 32:
                raise ValueError(f"master_key_hex 长度 {len(master)*2} 字符，应为 64（32 字节）")
            self._master_key = master
            return master
        # 2) 从 exe 提取（Windows 本机模式）
        if not self.exe_path or not Path(self.exe_path).exists():
            raise FileNotFoundError(
                f"AtomCode 客户端 exe 不存在: {self.exe_path}；"
                f"请在 config.json providers.atomcode 配置 exe_path（本机）"
                f"或 master_key_hex（服务器，免 exe）")
        off = _locate_key_offset(self.exe_path) or MASTER_KEY_OFFESET
        if off != MASTER_KEY_OFFESET:
            log.info("[atomcode] master key 自动定位: %#x（锚点字符串后 64B）", off)
        with open(self.exe_path, "rb") as f:
            f.seek(off)
            blob = f.read(64)
        if len(blob) < 64:
            raise ValueError("读取 master key blob 失败（exe 过小）")
        master = bytes(a ^ b for a, b in zip(blob[:32], blob[32:]))
        if master == b"\x00" * 32:
            raise ValueError("master key blob 全零，偏移可能不对（客户端升级？）")
        self._master_key = master
        return master

    # ─── 签名 ─────────────────────────────────────────────
    def sign_headers(self, method: str, path: str, body: bytes) -> dict:
        """生成一次请求的完整签名头（含 Authorization）。"""
        cred = self.get_cred()
        master = self.load_master_key()
        ts = int(time.time())
        nonce = os.urandom(16)
        bucket = ts // 3600
        salt = (cred.user_id.encode() + b"\x01" + struct.pack("<Q", bucket)
                + _sha256(cred.access_token.encode()) + _sha256(self.version.encode()))
        prk = _hmac(salt, master)                          # HKDF-Extract
        okm = _hmac(prk, b"atomcode-signing-v1" + b"\x01")  # HKDF-Expand（单块）
        msg = (b"v1\n" + method.encode() + b"\n" + path.encode() + b"\n"
               + str(ts).encode() + b"\n" + nonce.hex().encode() + b"\n"
               + _sha256(body).hex().encode())
        sig = _hmac(okm, msg)
        return {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": "Bearer " + cred.access_token,
            "X-AtomCode-Sig": "v1:" + sig.hex(),
            "X-AtomCode-Ts": str(ts),
            "X-AtomCode-Nonce": nonce.hex(),
            "X-AtomCode-Alg": "1",
            "X-AtomCode-Ver": self.version,
            "User-Agent": "atomcode/" + self.version,
        }

    # ─── 状态（供管理面板） ────────────────────────────────
    def status(self) -> dict:
        try:
            cred = self._load_cred()
            self.load_master_key()
            err = None
        except Exception as e:
            cred = None
            err = str(e)
        return {
            "name": self.name,
            "label": self.label,
            "version": self.version,
            "auth_path": self.auth_path,
            "exe_path": self.exe_path,
            "base_url": self.base_url,
            "logged_in": cred is not None,
            "user_id": cred.user_id if cred else None,
            "expires_at": cred.expires_at if cred else None,
            "token_valid_days": round(((cred.expires_at or 0) - time.time()) / 86400, 1) if cred else 0,
            "ok": err is None,
            "error": err,
        }


# ─── 通道注册表（config.json 驱动，mtime 缓存） ─────────────────────────────

_channels: dict[str, list] = {}      # provider name -> [AtomCodeChannel]
_channels_cfg_mtime = None
_channel_rr: dict[str, int] = {}     # provider name -> 轮转游标（同负载时轮转选账号）


def _parse_auth_paths(name: str, cfg: dict) -> list:
    """把 auth_path / auth_paths 归一化为 [{"path": ..., "label": ...}] 列表。

    规则（与 key 池 _parse_keys 一致）：
    - 数组元素可为字符串或 {"path":..., "label":...}；单 auth_path 兼容
    - 同一路径去重；label 缺省时：单账号用通道名（兼容旧统计），多账号用 <name>·<N>
    - label 必须唯一（负载跟踪/记账的 key）
    """
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

    # 同 path 去重（显式 label 优先，保留首次出现顺序）
    by_path = {}
    for path, label in raw:
        if path not in by_path:
            by_path[path] = [path, ""]
        if label:
            by_path[path][1] = label
    out = [{"path": p, "label": l} for p, l in by_path.values()]

    # 默认 label：单账号 → 通道名；多账号 → <name>·<N>（防撞）
    used = {o["label"] for o in out if o["label"]}
    n = 1
    for i, o in enumerate(out):
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
    """按 config providers 段重建 AtomCode 通道池（providers._load_providers 调用）。"""
    global _channels, _channels_cfg_mtime
    provs = (cfg.get("providers") or {})
    new = {}
    for name, p in provs.items():
        if isinstance(p, dict) and p.get("type") == "atomcode":
            entries = _parse_auth_paths(name, p)
            chs = [AtomCodeChannel(name, {**p, "auth_path": e["path"]}, label=e["label"])
                   for e in entries]
            if not chs:
                # 无任何凭证配置：仍放一个占位通道（status 报"未登录"，请求报 503 提示）
                chs = [AtomCodeChannel(name, p)]
            new[name] = chs
    _channels = new


def channels_for(name: str) -> list:
    """某 provider 的全部账号通道。"""
    return _channels.get(name) or []


def get_channel(name: str) -> AtomCodeChannel | None:
    """兼容接口：取该 provider 的首个通道（单账号时代的行为）。"""
    chs = channels_for(name)
    return chs[0] if chs else None


def all_channels() -> dict:
    """全部通道池 {provider_name: [AtomCodeChannel]}。"""
    return {k: list(v) for k, v in _channels.items()}


def pick_channel(name: str, exclude: set = None) -> AtomCodeChannel | None:
    """从该 provider 的账号池选一个通道：最少进行中请求优先，同负载轮转。

    exclude：重试时排除已失败通道的 label 集合。无可用通道返回 None。
    """
    from .router import registry
    chs = channels_for(name)
    if exclude:
        chs = [c for c in chs if c.label not in exclude]
    if not chs:
        return None
    if len(chs) == 1:
        return chs[0]
    # 负载键加 provider 前缀隔离命名空间：防止 agnes 的"主号"与 atomcode 的
    # "主号"这类同名 label 互相污染负载计数（曾导致 agnes 主号永远轮不上）
    loads = {c.label: registry.load_of(f"ac:{name}|{c.label}") for c in chs}
    start = _channel_rr.get(name, 0) % len(chs)
    best = None
    for i in range(len(chs)):
        c = chs[(start + i) % len(chs)]
        if best is None or loads[c.label] < loads[best.label]:
            best = c
    _channel_rr[name] = (start + 1) % len(chs)
    return best


def sign_headers_for(name: str, method: str, path: str, body: bytes) -> dict:
    """兼容接口：任选一个可用通道签名（water 探针等单次场景用）。"""
    ch = pick_channel(name)
    if not ch:
        raise KeyError(f"AtomCode 通道 {name} 未配置")
    return ch.sign_headers(method, path, body)
