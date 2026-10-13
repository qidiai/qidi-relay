#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vibex2api — 把 VibeX(RunningHub) 的对话能力封装成 OpenAI 兼容 API 的单文件适配器。

架构:
    浏览器 SPA --REST /api/*--> 网关 --> 沙箱容器内 cc-server(Claude Code 无头服务端)
              \\--WS wss://.../app-ws/{app_id}/ws--> (Cookie 鉴权)

对外端点:
    GET  /v1/models
    POST /v1/chat/completions   (支持 SSE 流式)
    GET  /healthz

用法:
    python vibex2api.py               启动服务(默认 127.0.0.1:8787)
    python vibex2api.py --check       检查 token, 打印 JWT / providers / apps
    python vibex2api.py --selftest    离线自测(不需要 token)
    python vibex2api.py --help        显示本帮助

配置: 与脚本同目录的 vibex2api.config.json (运行时自动生成/更新)。
      环境变量 RH_ACCESSTOKEN 优先级高于配置文件里的 token。
依赖: 仅 aiohttp + Python 标准库。
"""

# ============================== 1. imports + utf-8 stdout + 常量 ==============================
import asyncio, base64, json, os, sys, time, uuid
from pathlib import Path

try:
    # Windows 控制台默认 GBK, 直接输出中文会炸; 统一改成 utf-8。
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from aiohttp import ClientError, ClientSession, ClientTimeout, WSMsgType, web

__version__ = "1.0.0"


def _app_dir() -> Path:
    """配置文件所在目录: 打包后取 EXE 同目录, 脚本模式取脚本同目录。"""
    if getattr(sys, "frozen", False):                  # PyInstaller 打包后
        return Path(sys.executable).resolve().parent   # EXE 同目录(临时解压目录每次变, 不能放 __file__)
    return Path(__file__).resolve().parent             # 脚本模式: 脚本同目录


HERE = _app_dir()
CONFIG_PATH = HERE / "vibex2api.config.json"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# REST 单请求超时(总 120s / 连接 30s), 避免上游挂死时默认等 300s
_CLIENT_TIMEOUT = ClientTimeout(total=120, connect=30)

DEFAULTS = {
    "base_url": "https://vibex.runninghub.cn",
    "listen_host": "127.0.0.1", "listen_port": 8787,
    "api_key": "",                # 适配器自身鉴权; 空=不校验
    "token": "",                  # Rh-Accesstoken; 环境变量 RH_ACCESSTOKEN 优先
    "tenant_id": "",              # X-Tenant-Id; 空则自动 uuid4 生成并持久化
    "app_name": "vibex2api",      # 复用的沙箱项目名
    "app_type": "web",
    "model": "",                  # 默认 provider id; 空=自动选(优先 billing_tier=="lite")
    "fresh_session": True,        # 每请求先发 new_session (对齐 OpenAI 无状态)
    "turn_timeout": 900,          # 单轮秒
    "ready_timeout": 90,          # WS init->ready 秒
    "queue_limit": 4,             # 排队并发上限, 超出 429
    "heartbeat_s": 15,            # SSE 心跳注释间隔
    "include_thinking": False,    # thinking_delta -> delta.reasoning_content
    "expose_tools": False,        # tool_use -> 文本 "[tool: 名字]" 注入流
    "chat_instruction": "",       # prompt 尾部自定义指令; 空=用内置默认指令
    "connect_mode": "auto",       # auto=首次连接用 init_root, 之后用 init
}
DEFAULT_INSTRUCTION = ("請直接以「助手」的身份回答最後一條用戶消息: 直接給出回答內容本身; "
                       "這是一次純問答, 無需創建或修改任何項目文件。")
IMAGE_PLACEHOLDER = "[圖片內容不支持, 已忽略]"


# ============================== 2. log / load_config / persist / jwt_decode ==============================
def log(*args):
    """带时间戳的标准输出日志。"""
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        print(f"[{ts}]", *args, flush=True)
    except Exception:
        pass


def load_config():
    """DEFAULTS <- config.json <- env RH_ACCESSTOKEN; tenant_id 为空则生成并持久化。"""
    cfg = dict(DEFAULTS)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                for k in DEFAULTS:                 # 只接受已知键, 避免脏数据
                    if k in data:
                        cfg[k] = data[k]
        except Exception as e:
            log("配置文件读取失败, 使用默认值:", e)
    env_token = (os.environ.get("RH_ACCESSTOKEN") or "").strip()
    if env_token:
        cfg["token"] = env_token
    if not cfg.get("tenant_id"):                   # 租户 ID 为空 -> 生成并写回
        cfg["tenant_id"] = uuid.uuid4().hex
        try:
            persist(cfg)
        except Exception as e:
            log("tenant_id 持久化失败(忽略):", e)
    return cfg


def persist(cfg):
    """写回配置文件; 仅包含 DEFAULTS 的键。"""
    out = {k: cfg.get(k, DEFAULTS[k]) for k in DEFAULTS}
    tmp = str(CONFIG_PATH) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_PATH)


def jwt_decode(token):
    """解码 JWT 的 payload 段, 失败返回 None。"""
    if not token or not isinstance(token, str):
        return None
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return None
        seg = parts[1]
        seg += "=" * (-len(seg) % 4)               # 补 = padding
        data = json.loads(base64.urlsafe_b64decode(seg).decode("utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _is_valid_token_shape(t: str) -> bool:
    """JWT 形状粗校验: 非空、三段、两处 '.'(且中段够长, 排除明显占位串)"""
    return bool(t) and t.count(".") == 2 and len(t.split(".")[1]) > 10


_TOKEN_GUIDE = """未检测到 Rh-Accesstoken。获取方法（约 30 秒）:
  1. 浏览器打开 https://vibex.runninghub.cn 并登录（手机号注册）
  2. F12 → Application(应用) → Local Storage(本地存储) → 该域名下
  3. 找到键 Rh-Accesstoken，双击值全选复制（长字符串，含两个点）
请把 Rh-Accesstoken 粘贴到这里后回车:"""


def ensure_token_interactive(cfg) -> None:
    """首次运行的 token 交互引导: 已配置则直接返回; 不可交互则只打印指引。"""
    if cfg.get("token"):
        return
    interactive = sys.stdin.isatty() or os.environ.get("VIBEX_BOOTSTRAP_FORCE") == "1"
    if not interactive:
        print("token 未配置: 请设置环境变量 RH_ACCESSTOKEN, 或写入同目录 vibex2api.config.json 的 token 字段。")
        return
    for _ in range(3):
        print(_TOKEN_GUIDE)
        try:
            t = input().strip()
        except (EOFError, KeyboardInterrupt):
            t = ""
        if _is_valid_token_shape(t):
            cfg["token"] = t
            persist(cfg)
            print("token 已保存到 vibex2api.config.json")
            return
        print("格式不正确(应为含两个点的长字符串), 请重试。")
    print("多次输入均不合法, 已跳过。请稍后手动配置 token。")


# ============================== 3. class VibexError ==============================
class VibexError(Exception):
    """统一的业务异常: 带 HTTP 状态码与可选错误码。"""
    def __init__(self, message, status=500, code=None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


# ============================== 4. oai_error ==============================
def _dumps(obj):
    """所有 JSON 输出统一 ensure_ascii=False。"""
    return json.dumps(obj, ensure_ascii=False)


def _oai_error_obj(status, message, code=None, typ="vibex_error"):
    """构造 OpenAI 风格错误对象(流式错误事件也直接复用)。"""
    return {"error": {"message": message, "type": typ, "code": code}}


def oai_error(status, message, code=None, typ="vibex_error"):
    """返回统一的 OpenAI 风格错误响应。"""
    return web.json_response(_oai_error_obj(status, message, code, typ), status=status, dumps=_dumps)


# ============================== 5. class Vibex (REST + WS 客户端) ==============================
class Vibex:
    """VibeX 网关客户端: REST 调用 + 沙箱 WS 对话。"""
    def __init__(self, config):
        self.config = config
        self.session = None
    async def start(self):
        if self.session is None or self.session.closed:
            self.session = ClientSession(timeout=_CLIENT_TIMEOUT)
    async def close(self):
        if self.session is not None and not self.session.closed:
            try:
                await self.session.close()
            except Exception:
                pass
    @property
    def base_url(self):
        return (self.config.get("base_url") or DEFAULTS["base_url"]).rstrip("/")
    def _headers(self):
        """3.3 HTTP 鉴权头。"""
        token = self.config.get("token") or ""
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}", "RH-TOKEN": token,
            "X-Tenant-Id": self.config.get("tenant_id") or "",
            "X-Vibex-Locale": "zh-CN",
            "Origin": self.base_url, "Referer": self.base_url + "/",
            "User-Agent": UA,
        }
        payload = jwt_decode(token)
        if isinstance(payload, dict) and payload.get("sub"):
            sub = str(payload.get("sub"))
            headers["userId"] = sub
            nick = (payload.get("nickName") or payload.get("user_name")
                    or payload.get("username") or payload.get("mobile"))
            headers["username"] = str(nick if nick else sub)
        return headers
    async def api(self, method, path, body=None):
        """发一个 REST 请求并做容错解包 + 未授权识别。"""
        if self.session is None or self.session.closed:
            await self.start()
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        try:
            async with self.session.request(method, self.base_url + path, data=data,
                                            headers=self._headers(),
                                            timeout=_CLIENT_TIMEOUT) as resp:
                status, text = resp.status, await resp.text()
        except ClientError as e:
            raise VibexError(f"请求失败 {method} {path}: {e}", 502)
        except asyncio.TimeoutError:
            raise VibexError(f"请求超时 {method} {path}", 504)
        payload = _loads(text)
        # 未授权: {"code":403,"msg":"TOKEN_MISSION"} / TOKEN_INVALID / 401 / 412
        if isinstance(payload, dict):
            code = payload.get("code")
            msg = str(payload.get("msg") or payload.get("message") or "")
            if code in (401, 403, 412) or "TOKEN" in msg.upper():
                raise VibexError("鉴权失败: VibeX 拒绝了请求(" + (msg or str(code))
                                 + "), 请重新从浏览器提取 Rh-Accesstoken 并写入配置", 401)
        if status in (401, 403):
            raise VibexError("鉴权失败: 请重新从浏览器提取 Rh-Accesstoken", 401)
        if status >= 400:
            raise VibexError(f"接口 {path} 返回 HTTP {status}: {text[:200]}", 502)
        return payload
    async def providers(self):
        """GET /api/llm-providers -> list (容错解包 .providers/.items/.data)。"""
        data = await self.api("GET", "/api/llm-providers")
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("providers", "items", "data"):
                if isinstance(data.get(key), list):
                    return data[key]
        return []
    async def apps_list(self):
        """GET /api/apps -> list。"""
        return _apps_from(await self.api("GET", "/api/apps"))
    async def app_get(self, app_id):
        """GET /api/apps/{id} -> app 对象。"""
        return _unwrap_obj(await self.api("GET", f"/api/apps/{app_id}"))
    async def app_create(self, name, provider_id):
        """POST /api/apps -> 新建沙箱项目。"""
        body = {"name": name, "pinned_host_ip": None, "llm_provider_id": provider_id,
                "app_type": self.config.get("app_type") or "web", "flow": None,
                "enabled_capabilities": None}
        data = await self.api("POST", "/api/apps", body)
        if isinstance(data, dict):
            code = data.get("code")
            if code not in (None, 0):
                msg = str(data.get("msg") or data.get("message") or "")
                if "FREE_PROJECT_LIMIT" in _dumps(data):
                    raise VibexError("免费项目已达上限(约10个), 请在 VibeX 网页端删除旧项目后重试", 409)
                raise VibexError(f"创建项目失败: {msg or code}", 502)
        return _unwrap_obj(data)
    async def app_start(self, app_id):
        """POST /api/apps/{id}/start, 然后轮询到 running 且端口就绪(最长 240s)。"""
        await self.api("POST", f"/api/apps/{app_id}/start", {})
        deadline, last = time.time() + 240, None
        while time.time() < deadline:
            try:
                app = await self.app_get(app_id)
            except VibexError:
                app = None
            if isinstance(app, dict):
                last = app
                if _live_status(app) == "running" and _ports_ok(app):
                    return app
            await asyncio.sleep(2)
        if isinstance(last, dict) and _live_status(last) == "running":   # 超时但已 running 也放行
            return last
        raise VibexError(f"项目 {app_id} 启动超时(240s)", 504)
    async def set_llm_provider(self, app_id, provider_id):
        """PATCH /api/apps/{id}/llm-provider。"""
        await self.api("PATCH", f"/api/apps/{app_id}/llm-provider",
                       {"provider_id": provider_id, "llm_test": False})
    async def ensure_llm_settings(self, app_id):
        """POST /api/apps/{id}/ensure-llm-settings; 失败仅告警。"""
        try:
            await self.api("POST", f"/api/apps/{app_id}/ensure-llm-settings", {})
        except Exception as e:
            log("ensure-llm-settings 调用失败(忽略):", e)
    def _ws_url(self, app_id):
        # REST 走 base_url(可能含 /vc), 但 WS 不带任何 path: wss://<netloc>/app-ws/{id}/ws
        from urllib.parse import urlsplit
        parts = urlsplit(self.base_url)
        scheme = "wss" if parts.scheme == "https" else "ws"
        return f"{scheme}://{parts.netloc}/app-ws/{app_id}/ws"
    def _ws_headers(self):
        """WS 鉴权只带 Cookie。"""
        return {"Cookie": f"Rh-Accesstoken={self.config.get('token') or ''}",
                "Origin": self.base_url, "User-Agent": UA}
    async def _pinger(self, ws, interval=20):
        """每 20s 发一次 {"type":"ping"}。"""
        try:
            while True:
                await asyncio.sleep(interval)
                await ws.send_json({"type": "ping"})
        except asyncio.CancelledError:
            raise
        except Exception:
            return
    async def _handle_ws_message(self, data, out):
        """分派一条服务端 WS 消息; 返回 True 表示本轮结束。"""
        if not isinstance(data, dict):
            return False
        mtype = data.get("type")
        if mtype == "claude_event":
            for kind, value in _claude_events(data.get("event"), self.config):
                await out.put((kind, value))
        elif mtype == "done":
            await out.put(("done", {k: data.get(k) for k in (
                "context_overflow", "session_poison", "balance_insufficient",
                "rate_limit_failover", "failover_model", "primary_model")}))
            return True
        elif mtype == "error":
            await out.put(("error", str(data.get("message") or data.get("code") or "unknown error")))
        # ready / pong / context_compacting / context_compacted / 其它 -> 忽略
        return False
    async def run_turn(self, app, prompt, first_connect, fresh_session, out, turn_timeout):
        """跑完一轮对话, 事件写入 out 队列; 无论正常结束还是异常, 都向 out 写入哨兵 (None, None)。"""
        ping_task, ws = None, None
        try:
            app_id = app.get("app_id") if isinstance(app, dict) else None
            if not app_id:
                raise VibexError("app 对象缺少 app_id", 502)
            if self.session is None or self.session.closed:
                await self.start()
            try:
                ws = await self.session.ws_connect(self._ws_url(app_id), headers=self._ws_headers(),
                                                   autoping=True, heartbeat=None, max_msg_size=0, timeout=30)
            except Exception as e:
                raise VibexError(f"WS 连接失败: {e}", 502)
            ping_task = asyncio.create_task(self._pinger(ws))

            # (1) 发送 init_root(首次) 或 init
            if first_connect:
                init_msg = {"type": "init_root", "cwd": "/workspace/app",
                            "meta": _init_root_meta(app), "replay": False}
            else:
                init_msg = {"type": "init", "cwd": "/workspace/app", "replay": False}
            await ws.send_json(init_msg)

            # (2) 等待 ready
            ready_timeout = float(self.config.get("ready_timeout") or 90)
            ready_deadline = time.time() + ready_timeout
            while True:
                remain = ready_deadline - time.time()
                if remain <= 0:
                    raise VibexError(f"WS 未在 {ready_timeout:.0f}s 内就绪", 504)
                try:
                    msg = await ws.receive(timeout=min(remain, 5.0))
                except asyncio.TimeoutError:
                    continue
                if msg.type in (WSMsgType.CLOSED, WSMsgType.CLOSE, WSMsgType.ERROR):
                    raise VibexError("WS 在就绪前关闭", 502)
                if msg.type != WSMsgType.TEXT:
                    continue
                data = _loads(msg.data)
                if not isinstance(data, dict):
                    continue
                if data.get("type") == "ready":
                    break
                if data.get("type") == "error":
                    raise VibexError("连接失败: " + str(data.get("message")
                                                        or data.get("code") or "unknown"), 502)

            # (3) ensure-llm-settings (失败忽略)
            await self.ensure_llm_settings(app_id)
            # (4) new_session
            if fresh_session:
                await ws.send_json({"type": "new_session"})
            # (5) 发送 prompt
            await ws.send_json({"type": "prompt", "text": prompt, "permission_mode": "dontAsk"})

            # (6) 事件泵
            deadline = time.time() + float(turn_timeout)
            while True:
                remain = deadline - time.time()
                if remain <= 0:
                    try:
                        await ws.send_json({"type": "cancel"})
                    except Exception:
                        pass
                    await out.put(("note", "已达到单轮超时上限, 已发送 cancel"))
                    await out.put(("done", {"timeout": True}))
                    break
                try:
                    msg = await ws.receive(timeout=min(remain, 5.0))
                except asyncio.TimeoutError:
                    continue
                if msg.type in (WSMsgType.CLOSED, WSMsgType.CLOSE):
                    await out.put(("note", "连接关闭"))
                    await out.put(("done", {"ws_closed": True}))
                    break
                if msg.type == WSMsgType.ERROR:
                    await out.put(("note", "连接错误"))
                    await out.put(("done", {"ws_closed": True}))
                    break
                if msg.type != WSMsgType.TEXT:
                    continue
                if await self._handle_ws_message(_loads(msg.data), out):
                    break
        finally:
            # 先收尾(取消 ping、关闭 ws), 最后才放哨兵: 保证消费方被唤醒时
            # worker 的 except 已执行完、state["error"] 已就位(避免错误信息丢失)。
            if ping_task is not None:
                ping_task.cancel()
            if ws is not None:
                try:
                    await ws.close()
                except Exception:
                    pass
            try:
                await out.put((None, None))          # 哨兵: 必须是本 finally 的最后一个动作
            except Exception:
                pass


# ============================== 6. 辅助函数 ==============================
def _loads(s):
    """安全 json 解析。"""
    try:
        return json.loads(s)
    except Exception:
        return None


def _unwrap_obj(data):
    """app 对象可能在顶层, 也可能被 .data / .app 包裹。"""
    if isinstance(data, dict):
        for key in ("data", "app"):
            if isinstance(data.get(key), dict):
                return data[key]
    return data


def _apps_from(data):
    """容错解包 apps 列表。"""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("apps", "items", "data"):
            v = data.get(key)
            if isinstance(v, list):
                return v
            if isinstance(v, dict):
                for k2 in ("apps", "items"):
                    if isinstance(v.get(k2), list):
                        return v[k2]
    return []


def _live_status(app):
    """live.status 优先(需为 dict/str 且无 error 键), 否则 status_cached。"""
    if isinstance(app, dict):
        live = app.get("live")
        if isinstance(live, dict) and isinstance(live.get("status"), str) and "error" not in live:
            return live.get("status")
        return app.get("status_cached")
    return None


def _ports_ok(app):
    """host_ports 里 9000 与 7000 均 > 0。"""
    host_ports = app.get("host_ports") if isinstance(app, dict) else None
    if not isinstance(host_ports, dict):
        return False
    try:
        return int(host_ports.get("9000")) > 0 and int(host_ports.get("7000")) > 0
    except (TypeError, ValueError):
        return False


def _init_root_meta(app):
    """从 app 对象提取 init_root 的 meta。"""
    meta = {k: app.get(k) for k in ("app_id", "name", "app_type", "image", "host_ip",
                                    "host_ports", "created_at", "pocketbase_url")}
    if isinstance(app.get("flow"), str):
        meta["flow"] = app["flow"]
    if isinstance(app.get("enabled_capabilities"), list):
        meta["enabled_capabilities"] = list(app["enabled_capabilities"])
    return meta


def extract_text(content):
    """从 message.content 提取纯文本; 支持 str 与多模态 list。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for blk in content:
            if isinstance(blk, str):
                parts.append(blk)
            elif isinstance(blk, dict):
                if blk.get("type") == "image_url" or "image_url" in blk:
                    parts.append(IMAGE_PLACEHOLDER)
                elif isinstance(blk.get("text"), str):
                    parts.append(blk["text"])
                elif isinstance(blk.get("content"), str):
                    parts.append(blk["content"])
        return "\n".join([p for p in parts if p])
    if isinstance(content, dict) and isinstance(content.get("text"), str):
        return content["text"]
    return str(content)


def build_prompt(messages, chat_instruction=""):
    """把 OpenAI messages 拍平成单条 VibeX prompt。"""
    sys_texts, history = [], []        # history: (标签, 文本)
    for m in messages:
        if not isinstance(m, dict):
            continue
        role = (m.get("role") or "user").lower()
        text = extract_text(m.get("content"))
        if role in ("system", "developer"):
            sys_texts.append(text)
        elif role == "assistant":
            history.append(("助手", text))
        elif role == "tool":
            history.append(("工具輸出", text))
        else:                          # user 及未知角色
            history.append(("用戶", text))
    instruction = chat_instruction or DEFAULT_INSTRUCTION
    # 特例: 仅单条 user 且无 system -> 直接返回原文
    if (not sys_texts and len(messages) == 1 and isinstance(messages[0], dict)
            and (messages[0].get("role") or "").lower() == "user"):
        base = extract_text(messages[0].get("content"))
        return base + "\n\n【任務】\n" + instruction if chat_instruction else base
    blocks = []
    if sys_texts:
        blocks.append("【系統設定】\n" + "\n".join(sys_texts))
    if history:
        blocks.append("【對話記錄】\n" + "\n\n".join(f"【{lb}】\n{tx}" for lb, tx in history))
    blocks.append("【任務】\n" + instruction)
    return "\n\n".join(blocks)


def _oai_usage(usage):
    """把 Claude usage 映射成 OpenAI usage。"""
    it = ot = 0
    if isinstance(usage, dict):
        if isinstance(usage.get("input_tokens"), (int, float)):
            it = int(usage["input_tokens"])
        if isinstance(usage.get("output_tokens"), (int, float)):
            ot = int(usage["output_tokens"])
    return {"prompt_tokens": it, "completion_tokens": ot, "total_tokens": it + ot}


def _fold_sync_events(items):
    """把 (kind, value) 事件序列折叠成 (content, usage, done_flags, last_error)。

    非流式聚合: text/error_text/tool/note 优先; 若这些来源为空, 回退到
    assistant_text 缓冲(上游以完整 assistant 消息交付、无 text_delta 前缀时)。
    """
    parts, assistant_buf, usage, done_flags, last_error = [], [], None, {}, None
    for kind, value in items:
        if kind in ("text", "error_text", "tool", "note"):
            if value:
                parts.append(value)
        elif kind == "assistant_text":
            if value:
                assistant_buf.append(value)
        elif kind == "result":
            usage = (value or {}).get("usage")
        elif kind == "done":
            done_flags = value or {}
        elif kind == "error":
            last_error = last_error or value
    content = "".join(parts) or "".join(assistant_buf)
    return content, usage, done_flags, last_error


def _assistant_fallback(produced, assistant_buf):
    """流式兜底: text 流未产出但 assistant_text 有缓冲时返回其拼接, 否则 None。"""
    if not produced and assistant_buf:
        return "".join(assistant_buf)
    return None


def _claude_events(ev, config):
    """3.6 claude_event.event 分支 -> [(kind, value), ...]。"""
    res = []
    if not isinstance(ev, dict):
        return res
    etype = ev.get("type")
    if etype == "stream_event":
        inner = ev.get("event")
        if isinstance(inner, dict) and inner.get("type") == "content_block_delta":
            delta = inner.get("delta")
            if isinstance(delta, dict):
                if delta.get("type") == "text_delta":
                    res.append(("text", delta.get("text") or ""))
                elif delta.get("type") == "thinking_delta":
                    res.append(("thinking", delta.get("thinking") or ""))
    elif etype == "assistant":
        message = ev.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        is_err = bool(ev.get("isApiErrorMessage")) or bool(ev.get("balance_insufficient"))
        if isinstance(content, list):
            for blk in content:
                if not isinstance(blk, dict):
                    continue
                if blk.get("type") == "text":
                    txt = blk.get("text") or ""
                    res.append(("assistant_text", txt))
                    if is_err:
                        res.append(("error_text", txt))
                elif blk.get("type") == "tool_use" and config.get("expose_tools"):
                    res.append(("tool", f"[tool: {blk.get('name')}]"))
    elif etype == "result":
        raw = ev.get("result")
        res.append(("result", {"subtype": ev.get("subtype"),
                               "result": raw if isinstance(raw, str) else None,
                               "usage": ev.get("usage")}))
    # system / user -> 忽略
    return res


def _match_provider(providers, want):
    """按 id 精确匹配, 再按 display_name / label / name 匹配。"""
    want = str(want)
    for p in providers:
        if isinstance(p, dict) and str(p.get("id") or p.get("provider_id") or "") == want:
            return p
    for p in providers:
        if isinstance(p, dict):
            for key in ("display_name", "label", "name"):
                if want and want == str(p.get(key) or ""):
                    return p
    return None


def _provider_id(provider):
    return (provider.get("id") or provider.get("provider_id")) if isinstance(provider, dict) else None


# ============================== 7. class Engine ==============================
class Engine:
    """编排层: provider 解析 + 沙箱项目复用 + 并发控制。"""
    def __init__(self, config, vibex):
        self.config = config
        self.vibex = vibex
        self.turn_lock = asyncio.Lock()                            # 单 app 同时只跑一轮
        self.queue_sem = asyncio.Semaphore(int(config.get("queue_limit") or 4))
        self.current_provider = None
        self.app = None
        self._connect_count = 0
        self.started = False
    def ensure_started(self):
        """惰性启动标记(真正的 ClientSession 在 Vibex.start 里建)。"""
        self.started = True
    def next_first_connect(self):
        """connect_mode==auto 时首次返回 True(用 init_root), 之后 False(用 init)。"""
        if (self.config.get("connect_mode") or "auto").lower() == "auto":
            first = (self._connect_count == 0)
            self._connect_count += 1
            return first
        return False
    async def resolve_model(self, requested=None):
        """3.8 provider 解析。"""
        providers = await self.vibex.providers()
        if not providers:
            raise VibexError("未能获取 llm-providers 列表(为空)", 502)
        if requested:                                              # 1) 请求指定
            got = _match_provider(providers, requested)
            if got is not None:
                return got
        if self.config.get("model"):                               # 2) 配置默认
            got = _match_provider(providers, self.config["model"])
            if got is not None:
                return got
        for p in providers:                                        # 3) 兜底
            if isinstance(p, dict) and p.get("billing_tier") == "lite":
                return p
        return providers[0]
    async def ensure_app(self, provider):
        """3.9 复用/创建 + 启动 + 绑定 provider。"""
        provider_id = _provider_id(provider)
        name = self.config.get("app_name") or "vibex2api"
        target = None
        for a in await self.vibex.apps_list():
            if isinstance(a, dict) and (a.get("name") == name or a.get("app_id") == name):
                target = a
                break
        if target is None:                                         # 找不到才创建(限流错误已在 app_create 抛 409)
            target = await self.vibex.app_create(name, provider_id)
        app_id = target.get("app_id") if isinstance(target, dict) else None
        if not app_id:
            raise VibexError("无法从沙箱项目对象中确定 app_id", 502)
        started = await self.vibex.app_start(app_id)               # 启动并轮询
        if isinstance(started, dict):
            target = {**target, **started}
        if self.current_provider != provider_id:                   # provider 变更才 PATCH
            try:
                await self.vibex.set_llm_provider(app_id, provider_id)
                self.current_provider = provider_id
            except Exception as e:
                log("set_llm_provider 失败(忽略):", e)
        self.app = target
        return target


# ============================== 8. handlers ==============================
def _auth_check(request, config):
    """适配器自身 api_key 校验; 通过返回 None, 否则返回错误响应。"""
    key = (config.get("api_key") or "").strip()
    if not key:
        return None
    if request.headers.get("Authorization", "") != f"Bearer {key}":
        return oai_error(401, "适配器 API key 校验失败", "INVALID_API_KEY")
    return None


_TOKEN_HINT = ("未配置 VibeX token, 请重新从浏览器提取 Rh-Accesstoken "
               "并写入 vibex2api.config.json 或设置环境变量 RH_ACCESSTOKEN")


async def handle_health(request):
    config = request.app["config"]
    return web.json_response({
        "status": "ok", "service": "vibex2api", "version": __version__,
        "token_configured": bool(config.get("token")),
        "base_url": config.get("base_url"),
        "model_default": config.get("model") or None,
    }, dumps=_dumps)


async def handle_models(request):
    engine = request.app["engine"]
    err = _auth_check(request, engine.config)
    if err is not None:
        return err
    if not engine.config.get("token"):
        return oai_error(401, _TOKEN_HINT, "TOKEN_NOT_CONFIGURED")
    try:
        providers = await engine.vibex.providers()
        if not providers:
            raise VibexError("provider 列表为空", 502)
        data = []
        for p in providers:
            if not isinstance(p, dict) or _provider_id(p) is None:
                continue
            data.append({"id": str(_provider_id(p)), "object": "model", "created": 0,
                         "owned_by": "vibex",
                         "vibex": {"display_name": p.get("display_name") or p.get("label") or p.get("name"),
                                   "billing_tier": p.get("billing_tier"),
                                   "description": p.get("description")}})
        return web.json_response({"object": "list", "data": data}, dumps=_dumps)
    except VibexError as e:
        return oai_error(e.status, e.message, e.code)
    except Exception as e:
        return oai_error(500, str(e))


async def handle_chat(request):
    engine = request.app["engine"]
    config = engine.config
    err = _auth_check(request, config)
    if err is not None:
        return err
    # 入参校验(先于 token 检查, 空 body 固定 400)
    try:
        raw = await request.text()
        body = json.loads(raw) if raw and raw.strip() else None
    except Exception:
        return oai_error(400, "请求体不是合法 JSON", "BAD_REQUEST")
    if not isinstance(body, dict):
        return oai_error(400, "请求体必须是 JSON 对象", "BAD_REQUEST")
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return oai_error(400, "messages 必须是非空数组", "BAD_REQUEST")
    stream = bool(body.get("stream", False))
    if not config.get("token"):
        return oai_error(401, _TOKEN_HINT, "TOKEN_NOT_CONFIGURED")
    requested_model = body.get("model") if isinstance(body.get("model"), str) else None
    try:
        provider = await engine.resolve_model(requested_model)
    except VibexError as e:
        return oai_error(e.status, e.message, e.code)
    except Exception as e:
        return oai_error(500, str(e))
    if engine.queue_sem.locked():                                  # 排队已满 -> 429
        return oai_error(429, "当前排队已达上限, 请稍后重试", "QUEUE_FULL")
    await engine.queue_sem.acquire()
    try:
        return await _do_turn(request, engine, provider, messages, stream, requested_model)
    finally:
        engine.queue_sem.release()


async def _do_turn(request, engine, provider, messages, stream, model):
    """跑一轮: 同步聚合 或 SSE 流式。"""
    prompt = build_prompt(messages, engine.config.get("chat_instruction") or "")
    out = asyncio.Queue()
    state = {"error": None, "entered": False}
    fresh, timeout = bool(engine.config.get("fresh_session", True)), float(engine.config.get("turn_timeout") or 900)
    async def worker():
        try:
            async with engine.turn_lock:
                app = await engine.ensure_app(provider)
                state["entered"] = True
                await engine.vibex.run_turn(app, prompt, engine.next_first_connect(), fresh, out, timeout)
        except VibexError as e:
            state["error"] = e.message
        except asyncio.CancelledError:
            raise
        except Exception as e:
            state["error"] = str(e)
        finally:
            if not state["entered"]:                               # run_turn 自身已放哨兵, 这里只在它之前异常时补
                await out.put((None, None))
    task = asyncio.create_task(worker())
    try:
        if stream:
            return await _stream_out(request, out, state, model, provider, engine.config)
        return await _sync_out(out, state, model, provider)
    finally:
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=5)
        except Exception:
            if not task.done():
                task.cancel()


async def _sync_out(out, state, model, provider):
    """非流式: 聚合文本返回标准 completion。"""
    cid, created, model_name = "chatcmpl-" + uuid.uuid4().hex[:24], int(time.time()), (
        model or str(_provider_id(provider) or "vibex"))
    items = []
    while True:
        item = await out.get()
        if item[0] is None:
            break
        items.append(item)
    content, usage, done_flags, last_error = _fold_sync_events(items)
    if state.get("error"):
        last_error = last_error or state["error"]
    if not content:
        if done_flags.get("balance_insufficient"):
            return oai_error(402, "上游余额不足, 无法完成本轮对话", "BALANCE_INSUFFICIENT")
        if last_error:
            return oai_error(502, last_error, "UPSTREAM_ERROR")
        return oai_error(502, "本轮未产生任何内容", "EMPTY_TURN")
    return web.json_response({
        "id": cid, "object": "chat.completion", "created": created, "model": model_name,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                     "finish_reason": "stop"}],
        "usage": _oai_usage(usage),
    }, dumps=_dumps)


async def _stream_out(request, out, state, model, provider, config):
    """流式: SSE 输出。"""
    cid, created, model_name = "chatcmpl-" + uuid.uuid4().hex[:24], int(time.time()), (
        model or str(_provider_id(provider) or "vibex"))
    heartbeat, include_thinking = float(config.get("heartbeat_s") or 15), bool(config.get("include_thinking"))
    resp = web.StreamResponse(status=200, headers={
        "Content-Type": "text/event-stream; charset=utf-8",
        "Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    await resp.prepare(request)
    def _chunk(delta, finish=None, usage=None):
        obj = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": model_name,
               "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
        if usage is not None:
            obj["usage"] = usage
        return obj
    async def send(obj):
        await resp.write(("data: " + _dumps(obj) + "\n\n").encode("utf-8"))
    await send(_chunk({"role": "assistant"}))                      # 首块: 角色
    produced, usage, done_flags, last_error, assistant_buf = False, None, {}, None, []
    while True:
        try:
            item = await asyncio.wait_for(out.get(), timeout=heartbeat)
        except asyncio.TimeoutError:
            await resp.write(b": hb\n\n")                          # 心跳注释
            continue
        kind, value = item
        if kind is None:
            break
        if kind == "text":
            if value:
                produced = True
                await send(_chunk({"content": value}))
        elif kind == "assistant_text":
            if value:
                assistant_buf.append(value)                        # text_delta 未覆盖时兜底
        elif kind == "thinking":
            if include_thinking and value:
                await send(_chunk({"reasoning_content": value}))
        elif kind in ("tool", "note", "error_text"):
            if value:
                produced = True
                await send(_chunk({"content": value}))
        elif kind == "result":
            usage = (value or {}).get("usage")
        elif kind == "done":
            done_flags = value or {}
        elif kind == "error":
            last_error = last_error or value
    if state.get("error"):
        last_error = last_error or state["error"]
    fb = _assistant_fallback(produced, assistant_buf)               # 无 text 流但有完整 assistant 消息 -> 兜底
    if fb is not None:
        produced = True
        await send(_chunk({"content": fb}))
    if not produced and last_error:                                # error 且无内容 -> 错误事件
        await send(_oai_error_obj(502, last_error, "UPSTREAM_ERROR"))
        await resp.write(b"data: [DONE]\n\n")
        await resp.write_eof()
        return resp
    if done_flags.get("balance_insufficient"):
        await send(_chunk({"content": "\n[上游余额不足]"}))
    await send(_chunk({}, finish="stop", usage=_oai_usage(usage)))
    await resp.write(b"data: [DONE]\n\n")
    await resp.write_eof()
    return resp


# ============================== 9. make_app + cmd_* + main ==============================
async def _on_startup(app):
    await app["engine"].vibex.start()


async def _on_cleanup(app):
    await app["engine"].vibex.close()


def make_app(config):
    """构建 aiohttp 应用。"""
    vibex = Vibex(config)
    engine = Engine(config, vibex)
    engine.ensure_started()
    app = web.Application()
    app["config"], app["engine"] = config, engine
    app.router.add_get("/healthz", handle_health)
    app.router.add_get("/v1/models", handle_models)
    app.router.add_post("/v1/chat/completions", handle_chat)
    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    return app


async def _check_async(config):
    vibex = Vibex(config)
    await vibex.start()
    try:
        print("== Providers ==")
        try:
            providers = await vibex.providers()
        except Exception as e:
            print("  获取失败:", e)
            providers = []
        for p in providers:
            if isinstance(p, dict):
                print(f"  - id={_provider_id(p)} tier={p.get('billing_tier')} "
                      f"name={p.get('display_name') or p.get('label') or p.get('name')}")
        if not providers:
            print("  (空)")
        print("== Apps ==")
        try:
            apps = await vibex.apps_list()
        except Exception as e:
            print("  获取失败:", e)
            apps = []
        for a in apps:
            if isinstance(a, dict):
                print(f"  - app_id={a.get('app_id')} name={a.get('name')} "
                      f"status={_live_status(a)} ports_ok={_ports_ok(a)}")
        if not apps:
            print("  (空)")
    finally:
        await vibex.close()


def cmd_check(config):
    """--check: 解码 JWT, 打印 providers / apps。"""
    ensure_token_interactive(config)               # load_config 之后、真正检查之前
    token = config.get("token") or ""
    if not token:
        print("未配置 VibeX token。")
        print("请从浏览器打开 vibex.runninghub.cn -> DevTools -> Application -> Cookies,")
        print("复制 Rh-Accesstoken 的值, 然后:")
        print("  方式一: set RH_ACCESSTOKEN=<粘贴的值>")
        print("  方式二: 写入同目录的 vibex2api.config.json 的 token 字段")
        print("配置完成后重新运行: python vibex2api.py --check")
        return 1
    print("== JWT payload ==")
    payload = jwt_decode(token)
    if payload is None:
        print("  (无法解析为 JWT)")
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        exp = payload.get("exp")
        if isinstance(exp, (int, float)):
            print("  exp:", time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(exp)))
    try:
        asyncio.run(_check_async(config))
    except Exception as e:
        print("检查失败:", e)
        return 1
    return 0


def cmd_selftest():
    """--selftest: 离线自测, 不需要 token。"""
    def _b64u(obj):
        return base64.urlsafe_b64encode(json.dumps(obj, ensure_ascii=False).encode("utf-8")
                                        ).decode("utf-8").rstrip("=")
    # 1) jwt_decode: 手工构造 JWT
    token = (_b64u({"alg": "HS256", "typ": "JWT"}) + "."
             + _b64u({"sub": "user-123", "nickName": "tester", "exp": 4102444800}) + ".sig")
    payload = jwt_decode(token)
    assert isinstance(payload, dict), "jwt_decode 应返回 dict"
    assert payload.get("sub") == "user-123", "jwt_decode sub 解析错误"
    assert payload.get("nickName") == "tester", "jwt_decode nickName 解析错误"
    assert jwt_decode("not-a-jwt") is None, "非法 JWT 应返回 None"
    # 2) build_prompt: 多轮 + system
    msgs = [{"role": "system", "content": "你是一个严谨的助手"},
            {"role": "user", "content": "1+1=？"},
            {"role": "assistant", "content": "2"},
            {"role": "user", "content": "那 2+2 呢"}]
    prompt = build_prompt(msgs, "")
    for needle in ("【系統設定】", "【用戶】", "1+1=？", "【助手】", "【任務】"):
        assert needle in prompt, f"build_prompt 缺少 {needle}"
    assert prompt.index("【系統設定】") < prompt.index("【用戶】") < prompt.index("1+1=？")
    # 2b) build_prompt: 单条 user 特例 == 原文
    assert build_prompt([{"role": "user", "content": "只问一句"}], "") == "只问一句", "单条 user 特例应返回原文"
    # 3) extract_text: 多模态
    got = extract_text([{"type": "text", "text": "你好"},
                        {"type": "image_url", "image_url": {"url": "http://x/y.png"}}, "尾巴"])
    assert "你好" in got, "extract_text 文本块丢失"
    assert IMAGE_PLACEHOLDER in got, "extract_text 图片占位符丢失"
    assert "尾巴" in got, "extract_text 纯字符串块丢失"
    assert extract_text("纯文本") == "纯文本"
    # 4) oai_error 结构是合法 JSON
    parsed = json.loads(_dumps(_oai_error_obj(400, "bad request", "BAD_REQUEST")))
    assert parsed["error"]["type"] == "vibex_error"
    assert parsed["error"]["message"] == "bad request"
    assert parsed["error"]["code"] == "BAD_REQUEST"

    # 5) run_turn: ws_connect 抛异常时也必须放哨兵(防死锁; 纯离线, 假 session 不联网)
    class _BoomSession:
        closed = False

        async def ws_connect(self, *a, **k):
            raise RuntimeError("simulated connect failure")

    async def _sentinel_check():
        vx = Vibex({"base_url": "https://example.invalid", "token": "x.y.z"})
        vx.session = _BoomSession()
        q = asyncio.Queue()
        try:
            await vx.run_turn({"app_id": "test-app"}, "hi", True, True, q, 5)
        except VibexError:
            pass                                    # 连接失败 -> 预期抛 VibexError
        try:
            return q.get_nowait()
        except asyncio.QueueEmpty:
            return None

    item = asyncio.run(_sentinel_check())
    assert item is not None and item[0] is None, "run_turn 连接失败路径缺少哨兵(消费方会死等)"

    # 6) assistant_text 兜底: 非流式聚合(纯函数)
    content, _, _, _ = _fold_sync_events([("assistant_text", "合成错误 A"),
                                         ("assistant_text", "合成错误 B"), ("done", {})])
    assert content == "合成错误 A合成错误 B", "assistant_text 无 text 流时未兜底"
    content, _, _, _ = _fold_sync_events([("text", "真实回答"), ("assistant_text", "重复")])
    assert content == "真实回答", "有 text 流时不应使用 assistant_text"
    content, _, _, _ = _fold_sync_events([("error_text", "错误文本")])
    assert content == "错误文本", "error_text 行为应保持"

    # 7) assistant_text 兜底: 流式判定(纯函数)
    assert _assistant_fallback(False, ["a", "b"]) == "ab", "流式兜底应拼接 assistant_text"
    assert _assistant_fallback(True, ["a"]) is None, "已有 text 流时不应兜底"
    assert _assistant_fallback(False, []) is None, "无缓冲时不应兜底"

    # 8) run_turn 收尾时序: 哨兵必须是 finally 最后一个动作, 保证消费方唤醒时 state["error"] 已就位
    class _FakeWs:
        async def send_json(self, obj):
            pass

        async def receive(self, timeout=None):
            class _M:
                type = WSMsgType.TEXT
                data = '{"type": "error", "message": "boom"}'
            return _M()

        async def close(self):
            await asyncio.sleep(0.1)                # 真实让出点: 哨兵若先入队即触发竞态

    class _FakeSession:
        closed = False

        async def ws_connect(self, *a, **k):
            return _FakeWs()

    async def _race_check():
        vx = Vibex({"base_url": "https://example.invalid", "token": "x.y.z"})
        vx.session = _FakeSession()
        q, st = asyncio.Queue(), {"error": None}

        async def worker():
            try:
                await vx.run_turn({"app_id": "test-app"}, "hi", True, True, q, 5)
            except VibexError as e:
                st["error"] = e.message

        t = asyncio.create_task(worker())
        got = await q.get()                         # 消费方: 拿到哨兵那一刻
        err_at_wakeup = st["error"]
        await t
        return got, err_at_wakeup

    got, err_at_wakeup = asyncio.run(_race_check())
    assert got[0] is None, "run_turn 应放出哨兵"
    assert err_at_wakeup, "哨兵必须是 finally 最后动作, 否则消费方唤醒时丢失 state['error']"

    # 9) _is_valid_token_shape: JWT 形状粗校验
    assert _is_valid_token_shape("a." + "b" * 12 + ".c") is True, "段长够的三段串应判合法"
    assert _is_valid_token_shape("x.yyyyyyyyyyyy.z") is True
    assert _is_valid_token_shape("abc") is False, "无点应判非法"
    assert _is_valid_token_shape("a.b") is False, "两段应判非法"
    assert _is_valid_token_shape("") is False, "空串应判非法"
    # 规格正文以 "a.b.c" 举例并标注"(段长够)": 按给定实现, 中段须 >10 字符,
    # 字面 "a.b.c"(中段 "b" 仅 1 字符) 判非法; True 分支由上面段长够的例子覆盖。
    assert _is_valid_token_shape("a.b.c") is False, "中段过短应判非法"

    # 10) _app_dir(): 脚本模式 == 脚本目录; frozen(EXE) == EXE 所在目录
    if getattr(sys, "frozen", False):
        assert _app_dir() == Path(sys.executable).resolve().parent, "_app_dir frozen 时应指向 EXE 目录"
    else:
        assert _app_dir() == Path(__file__).resolve().parent, "_app_dir 非 frozen 时应指向脚本目录"

    print("SELFTEST PASS")
    return 0


def cmd_serve(config):
    """默认: 启动 HTTP 服务。"""
    ensure_token_interactive(config)               # load_config 之后、真正启动之前
    app = make_app(config)
    host = config.get("listen_host") or "127.0.0.1"
    port = int(config.get("listen_port") or 8787)
    print("vibex2api 启动中 ...")
    print(f"  监听地址: http://{host}:{port}")
    print(f"  VibeX 上游: {config.get('base_url')}")
    print("  token 状态: " + ("已配置" if config.get("token")
                             else "未配置(请设置 RH_ACCESSTOKEN 或写入 vibex2api.config.json)"))
    print(f"  默认模型: {config.get('model') or '(自动: 优先 billing_tier==lite)'}")
    if not (config.get("api_key") or "").strip():
        print("  风险提示: 未设置 api_key, 任何能访问本端口的程序都能消耗你的 VibeX 额度;")
        print("            建议设置 api_key, 并仅监听 127.0.0.1")
    print("  OpenAI 兼容端点: GET /v1/models , POST /v1/chat/completions")
    print("  健康检查: GET /healthz")
    web.run_app(app, host=host, port=port)
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if "--help" in argv or "-h" in argv:
        print(__doc__)
        return 0
    config = load_config()
    if "--check" in argv:
        return cmd_check(config)
    if "--selftest" in argv:
        return cmd_selftest()
    return cmd_serve(config)


if __name__ == "__main__":
    sys.exit(main())
