"""cli_api_key 缓存与 LiteLLM 请求签名。

签名机制对齐 codely-cli 的 X-Codely-Signature：
  signingKey = HMAC-SHA256(HMAC-SHA256(ROOT, "codely-signing-v1"), cli_api_key)
  signature  = "v1." + ts + "." + base64url(HMAC-SHA256(signingKey, "v1\\n"+path+"\\n"+ts))
"""

import base64
import hashlib
import hmac
import logging
import os
import re
import secrets
import subprocess
import time
import uuid

import httpx

from .settings import CLI_API_KEY_FETCH_URL

log = logging.getLogger("qidi-relay")


def _detect_cli_version() -> str:
    """从本机 codely CLI 读取实际版本号，失败时回退到 release.52。"""
    # 1) 尝试从环境变量覆盖（config.json 未实现，预留）
    env_ver = os.environ.get("CODELY_CLI_VERSION")
    if env_ver:
        return env_ver
    # 2) 从 codely --version 读取
    for exe in [
        os.path.join(os.environ.get("LOCALAPPDATA", ""),
                     "Programs", "Tuanjie Cowork", "cli", "bin",
                     "win32-x64", "codely.exe"),
        "codely",
    ]:
        try:
            r = subprocess.run([exe, "--version"], capture_output=True,
                               text=True, timeout=5)
            m = re.search(r"(\d+\.\d+\.\d+(?:-release\.\d+|rc\.\d+|\.\d+)?)",
                          r.stdout.strip())
            if m:
                return m.group(1)
        except Exception:
            continue
    return "1.0.0-release.52"


_CLI_VERSION = _detect_cli_version()
_PLATFORM = "win32; x64"
log.info("CLI 版本: %s (User-Agent: codely-cli/%s (%s))", _CLI_VERSION, _CLI_VERSION, _PLATFORM)

SIGNING_ROOT_KEY = bytes.fromhex(
    "406f00f74768ba0cb0cd30f097ec6c2bdacb89c61a38b7dd140838bbd0e98018"
)


def derive_signing_key(cli_api_key: str) -> bytes:
    """从 cli_api_key 派生签名密钥。"""
    k1 = hmac.new(SIGNING_ROOT_KEY, b"codely-signing-v1", hashlib.sha256).digest()
    return hmac.new(k1, cli_api_key.encode("utf-8"), hashlib.sha256).digest()


def make_signature(path: str, signing_key: bytes) -> str:
    """生成 v1.{timestamp}.{base64url} 形式的请求签名。"""
    ts = str(int(time.time()))
    payload = "\n".join(["v1", path, ts])
    digest = hmac.new(signing_key, payload.encode("utf-8"), hashlib.sha256).digest()
    b64 = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return f"v1.{ts}.{b64}"


class _SessionCtx:
    """一个 CLI 会话：持久的 session ID + B3 trace 上下文。

    官方 CLI 每个窗口（进程）一个 session ID，窗口内复用，关闭后消失。
    session ID 4h 轮换模拟 CLI 重启；每次请求生成新 span ID。
    """

    def __init__(self, cli_api_key: str):
        self.cli_api_key = cli_api_key
        self.session_id = str(uuid.uuid4())
        self.trace_id = secrets.token_hex(16)   # 32 hex chars
        self.root_span = secrets.token_hex(8)   # 16 hex chars
        self._born = time.time()

    def _maybe_rotate(self):
        if time.time() - self._born > 4 * 3600:
            self.session_id = str(uuid.uuid4())
            self.trace_id = secrets.token_hex(16)
            self.root_span = secrets.token_hex(8)
            self._born = time.time()

    def span_id(self):
        return secrets.token_hex(8)


import threading

class SessionPool:
    """并发感知 session 池：每个在途请求独占一个 session，请求结束后释放。

    模拟多个 CLI 窗口：
    - 新请求优先复用空闲 session（串行请求自然复用同一个 → 会话连续）
    - 全忙时自动新建 session（模拟又开了一个窗口）
    - 闲置 session 保留 30 分钟后清理（模拟关闭窗口）
    """

    _IDLE_TTL = 1800  # 30 min

    def __init__(self):
        self._lock = threading.Lock()
        self._pools: dict[str, list] = {}  # cli_api_key -> [[ctx, inflight_count, last_used_ts]]

    def acquire(self, cli_api_key: str) -> _SessionCtx:
        with self._lock:
            pool = self._pools.setdefault(cli_api_key, [])
            now = time.time()
            # 清理过期 idle session
            pool[:] = [e for e in pool if e[1] > 0 or now - e[2] < self._IDLE_TTL]
            # 找空闲 session
            for entry in pool:
                if entry[1] == 0:
                    entry[0]._maybe_rotate()
                    entry[1] = 1
                    entry[2] = now
                    return entry[0]
            # 全忙，新建
            ctx = _SessionCtx(cli_api_key)
            pool.append([ctx, 1, now])
            return ctx

    def release(self, ctx: _SessionCtx):
        with self._lock:
            pool = self._pools.get(ctx.cli_api_key, [])
            for entry in pool:
                if entry[0] is ctx:
                    entry[1] = max(0, entry[1] - 1)
                    entry[2] = time.time()
                    break

    def stats(self) -> dict:
        with self._lock:
            out = {}
            for key, pool in self._pools.items():
                active = sum(1 for e in pool if e[1] > 0)
                idle = len(pool) - active
                out[key[:16]] = {"total": len(pool), "active": active, "idle": idle}
            return out


session_pool = SessionPool()


def build_litellm_headers(cli_api_key: str, path: str = "/v1/chat/completions",
                          session: _SessionCtx | None = None) -> dict:
    """构建转发到 LiteLLM 的请求头（含 X-Codely-Signature + B3 追踪头）。

    session: 传入则用指定 session（从 pool acquire 的），None 则临时建一个
    （用于 /v1/models 等非聊天请求，不需要 session 连续性）。
    """
    if session is None:
        session = _SessionCtx(cli_api_key)
    return {
        "Authorization": f"Bearer {cli_api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": f"codely-cli/{_CLI_VERSION} ({_PLATFORM})",
        "x-litellm-session-id": session.session_id,
        "x-b3-traceid": session.trace_id,
        "x-b3-spanid": session.span_id(),
        "x-b3-parentspanid": session.root_span,
        "x-b3-sampled": "1",
        "x-b3-flags": "0",
        "X-Codely-Signature": make_signature(path, derive_signing_key(cli_api_key)),
    }


class ApiKeyCache:
    """缓存 cli_api_key（50 分钟过期），避免频繁向上游换取。"""

    def __init__(self):
        self.cache = {}  # user_id -> {"key": ..., "expires": timestamp}

    def get(self, account: dict) -> str | None:
        uid = account["user_id"]
        now = time.time()
        entry = self.cache.get(uid)
        if entry and entry["expires"] > now:
            return entry["key"]
        key = self._fetch_key_sync(account)
        if key:
            self.set(account, key)
            return key
        return None

    def set(self, account: dict, key: str):
        self.cache[account["user_id"]] = {"key": key, "expires": time.time() + 50 * 60}

    def clear(self):
        self.cache.clear()

    def _fetch_key_sync(self, account: dict) -> str | None:
        """从上游 API 同步获取 cli_api_key。"""
        org_id = account.get("org_id", "")
        headers = {
            "Authorization": f"Bearer {account['access_token']}",
            "Accept": "application/json",
        }
        params = {"teamId": org_id} if org_id else {}
        try:
            with httpx.Client() as client:
                resp = client.get(
                    CLI_API_KEY_FETCH_URL,
                    params=params,
                    headers=headers,
                    timeout=10,
                )
                if resp.status_code == 200:
                    return resp.json().get("cli_api_key")
                log.error("获取 cli_api_key 失败 HTTP %s: %s", resp.status_code, resp.text[:200])
        except Exception as e:
            log.error("获取 cli_api_key 失败: %s", e)
        return None
