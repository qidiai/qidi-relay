"""代理转发路由：/health、/v1/*、/codely/*（兼容旧路径）。

相对 v3 的增强：
- 每次转发记录耗时与结果到 ActivityLog（实时动态面板）
- /health 返回版本与运行时长
"""

import json
import hmac as hmac_mod
import logging
import re
import time
import uuid
from datetime import datetime
from functools import wraps
from urllib.parse import urlparse

import httpx
from flask import Blueprint, Response, g, has_request_context, jsonify, request, stream_with_context
from werkzeug.exceptions import RequestEntityTooLarge

from proxy_core import providers
from proxy_core import cline
from proxy_core import minicode
from proxy_core import anthropic
from proxy_core.accounts import AUTH_COOLDOWN_S, QUOTA_COOLDOWN_S
from proxy_core.atomcode import channels_for, pick_channel
from proxy_core.keystore import build_litellm_headers, session_pool
from proxy_core.router import economic_reroute, get_role_map, registry, resolve_role
from proxy_core.water import water
from proxy_core.db import kdb
from proxy_core.settings import (
    LITELLM_API_BASE,
    VERSION,
    load_config,
    get_api_keys,
    get_thinking_models,
    get_vision_capable_models,
    get_vision_model,
)
from proxy_core.state import SERVER_STARTED_AT, activity, am, api_cache, token_stats

log = logging.getLogger("qidi-relay")

bp = Blueprint("proxy", __name__)


def _record_key_usage(model: str, usage: dict):
    """若本次请求带了 API key（鉴权开启），同步按 key 记账。"""
    if not usage:
        return
    from flask import g, has_request_context
    if not has_request_context():
        return
    label = getattr(g, "api_key_label", None)
    if label:
        token_stats.record_key(label, model, usage)


def require_api_key(f):
    """公网部署时校验客户端 API Key（config.json 的 "api_keys"，空列表 = 不鉴权）。

    兼容两种传递方式：Authorization: Bearer <key> 与 x-api-key: <key>。
    校验通过后把 key 标签挂到 flask.g.api_key_label（供按 key 记账），
    并检查该 key 的日 token 限额（超限返回 429）。
    """
    @wraps(f)
    def wrapper(*args, **kwargs):
        from flask import g
        keys = get_api_keys()
        if not keys:
            return f(*args, **kwargs)
        auth = request.headers.get("Authorization", "")
        supplied = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        if not supplied:
            supplied = request.headers.get("x-api-key", "").strip()
        if supplied:
            for k, meta in keys.items():
                if hmac_mod.compare_digest(supplied, k):
                    g.api_key_label = meta["label"]
                    limit = meta.get("limit")
                    if limit and token_stats.key_exceeded(meta["label"], limit):
                        log.warning("API key %s 今日用量已达限额 %d，拒绝请求", meta["label"], limit)
                        return jsonify({
                            "error": {
                                "message": f"Daily token limit reached ({limit}/day) for this API key.",
                                "type": "rate_limit_error",
                                "param": None,
                                "code": "daily_token_limit_exceeded",
                            }
                        }), 429
                    return f(*args, **kwargs)
        return jsonify({
            "error": {
                "message": "Invalid API key. Set Authorization: Bearer <key> or x-api-key header.",
                "type": "invalid_request_error",
                "param": None,
                "code": "invalid_api_key",
            }
        }), 401
    return wrapper


@bp.route("/health")
def health():
    uptime = int((datetime.now() - SERVER_STARTED_AT).total_seconds())
    return jsonify({
        "status": "ok",
        "version": VERSION,
        "accounts": len(am.accounts),
        "accounts_enabled": sum(1 for a in am.accounts if a.get("enabled", True)),
        "endpoint": f"http://{request.host}",
        "uptime_seconds": uptime,
        "time": datetime.now().isoformat(),
    })


# /v1/models 聚合缓存：组→(到期时间, 模型id列表)。60s 内同组直接用缓存，
# 避免每次列表请求都对上游逐组发 /v1/models 探测。
_models_agg_cache: dict = {}
_MODELS_AGG_TTL = 60.0


def _fetch_models_for_groups() -> list:
    """按「订阅池 + 每个分组」各取一个可用账号问上游，合并所有组能访问的模型。

    背景（0831）：v1/models 之前只随机挑一个账号问上游，挑到别名号（free 组
    <账号ID>，team 只有 alias-only-proxy-models 权限）时订阅模型（GLM-5.3 /
    GLM-5.3-FLASH / KIMI-K3）就从列表里消失——客户端启动拉一次列表就看不到
    这些模型，且"多刷几次才出现"全看轮询撞到谁。改为聚合各组并集，让池子里
    所有账号能跑的模型都稳定暴露。
    """
    now = time.time()
    enabled = [a for a in am.accounts
               if a.get("enabled", True) and not a.get("budget_exceeded")]
    # 分组视图：订阅池（group 为空）+ 每个 group 各取第一个可用账号
    groups: dict = {}
    for a in enabled:
        groups.setdefault(a.get("group") or "", a)
    if not groups:
        return []

    merged: dict = {}
    any_ok = False
    for g, acc in groups.items():
        ck = _models_agg_cache.get("g:" + g)
        if ck and ck[0] > now:
            ids = ck[1]
        else:
            key = api_cache.get(acc)
            ids = []
            if key:
                headers = build_litellm_headers(key, "/v1/models")
                try:
                    with httpx.Client() as client:
                        r = client.get(f"{LITELLM_API_BASE}/v1/models",
                                       headers=headers, timeout=10)
                        if r.status_code == 200:
                            ids = [m.get("id") for m in r.json().get("data", [])
                                   if isinstance(m, dict) and m.get("id")]
                except Exception as e:
                    log.error("获取组 %s（账号 %s）模型列表失败: %s",
                              g or "订阅池", acc["user_id"], e)
            _models_agg_cache["g:" + g] = (now + _MODELS_AGG_TTL, ids)
        if ids:
            any_ok = True
            for mid in ids:
                merged.setdefault(mid, {"id": mid, "object": "model",
                                        "owned_by": "codely-pool"})
    if not any_ok:
        return []
    return list(merged.values())


@bp.route("/v1/models")
@require_api_key
def v1_models():
    # 主账号池模型（分组聚合，覆盖订阅池+各分组全部能力）+ 外部 provider 合并展示
    external = providers.all_models()
    data = {"object": "list", "data": _fetch_models_for_groups()}
    if external:
        existing = {m.get("id") for m in data.get("data", []) if isinstance(m, dict)}
        data.setdefault("data", []).extend([m for m in external if m["id"] not in existing])
    # 角色别名（commander/coder/reviewer...）也作为可请求的 model 展示
    for alias, target in get_role_map().items():
        existing = {m.get("id") for m in data.get("data", []) if isinstance(m, dict)}
        if alias not in existing:
            data["data"].append({"id": alias, "object": "model", "owned_by": f"role→{target}"})
    if not data.get("data"):
        return jsonify({"error": "无可用模型"}), 503
    return jsonify(data)


def _extract_usage_from_line(line: str):
    """从 SSE 行中提取 usage，没有则返回 None。"""
    try:
        if line.startswith("data: ") and "usage" in line:
            payload = json.loads(line[6:].strip())
            return payload.get("usage")
    except Exception:
        pass
    return None


def _extract_model_from_line(line: str):
    """从 SSE 行中提取响应 model 字段（注水检测：核对上游实际用的模型名）。"""
    try:
        if line.startswith("data: ") and '"model"' in line:
            payload = json.loads(line[6:].strip())
            m = payload.get("model")
            if isinstance(m, str) and m:
                return m
    except Exception:
        pass
    return None


# ─── 上游失败的账号级判定 ────────────────────────────────────────────────────
# 换下一个账号有意义的状态码：凭证失效/配额耗尽/网关级故障都是账号维度的；
# 400 一类是请求体本身的问题，换号只会白打 N 次上游，所以如实透给客户端。
FAILOVER_UPSTREAM_STATUSES = {401, 402, 403, 502, 503, 504}
# 一次请求最多换几个账号（含首个）：兜住"整池都返回同一类错误"时的重试放大
MAX_ACCOUNT_ATTEMPTS = 3
# 上游 401 team_model_access_denied = "这个 team 的白名单里没有该模型"：
# 是模型权限事实，不是账号健康信号——_penalize_account 对它一律不冷却（冷却会把
# 本来好的账号一起锁掉，0829/0830 两次整池 503 即由此而来）。0929 起各订阅账号的
# team 权限已分化（实测 <账号ID> 无 DeepSeek-V4.1-Flash 权限、<账号ID> 有，命中前者时
# 整单 401 直接失败），故改判 failover=True 换下一账号再试，白打上游由
# MAX_ACCOUNT_ATTEMPTS=3 兜底。
MODEL_ACCESS_DENIED = "team_model_access_denied"
# 上游 429 = litellm model_group 级滑窗 TPM 限流（换账号无益）：按 Retry-After
# （缺省 60s，恰为一个滑窗）等待后原样重发，单请求总预算 30 分钟。
UPSTREAM_429_BUDGET_S = 1800
UPSTREAM_429_DEFAULT_WAIT_S = 60.0
UPSTREAM_429_MAX_WAIT_S = 120.0
# 0929：外部 provider 全 key 配额耗尽（429 quota exceeded）的 provider 级冷却。
# 背景：intern 通道 10 把 key 的配额被一晚编码会话打穿后，客户端每 ~100s 重试
# 一次，中转每笔都串行打满全部 key（整夜 ~3700 笔 × 10 key = 3.7 万次无效上游
# 请求，还灌满了 50MB 日志）。冷却窗内对客户端快速失败（503 + Retry-After），
# 不再打上游；任何一把 key 成功即解除冷却。
PROVIDER_QUOTA_COOLDOWN_S = 900.0
_prov_quota_cooldown = {}   # provider name -> until_ts


class _UpstreamOpen:
    """一次上游请求的判定结果（流式开闸 / 非流式响应共用）。

    ok=True 时持有 resp/client，交由吐流阶段（_stream_body）关闭；失败时本对象
    已自行关干净，只留状态码与错误摘要供选号循环决策。
    """

    def __init__(self, *, status: int = 0, resp=None, client=None,
                 snippet: str = "", retry_after: float | None = None,
                 error: str | None = None):
        self.status = status
        self.resp = resp
        self.client = client
        self.snippet = snippet
        self.retry_after = retry_after
        self.error = error

    @property
    def ok(self) -> bool:
        return self.resp is not None

    @property
    def access_denied(self) -> bool:
        """上游明说"该 team 无权访问此模型"：换号无益，也不该记成账号故障。"""
        return MODEL_ACCESS_DENIED in (self.snippet or "")

    @property
    def failover(self) -> bool:
        """是否值得换下一个账号重试（连接层异常也算：可能是单账号侧问题）。

        0929 修复：team_model_access_denied 也参与换号。原假设"所有账号同队
        同权限、换号必然同样失败"已过时——上游把 team 收成 alias-only 后各订阅
        账号权限分化（实测 <账号ID> 无 DeepSeek-V4.1-Flash 权限、<账号ID> 有，命中
        前者时整单 401 直接失败）。access_denied 仍不冷却账号（_penalize_account
        已豁免），仅改为换下一账号再试，白打上游由 MAX_ACCOUNT_ATTEMPTS 兜底。
        """
        return self.error is not None or self.status in FAILOVER_UPSTREAM_STATUSES

    def close(self):
        for res in (self.resp, self.client):
            if res is None:
                continue
            try:
                res.close()
            except Exception:
                pass


def _parse_retry_after(raw) -> float | None:
    """Retry-After 头 → 秒数（上限 120s）；缺失或非法返回 None。"""
    if raw is None:
        return None
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(v, UPSTREAM_429_MAX_WAIT_S))


def _stream_open(data: dict, model: str, headers: dict) -> _UpstreamOpen:
    """建立上游流式连接并确认状态码，但尚未向客户端输出任何字节。

    这一步刻意放在返回 Response 之前：状态码还没定，失败还能换号或如实报错。
    429 在预算内等待重发（换账号无益），预算耗尽才带着 Retry-After 放弃。
    """
    deadline = time.time() + UPSTREAM_429_BUDGET_S
    attempt = 0
    while True:
        attempt += 1
        client = httpx.Client()
        try:
            req = client.build_request(
                "POST", f"{LITELLM_API_BASE}/v1/chat/completions",
                json=data, headers=headers,
                # 流式专用超时：连接 15s，chunk 间最多静默 120s
                #（上游卡死自动断开，不再无限挂），总时长不限（长生成合法）
                timeout=httpx.Timeout(connect=15, read=120, write=180, pool=15))
            resp = client.send(req, stream=True)
        except Exception as e:
            client.close()
            log.error("上游流式连接失败（账号请求 %s）: %s", model, e)
            return _UpstreamOpen(error=f"connect failed: {e}")

        if resp.status_code == 200:
            return _UpstreamOpen(status=200, resp=resp, client=client)

        snippet = ""
        try:
            snippet = resp.read()[:800].decode("utf-8", errors="replace")
        except Exception:
            pass
        retry_after = _parse_retry_after(resp.headers.get("retry-after"))
        status = resp.status_code
        resp.close()
        client.close()

        if status == 429 and time.time() < deadline:
            wait = retry_after if retry_after is not None else UPSTREAM_429_DEFAULT_WAIT_S
            log.warning("上游流式 429（第 %d 次，等待 %ss 后重发 %s，剩余预算 %.0fs）",
                        attempt, wait, model, deadline - time.time())
            time.sleep(wait)
            continue
        log.error("上游流式返回 %s: %s", status, snippet)
        return _UpstreamOpen(status=status, snippet=snippet, retry_after=retry_after)


def _stream_body(opened: _UpstreamOpen, model: str, uid: str, started: float,
                 sess=None, key_label: str | None = None,
                 load_key: str | None = None):
    """把已确认 200 的上游流吐给客户端，边吐边提取 usage 与上游实际模型名。

    首字节已出 → HTTP 状态码改不了，中途失败只能用 in-band SSE 错误事件收尾。
    uid 是记账/画像/面板用的身份（主池=账号，外部通道=provider 或 key 标签）；
    load_key 是并发负载登记用的键（外部通道带前缀，与 pick_key 的查询键一致）。
    """
    usage_captured = None
    line_buf = ""
    model_returned = None
    ttft_ms = None
    saw_done = False
    rid = registry.register(model, load_key or uid, stream=True)
    try:
        for chunk in opened.resp.iter_bytes():
            registry.touch(rid, len(chunk))
            if ttft_ms is None:
                ttft_ms = int((time.perf_counter() - started) * 1000)
            line_buf += chunk.decode("utf-8", errors="ignore")
            while "\n" in line_buf:
                line, line_buf = line_buf.split("\n", 1)
                if line.strip() == "data: [DONE]":
                    saw_done = True
                u = _extract_usage_from_line(line)
                if u:
                    usage_captured = u
                m = _extract_model_from_line(line)
                if m and not model_returned:
                    model_returned = m
            yield chunk
        if line_buf:
            u = _extract_usage_from_line(line_buf)
            if u:
                usage_captured = u
        if not saw_done:
            # 上游没吐终止符就断了 socket：httpx 只看到正常 EOF，只能靠契约判断
            log.warning("上游流未以 [DONE] 收尾（账号 %s，%s），回复可能被截断", uid, model)
            activity.add("warn", f"{model} · 账号 {uid} 流未以 [DONE] 收尾，疑似截断",
                         model=model, user_id=uid, status="截断")
    except Exception as e:
        log.error("流式中断（账号 %s）: %s", uid, e)
        latency_ms = int((time.perf_counter() - started) * 1000)
        activity.add("error", f"{model} · 账号 {uid} 流式中断: {e}", model=model,
                     user_id=uid, latency_ms=latency_ms, tokens=0, status="流中断")
        yield (f'data: {json.dumps({"error": {"message": f"upstream stream interrupted: {e}", "type": "proxy_stream_error"}}, ensure_ascii=False)}\n\n').encode()
        yield b"data: [DONE]\n\n"
        return
    finally:
        registry.finish(rid, "done")
        opened.close()
        if sess is not None:
            session_pool.release(sess)

    latency_ms = int((time.perf_counter() - started) * 1000)
    if usage_captured:
        token_stats.record(uid, model, usage_captured)
        if key_label:
            # 流式生成器已在请求上下文之外运行，label 由调用方提前捕获传入
            token_stats.record_key(key_label, model, usage_captured)
    # 知识库记录（0905）：流式成功
    kdb.record_request(
        user_id=uid, model_requested=model,
        model_served=model_returned, status_code=200,
        prompt_tokens=(usage_captured or {}).get("prompt_tokens", 0),
        completion_tokens=(usage_captured or {}).get("completion_tokens", 0),
        total_tokens=(usage_captured or {}).get("total_tokens", 0),
        duration_ms=latency_ms, is_stream=True,
    )
    # 注水检测：核对上游实际返回的模型名 + 记录性能画像
    water.observe(uid, model, model_returned, usage_captured,
                  ttft_ms=ttft_ms, total_ms=latency_ms)
    activity.add("ok", f"{model} · 账号 {uid}（流式）", model=model, user_id=uid,
                 latency_ms=latency_ms,
                 tokens=(usage_captured or {}).get("total_tokens", 0), status=200)


def _openai_error(message: str, err_type: str, code: str, status: int = 503,
                  retry_after: float | None = None):
    """OpenAI 兼容的错误响应（error 必须是对象，字符串形式会让严格客户端解析失败）。"""
    resp = jsonify({"error": {"message": message, "type": err_type,
                               "param": None, "code": code}})
    resp.status_code = status
    if retry_after:
        resp.headers["Retry-After"] = str(int(retry_after) + 1)
    return resp


def _upstream_fail_response(opened: _UpstreamOpen, model: str, uid: str,
                            started: float):
    """上游最终失败且未向客户端输出任何字节：如实回真实状态码。

    上游错误原文只进日志不回客户端——里面带 team id 与实际消费等内部信息。
    """
    latency_ms = int((time.perf_counter() - started) * 1000)
    status = opened.status or 502
    log.error("流式转发最终失败 %s（账号 %s，%dms）: %s", status, uid, latency_ms,
              (opened.snippet or opened.error or "")[:300])
    activity.add("error", f"{model} · 账号 {uid} 上游返回 {status}", model=model,
                 user_id=uid, latency_ms=latency_ms, tokens=0, status=status)
    retry_after = opened.retry_after
    if not retry_after and status in (401, 402, 403, 429):
        retry_after = am.next_recovery_in()
    return _openai_error(
        f"upstream returned {status} for model '{model}' and no account could serve it",
        "proxy_upstream_error", f"upstream_{status}", status, retry_after)


_TEAM_RE = re.compile(r"Team=([0-9a-zA-Z-]{6,})")


def _team_from_snippet(snippet: str) -> str | None:
    """上游 402 错误体里的 LiteLLM team id —— 预算实际挂在这个 team 上。"""
    m = _TEAM_RE.search(snippet or "")
    return m.group(1) if m else None


def _penalize_account(uid: str, opened: _UpstreamOpen, model: str):
    """账号维度失败 → 放进冷却窗口，窗口内选号直接跳过（到点自愈，不用人工点启用）。

    402 的预算挂在 team 上而不是账号上：拿到 team 就把同队账号一并冷却，省掉
    换号后注定重复的那几次上游请求。401/403（凭证失效、签名被安全门拒绝）窗口
    取得更长——上游正在排查反代，"每隔几十秒撞一次安全门"是最刺眼的请求模式，
    冷却把它压成窗口到期时才探一次。
    例外：team_model_access_denied 不冷却任何账号，那是模型权限而非账号故障。
    """
    # 知识库记录（0905）：错误事件
    kdb.record_error(uid, model, opened.status, "upstream_error",
                     (opened.snippet or "")[:200])
    if opened.status == 402:
        team = _team_from_snippet(opened.snippet)
        # 0905：改用渐进冷却（600s 反复撞 402 → 30min/90min/5h 阶梯，
        # 吸收 ProxyDeck BudgetResetAt 的"等窗口重置、别白烧上游"思路）
        secs, cooled = am.mark_budget_402(uid, team=team)
        activity.add("error",
                     f"{model} · 账号 {uid} 配额用尽，渐进冷却 {secs}s"
                     + (f"（同队共 {len(cooled)} 个账号一并冷却）" if len(cooled) > 1 else ""),
                     model=model, user_id=uid, tokens=0, status=402)
    elif opened.status in (401, 403):
        body = opened.snippet or ""
        if opened.access_denied:
            # 白名单外模型：账号没坏，冷却它只会把可用池越缩越小直至整池 503
            log.warning("账号 %s 无权访问 %s（不在其 team 白名单），不冷却", uid, model)
            activity.add("error", f"{model} · 账号 {uid} 所在 team 无权访问该模型",
                         model=model, user_id=uid, tokens=0, status=opened.status)
            return
        # 上游安全门（要求升级客户端 / 拒签名）：换 key 也没用，长窗口避免重复撞门
        gated = ("codely-signature" in body) or ("升级" in body) or ("upgrade" in body.lower())
        if not gated:
            # 更可能只是缓存的 cli_api_key 失效：剔除后下次探测会重新换取，
            # 冷却窗口也放短，别把本来能用的账号无故关掉
            api_cache.cache.pop(uid, None)
        secs = AUTH_COOLDOWN_S if gated else QUOTA_COOLDOWN_S
        am.mark_cooldown(uid, secs, "上游安全门拒绝签名" if gated else "上游拒绝凭证")
        activity.add("error",
                     f"{model} · 账号 {uid} 被上游拒绝（{opened.status}），冷却 {secs}s",
                     model=model, user_id=uid, tokens=0, status=opened.status)


def _pool_exhausted(model: str):
    """账号池暂无可用账号（全部冷却或禁用）→ 503 + Retry-After 让客户端延后重试。"""
    kdb.record_error("-", model, 503, "pool_exhausted",
                     "no available account (all cooling/disabled)")
    left = am.next_recovery_in()
    if left:
        log.warning("账号池全部处于冷却中（%s 分钟后恢复），拒绝 %s", round(left / 60, 1), model)
        return _openai_error(
            f"all upstream accounts for model '{model}' are quota-cooling; retry in {int(left)}s",
            "rate_limit_error", "account_pool_cooling_down", 503, left)
    return _openai_error(f"no available upstream account for model '{model}'",
                         "service_unavailable", "no_available_account", 503)


def _proxy_chat(data: dict, upstream_path: str = "/v1/chat/completions", transform=None):
    """多账号轮询转发 chat 请求的公共逻辑。

    transform: 可选，把上游 chat 响应 JSON 转换成其它响应格式（Responses API 用）。
    """
    model = data.get("model", "")
    started = time.perf_counter()
    # 角色别名解析（commander/coder/reviewer → 真实模型），先于视觉/经济路由
    role_note = resolve_role(data)
    if role_note:
        model = data.get("model", "")
        log.info("角色路由: %s", role_note)
        activity.add("info", f"角色路由 {role_note}", model=model)
    reroute_note = _reroute_if_media(data)
    if reroute_note:
        model = data.get("model", "")
        log.info("视觉改路由: %s", reroute_note)
    eco_note = economic_reroute(data)
    if eco_note:
        model = data.get("model", "")
        log.info("经济路由: %s", eco_note)
        activity.add("info", f"经济路由 {eco_note}", model=model)
    # effort 守卫（0905）：实测矩阵拦截 400 组合（minimax-m3 拒 off 等）。
    # 放在外部通道匹配之前——smtell/agnes 等外部通道的模型也要守卫，
    # 否则 minimax-m3（smtell）的 effort=off 会直接打到上游 400。
    _effort_guard(data, model)

    # 外部 provider 模型：直接转发，不走主账号池
    # 视觉回落链（0905）：reroute 到视觉模型失败时按链降级（502/503 换下一个）
    prov = providers.match(model)
    if prov:
        if reroute_note:
            # 视觉 reroute 的外部模型：失败时按链降级
            for vm in _get_vision_chain():
                vp = providers.match(vm)
                if not vp:
                    # 池模型（codely-vl）：跳出循环走下面的账号池路径
                    data["model"] = vm
                    model = vm
                    log.info("视觉回落: 切到池模型 %s", vm)
                    break
                data["model"] = vm
                resp = _forward_external(data, vm, vp)
                # 统一取状态码（tuple 形态和 Response 形态都覆盖）
                st = None
                if isinstance(resp, tuple) and len(resp) == 2 and isinstance(resp[1], int):
                    st = resp[1]
                else:
                    st = getattr(resp, "status_code", None)
                if st in (429, 500, 502, 503):
                    log.warning("视觉回落: %s 失败(%d)，尝试下一个", vm, st)
                    continue
                return resp
            # 所有外部视觉 provider 都失败 → 回落 codely-vl（账号池路径）
            data["model"] = "codely-vl"
            model = "codely-vl"
            log.warning("视觉回落: 外部视觉模型全挂，降级到 codely-vl（账号池）")
            # 不 return，让它走下面的账号池路径
        else:
            return _forward_external(data, model, prov)

    # ── System Prompt 守卫 ──────────────────────────────────────
    # 上游会检测 system prompt 内容，非官方 CLI 的系统提示词会触发 400。
    # qidicode 等第三方客户端自带 "You are QIDI Code" 之类的系统提示词，
    # 转发前替换为通用提示词，避免被上游识别。
    msgs = data.get("messages")
    if isinstance(msgs, list):
        for m in msgs:
            if isinstance(m, dict) and m.get("role") == "system":
                orig_head = (m.get("content") or "")[:60]
                m["content"] = (
                    "You are Codely CLI, an interactive CLI tool that helps "
                    "users with software engineering tasks. Follow the user's "
                    "instructions carefully and provide accurate, helpful responses."
                )
                log.info("System prompt 守卫: 替换非官方提示词 (原前60字: %r)", orig_head)
                activity.add("info", "System prompt 守卫: 已替换", model=model, status="rewrite")
                break

    # ── 模型名守卫（0825）──────────────────────────────────────────
    # 官方 codely 客户端只会请求团队白名单内的模型（GLM-5.3 / KIMI-K3）。
    # 任何其它模型名（如 grok-build、grok-4.20-multi-agent）一旦原样转发
    # 到上游，等于明示"这不是官方客户端"。这里把未知模型名一律改写为
    # 默认池模型，从源头杜绝模型名泄漏。外部 provider（agnes/atomcode 等）
    # 已在上面 providers.match 命中返回，不受影响。
    # 0906: 守卫白名单制（审计整改版）。单次读盘 _cfg 全程复用（消除 TOCTOU 与
    # 每请求 3-4 次磁盘读）。放行 = 池模型 ∪ providers.models ∪ roles 别名
    # （大小写不敏感判定）；白名单外一律 404——死模型死得响亮，绝不静默
    # 改写假活（fable 盲审调包事故：27 次盲审被 codely-core 代答）。
    _cfg_g = load_config()
    pool_models = _cfg_g.get("pool_models") or ["GLM-5.3", "KIMI-K3"]
    _str = lambda xs: {x for x in xs if isinstance(x, str)}  # 防 config 手误非 str
    _provider_models = set()
    for _pname, _p in (_cfg_g.get("providers") or {}).items():
        if isinstance(_p, dict):
            _provider_models |= _str(_p.get("models") or [])
            # 0928: model_aliases 别名键一并入白名单（放行守卫）；
            # 别名键带前缀，不会与池模型大小写不敏感撞名，
            # 故不引入变体规范化劫持。
            _pa = _p.get("model_aliases")
            if isinstance(_pa, dict):
                _provider_models |= {k for k in _pa if isinstance(k, str) and k.strip()}
    _roles = get_role_map()
    _allowed = _str(pool_models) | _provider_models
    for _rn, _rt in _roles.items():
        if isinstance(_rn, str) and isinstance(_rt, str):
            _allowed.add(_rn)
            _allowed.add(_rt)
    # 0906: OpenClaw 等客户端的内置目录模型名（不在中转名单）——config 的
    # guard_rewrite_aliases 显式收编：放行守卫并落入改写 codely-core 分支，
    # 避免被 404 拒之门外（agent 框架的默认模型名基本固定，收编一次一劳永逸）
    _allowed |= _str(_cfg_g.get('guard_rewrite_aliases') or [])
    _allowed_lower = {x.lower() for x in _allowed}
    if model not in _allowed and model.lower() not in _allowed_lower:
        log.warning("模型名守卫: 未知模型 %r 拒绝服务（404，防止死模型假活/特征暴露）", model)
        activity.add("warn", f"模型名守卫: 未知模型 {model} → 404", model=model, status="reject")
        return jsonify({"error": {
            "message": f"The model '{model}' does not exist on this relay. "
                       "Available models: see GET /v1/models.",
            "type": "invalid_request_error",
            "param": "model",
            "code": "model_not_found",
        }}), 404
    # 0906 整改（审计 B-1）：provider 模型大小写变体——providers.match 精确
    # 匹配，变体名会 miss 而落到守卫。规范化为 canonical 名并重试转发；仍
    # miss 则 fallthrough 到改写分支（绝不以变体原名落入官方账号池）。
    if model.lower() in {x.lower() for x in _provider_models} and model not in _provider_models:
        _canonical = next(x for x in _provider_models if x.lower() == model.lower())
        log.info("模型名守卫: provider 模型规范化 %r → %r（match 精确匹配）", model, _canonical)
        data["model"] = _canonical
        model = _canonical
        _prov = providers.match(model)
        if _prov:
            return _forward_external(data, model, _prov)
        # canonical 仍 miss（providers 配置与 models 列表短暂不一致）：
        # fallthrough 到下方改写分支，绝不原样落池
        log.warning("模型名守卫: %r canonical 后仍无 provider 通道，转改写分支", model)
    if model not in pool_models and model not in _provider_models:
        # 0929 修复（池模型大小写规范化）：上方白名单判定是大小写不敏感的，但本行
        # 池判断是精确匹配——小写变体（如 deepseek-v4.1-flash，与 qidicode 云端
        # slug 同名）会漏到这里被静默改写成 codely-core，客户端拿到 200 但答话的
        # 是 GLM（静默假活，违反 0906 审计"绝不静默改写假活"原则）。与 635 行
        # provider 模型规范化同思路：池模型变体先归一到 canonical 名再进池。
        _pool_canon = next((x for x in pool_models
                            if isinstance(x, str) and x.lower() == model.lower()), None)
        if _pool_canon:
            log.info("模型名守卫: 池模型大小写规范化 %r → %r（进池不改写）",
                     model, _pool_canon)
            activity.add("info", f"模型名规范化: {model}→{_pool_canon}",
                         model=_pool_canon, status="rewrite")
            data["model"] = model = _pool_canon
        else:
            log.warning("模型名守卫: 非池非 provider 模型 %r 改写为 codely-core（防止上游特征暴露）", model)
            activity.add("warn", f"模型名守卫: {model}→codely-core", model=model, status="rewrite")
            data["model"] = "codely-core"
            model = "codely-core"
            # 改写后重新解析角色别名与外部通道（codely 池已停用，
            # GLM-5.3/KIMI-K3 现在都映射到 agnes/atomcode）
            role_note = resolve_role(data)
            if role_note:
                model = data.get("model", "")
                log.info("守卫改写后角色路由: %s", role_note)
            prov = providers.match(model)
            if prov:
                return _forward_external(data, model, prov)

    # ── GLM-5.3 直连改写（0903）─────────────────────────────────
    # 上游已收回全部团队的 GLM-5.3 直连权限（team_model_access_denied 401），
    # 现在只有 codely-core（后端即 glm-5.3）可达。请求 GLM-5.3 的客户端
    # 统一改写为 codely-core 转发，无感知恢复服务。
    if model == "GLM-5.3":
        log.info("模型改写: GLM-5.3 → codely-core（上游已收回直连权限）")
        activity.add("info", "GLM-5.3→codely-core（直连被收回）", model=model, status="rewrite")
        data["model"] = "codely-core"
        model = "codely-core"

    is_stream = data.get("stream", False)
    # ── thinking 模型 tool_choice 守卫（0831）─────────────────────
    # 上游对 thinking 模型会把未指定/对象形式的 tool_choice 处理成
    # 'specified'，与思考模式互斥 → 400。显式补 "auto" 规避。
    if model in get_thinking_models() and isinstance(data.get("tools"), list) and data.get("tools"):
        tc = data.get("tool_choice")
        if not isinstance(tc, str) or tc not in ("auto", "none"):
            data["tool_choice"] = "auto"
            log.info("tool_choice 守卫: %s thinking 模型 %s → auto", model, repr(tc)[:40])
    # 分组路由可视化：模型绑定了分组时打印将走的账号组
    mg = load_config().get("model_groups") or {}
    bound_group = mg.get(model) or mg.get(str(model).lower())
    if bound_group:
        members = [a["user_id"] for a in am.accounts
                   if (a.get("group") or "") == bound_group and a.get("enabled", True)]
        log.info("[分组路由] %s 绑定 %s 组，可用成员: %s",
                 model, bound_group, members or "（组内无启用账号，将回退全池）")

    # 尝试次数 = min(账号数, 上限)：整池同类错误时不会白打无限次上游
    max_attempts = min(len(am.accounts), MAX_ACCOUNT_ATTEMPTS)
    # 流式生成器运行在请求上下文之外，label 必须提前捕获（按 key 记账用）
    key_label = getattr(g, "api_key_label", None) if has_request_context() else None

    last_open = None
    last_uid = "?"
    for _attempt in range(max_attempts):
        # 负载每轮重算：上一轮换号可能已让某账号多出一条长流
        loads = {a["user_id"]: registry.load_of(a["user_id"]) for a in am.accounts}
        # 0929 补丁：上一轮刚失败的账号强加一个"虚拟负载"，最少负载选号天然避开
        # 它——修掉同请求内换号又撞回同一无权账号的浪费（实测 <账号ID> 连撞两次才
        # 命中 <账号ID>）；全池只剩它时兜底仍会选它，不影响可用性。
        if last_uid != "?":
            loads[last_uid] = (max(loads.values()) if loads else 0) + 1
        acc = am.get_next(model, loads=loads)
        if not acc:
            # 全部冷却/禁用：有上游失败记录就报那个状态码，否则 503 + Retry-After
            if last_open is not None:
                return _upstream_fail_response(last_open, model, last_uid, started)
            return _pool_exhausted(model)

        uid = acc["user_id"]
        key = api_cache.get(acc)
        if not key:
            return _openai_error("获取 API Key 失败", "proxy_config_error",
                                 "api_key_unavailable")

        sess = session_pool.acquire(key)
        headers = build_litellm_headers(key, session=sess)

        if is_stream:
            opened = _stream_open(data, model, headers)
            if opened.ok:
                return Response(_stream_body(opened, model, uid, started, sess, key_label),
                                content_type="text/event-stream")
            session_pool.release(sess)
            last_open, last_uid = opened, uid
            _penalize_account(uid, opened, model)
            if not opened.failover:
                return _upstream_fail_response(opened, model, uid, started)
            log.warning("流式账号 %s 失败（%s），换下一账号", uid,
                        opened.status or opened.error)
            continue

        try:
            with httpx.Client() as client:
                r = client.post(f"{LITELLM_API_BASE}/v1/chat/completions",
                                json=data, headers=headers, timeout=300)
                latency_ms = int((time.perf_counter() - started) * 1000)

                # 429 兜底：litellm 滑窗 TPM 限流是 model_group 级（换账号无益），
                # 按 Retry-After（缺省 60s = 一个滑窗）等待后原样重发；30 分钟预算。
                if r.status_code == 429:
                    _deadline = time.time() + UPSTREAM_429_BUDGET_S
                    while r.status_code == 429 and time.time() < _deadline:
                        ra = r.headers.get("retry-after") or "60"
                        try:
                            wait = min(float(ra), 120)
                        except ValueError:
                            wait = 60
                        log.warning("上游非流式 429，按 Retry-After=%ss 等待重发 %s（剩余预算 %.0fs）",
                                    wait, model, _deadline - time.time())
                        time.sleep(wait)
                        r = client.post(f"{LITELLM_API_BASE}/v1/chat/completions",
                                        json=data, headers=headers, timeout=300)
                    latency_ms = int((time.perf_counter() - started) * 1000)

                # 402 配额不足 / 401·403 凭证被拒：账号进冷却窗口，换下一个
                if r.status_code in (401, 402, 403):
                    opened = _UpstreamOpen(status=r.status_code,
                                           snippet=(r.text or "")[:800],
                                           retry_after=_parse_retry_after(
                                               r.headers.get("retry-after")))
                    _penalize_account(uid, opened, model)
                    last_open, last_uid = opened, uid
                    session_pool.release(sess)
                    # 与流式同一条 failover 判定（0929：access_denied 也换号再试）
                    if not opened.failover:
                        return _upstream_fail_response(opened, model, uid, started)
                    continue

                # 上游非 2xx：如实透传
                if r.status_code != 200:
                    log.error("上游返回 %s: %s", r.status_code, r.text[:800])
                    activity.add("error", f"{model} · 账号 {uid} 上游返回 {r.status_code}",
                                 model=model, user_id=uid, latency_ms=latency_ms, tokens=0, status=r.status_code)
                    session_pool.release(sess)
                    return Response(r.content, status=r.status_code,
                                    content_type=r.headers.get("content-type", "application/json"))

                resp_json = r.json()
                usage = resp_json.get("usage")
                if usage:
                    token_stats.record(uid, model, usage)
                    _record_key_usage(model, usage)
                # 知识库记录（0905）：非流式池成功
                kdb.record_request(
                    user_id=uid, model_requested=model,
                    model_served=resp_json.get("model", model), status_code=200,
                    prompt_tokens=(usage or {}).get("prompt_tokens", 0),
                    completion_tokens=(usage or {}).get("completion_tokens", 0),
                    total_tokens=(usage or {}).get("total_tokens", 0),
                    duration_ms=latency_ms, is_stream=False,
                )
                # 注水检测：核对上游实际返回的模型名 + 记录性能画像
                water.observe(uid, model, resp_json.get("model"), usage,
                              ttft_ms=latency_ms, total_ms=latency_ms)
                activity.add("ok", f"{model} · 账号 {uid}",
                             model=model, user_id=uid, latency_ms=latency_ms,
                             tokens=(usage or {}).get("total_tokens", 0), status=200)
                session_pool.release(sess)
                return jsonify(transform(resp_json, model) if transform else resp_json)
        except Exception as e:
            log.error("账号 %s 请求失败: %s", acc.get("user_id", "?"), e)
            activity.add("error", f"{model} · 账号 {acc.get('user_id', '?')} 异常: {e}",
                         model=model, user_id=acc.get("user_id", "?"),
                         latency_ms=int((time.perf_counter() - started) * 1000),
                         tokens=0, status="异常")
            session_pool.release(sess)
            continue

    if last_open is not None:
        return _upstream_fail_response(last_open, model, last_uid, started)
    if bound_group:
        return _openai_error(
            f"no available account for model '{model}' (bound group '{bound_group}' "
            f"has no usable member)", "service_unavailable",
            "account_group_exhausted", 503, am.next_recovery_in())
    return _pool_exhausted(model)


def _flask_response(value):
    """把 _proxy_chat 的返回值统一成 Response。

    _proxy_chat 直接返回 Flask 视图值：多数分支是 Response，但守卫/池耗尽/
    上游失败透传等分支返回 (Response, status) 元组。调用方若按 Response 用
    （取 get_json / 读 content_type / 迭代 response），元组会抛
    AttributeError: 'tuple' object has no attribute ... → 500 或流式中途断链，
    客户端看到的是 HTML 错误页而不是守卫的 404。
    """
    if isinstance(value, tuple):
        resp = value[0]
        status = value[1] if len(value) > 1 else None
        if status is not None:
            resp.status_code = int(status)
        return resp
    return value


def _detect_media_type(data: dict) -> str | None:
    """检测 messages 中是否含非文本内容块（image_url / input_audio / file 等）。

    GLM-5.3 等纯文本模型收到图片会被上游 400 拒绝
    （"***.***.type 参数非法，取值范围 ['text']"），导致客户端无限重试。
    """
    for msg in data.get("messages", []):
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, dict) and part.get("type") not in (None, "text"):
                return part.get("type")
    return None


# ── delegate 模式：媒体落盘 + 路径注释 ─────────────────────────────────────

def _save_data_url_media(url) -> str | None:
    """把 data: 图片 URL 解码保存到媒体目录，返回绝对路径。

    非图片类型、非 data: URL、超限（>20MB）或写盘失败时返回 None。
    文件名按内容 sha1 去重：同一张图在多轮请求里重复出现只落盘一次。
    """
    if not isinstance(url, str) or not url.startswith("data:"):
        return None
    try:
        import base64 as _b64
        import hashlib as _hash
        import os as _os
        header, b64data = url.split(",", 1)
        mime = header[5:].split(";", 1)[0].strip().lower()
        if not mime.startswith("image/"):
            return None
        # 先检查编码长度再解码（避免 100MB data URL 打爆内存）
        if len(b64data) > 28 * 1024 * 1024:  # 20MB raw × 4/3
            log.warning("媒体落盘: data URL 超过 20MB 限制（编码 %d 字节），跳过", len(b64data))
            return None
        raw = _b64.b64decode(b64data)
        ext = {"image/jpeg": ".jpg", "image/jpg": ".jpg", "image/webp": ".webp",
               "image/gif": ".gif", "image/bmp": ".bmp"}.get(mime, ".png")
        name = _hash.sha1(raw).hexdigest()[:20] + ext
        media_dir = load_config().get("vision_media_dir") or _os.path.join(
            _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "media")
        _os.makedirs(media_dir, exist_ok=True)
        path = _os.path.join(media_dir, name)
        if not _os.path.exists(path):
            with open(path, "wb") as f:
                f.write(raw)
        return _os.path.abspath(path)
    except Exception:
        return None


def _delegate_msg_media(msg) -> int:
    """delegate 模式：把单条消息里的媒体 part 替换为路径注释（就地修改）。

    返回替换数量。图片（data: URL）会先落盘；其他媒体类型只留丢弃注释。
    """
    if not isinstance(msg, dict):
        return 0
    content = msg.get("content")
    if not isinstance(content, list):
        return 0
    replaced = 0
    for idx, p in enumerate(content):
        if not (isinstance(p, dict) and p.get("type") not in (None, "text")):
            continue
        note = "[媒体内容已丢弃：该类型不支持落盘]"
        if p.get("type") == "image_url":
            url = p.get("image_url") if isinstance(p.get("image_url"), dict) else None
            path = _save_data_url_media((url or {}).get("url"))
            if path:
                chain = _get_vision_chain()
                chain_str = " → ".join(chain)
                note = (f"[图片已保存至磁盘: {path}｜本模型不支持视觉。"
                        f"如需图片内容请派视觉子代理读取该文件并转述（"
                        f"视觉模型优先级: {chain_str}；主选挂掉按序降级，"
                        f"或直接用 model={chain[0]} 的 general-purpose 子代理）]")
        content[idx] = {"type": "text", "text": note}
        replaced += 1
    return replaced


def _get_vision_chain():
    """视觉模型回落链（0905 吸收 ProxyDeck VisionFallbackChain）。

    当前 vision_model → config vision_fallback 数组 → codely-vl 兜尾。
    去重保序。agnes 挂了自动降级到 codely-vl（团结官方视觉模型，走账号池
    独立于 agnes），codely-vl 也冷却时 agnes-2.5-pro-beta（另一路 provider key）。
    """
    chain = [get_vision_model()]
    vf = load_config().get("vision_fallback") or []
    chain.extend(vf)
    chain.append("codely-vl")  # 团结官方视觉模型永远兜尾
    out = []
    for m in chain:
        if m and m.lower() not in [x.lower() for x in out]:
            out.append(m)
    return out


def _effort_guard(data: dict, model: str):
    """effort 守卫（0905）：按实测矩阵拦截会 400 的 reasoning_effort 组合。

    minimax-m3 拒 off（0905 实测 400），其余模型全部接受。矩阵存在
    config.json effort_matrix；模型不在矩阵中的放行（不误伤）。
    """
    effort = data.get("reasoning_effort")
    if not effort:
        return
    matrix = load_config().get("effort_matrix") or {}
    row = matrix.get(model) or matrix.get(model.lower())
    if row is None:
        return  # 未测模型：放行
    if not row.get(str(effort).lower(), True):
        # 已知拒绝：替换为该模型接受的最低档位
        for fallback_e in ["low", "medium", "high", "max"]:
            if row.get(fallback_e):
                log.info("effort 守卫: %s 拒 reasoning_effort=%s → %s（0905 实测矩阵）",
                         model, effort, fallback_e)
                data["reasoning_effort"] = fallback_e
                return
        # 全拒（模型不可用）：删除参数放行（让上游如实报错）
        del data["reasoning_effort"]
        log.warning("effort 守卫: %s 无可接受档位，删除 reasoning_effort", model)


# 历史媒体占位符（vision_policy=latest_turn 时替换旧图片用）
MEDIA_PLACEHOLDER = "[媒体内容已省略：该图/文件已在前轮由视觉模型处理，分析结论见其后的回复]"


def _msg_media_type(msg) -> str | None:
    """单条消息中第一个非文本内容块的 type（无则 None）。"""
    if not isinstance(msg, dict):
        return None
    content = msg.get("content")
    if not isinstance(content, list):
        return None
    for part in content:
        if isinstance(part, dict) and part.get("type") not in (None, "text"):
            return part.get("type")
    return None


def _strip_msg_media(msg) -> bool:
    """把单条消息 content 里的非文本 part 替换为占位文本（就地修改）。

    返回是否发生了替换。
    """
    if not _msg_media_type(msg):
        return False
    msg["content"] = [
        {"type": "text", "text": MEDIA_PLACEHOLDER}
        if isinstance(p, dict) and p.get("type") not in (None, "text")
        else p
        for p in msg["content"]
    ]
    return True


def _reroute_if_media(data: dict) -> str | None:
    """含媒体内容且目标模型不支持视觉时，把 model 改写为视觉模型。

    vision_policy（config.json，默认 "reroute_all" 保持旧行为）：
    - "reroute_all"：历史里出现任何媒体即整包改路由（旧行为——对话里
      一旦贴过图，后续所有轮次都会被视觉模型接管）。
    - "latest_turn"：仅当"当前轮"（最后一条 assistant 消息之后新增的
      user/tool 内容）含媒体时才改路由；更早历史里的旧图片替换为文字
      占位符。效果：视觉模型只在贴新图那一轮看图，其余轮次由请求的
      模型正常推理，并省掉每轮重传 base64 大图的 token。

    返回改写说明（未改写返回 None），供日志与动态面板展示。
    """
    model = data.get("model", "")
    if not model or model.lower() in {m.lower() for m in get_vision_capable_models()}:
        return None
    policy = str(load_config().get("vision_policy") or "reroute_all").strip().lower()
    msgs = data.get("messages")
    if policy == "delegate" and isinstance(msgs, list):
        # 终极方案（0905）：媒体一律落盘并替换为"路径注释"，主模型永不改路由。
        # 需要图片内容时由主模型派 vision 子代理（视觉白名单模型）按路径查看，
        # 文字描述回传后沉淀在会话历史里（每张图只需看一次）。
        n = sum(_delegate_msg_media(m) for m in msgs)
        if n:
            log.info("视觉代理（delegate）: %s（%d 个媒体已落盘并替换为路径注释，未改路由）",
                     model, n)
            kdb.record_request(
                user_id="delegate", model_requested=model, model_served=model,
                status_code=200, has_media=True, is_stream=False,
                reroute_note="delegate:%d_media" % n,
            )
        return None
    if policy == "latest_turn" and isinstance(msgs, list):
        last_asst = -1
        for i, m in enumerate(msgs):
            if isinstance(m, dict) and m.get("role") == "assistant":
                last_asst = i
        stripped = sum(1 for m in msgs[: last_asst + 1] if _strip_msg_media(m))
        media = None
        for m in msgs[last_asst + 1:]:
            t = _msg_media_type(m)
            if t:
                media = t
                break
        if media:
            target = get_vision_model()
            data["model"] = target
            return f"{model}→{target}（当前轮检测到 {media}，历史剥离 {stripped} 条）"
        if stripped:
            log.info("视觉历史剥离: %s（%d 条历史消息的媒体已占位替换，未改路由）",
                     model, stripped)
        return None
    media = _detect_media_type(data)
    if not media:
        return None
    target = get_vision_model()
    data["model"] = target
    return f"{model}→{target}（检测到 {media}）"


def _sanitize_for_external(data: dict):
    """修复客户端历史消息中缺失的 tool_call_id 等字段（就地修改）。

    外部 Provider（如 Agnes）对 OpenAI schema 校验比上游更严格，
    tool 消息缺 tool_call_id 会直接 400（json_parse_error）。
    策略：assistant.tool_calls 缺 id 则补齐并记录；role=tool 消息缺
    tool_call_id 时按顺序回填；无对应调用则降级为 user 文本消息。
    """
    msgs = data.get("messages")
    if not isinstance(msgs, list):
        return
    pending_ids = []
    for m in msgs:
        if not isinstance(m, dict):
            continue
        if m.get("role") == "assistant" and isinstance(m.get("tool_calls"), list):
            for tc in m["tool_calls"]:
                if not isinstance(tc, dict):
                    continue
                cid = tc.get("id")
                if not isinstance(cid, str) or not cid:
                    cid = f"call_{uuid.uuid4().hex[:12]}"
                    tc["id"] = cid
                pending_ids.append(cid)
        elif m.get("role") == "tool":
            tcid = m.get("tool_call_id")
            if not isinstance(tcid, str) or not tcid:
                if pending_ids:
                    m["tool_call_id"] = pending_ids.pop(0)
                else:
                    content = m.get("content")
                    if isinstance(content, list):
                        text = " ".join(
                            p.get("text", "") for p in content if isinstance(p, dict)
                        )
                    elif content is None:
                        text = ""
                    else:
                        text = str(content)
                    m["role"] = "user"
                    m["content"] = f"[tool result] {text}".strip()
                    m.pop("tool_call_id", None)


# ─── Cline 官方通道转发（2026-09-24 新增） ─────────────────────────────────

def _forward_cline(data: dict, model: str, prov: dict):
    """转发到 Cline 官方 Provider（凭证/指纹头/冷却由 proxy_core.cline 管理）。

    与通用外部通道分路的原因：上游虽是 OpenAI 协议，但免费通道有三道风控
    （指纹头 403 / 剥 max_tokens / 非流式强制转流式）+ 请求发起串行门 +
    429 日配额冷却 + 空响应（只有 reasoning 无 content）换号重试，
    与静态 key 池的 401/402/429 换 key 逻辑完全不同路数。
    """
    started = time.perf_counter()
    _sanitize_for_external(data)
    pname = prov["name"]
    channels = cline.channels_for(pname)
    if not channels:
        return jsonify({"error": f"Cline 通道 {pname} 未配置任何凭证",
                        "hint": "请先登录 Cline 官方客户端（VS Code 插件或桌面版）"}), 503
    is_stream = bool(data.get("stream"))
    body, force_stream = cline.build_upstream_body(data, model)
    attempts = max(3, len(channels))
    tried = set()

    def _pick():
        return cline.pick_channel(pname, exclude=tried or None)

    def _send(client, ch, token):
        """指纹头 → 串行门内发请求（门只覆盖拿响应头阶段，对齐 cline2api）。"""
        sid = f"sess_{int(time.time() * 1000)}"
        hdrs = ch.request_headers(token, sid)
        if is_stream or force_stream:
            hdrs["Accept"] = "text/event-stream"
        req = client.build_request(
            "POST", cline.API_BASE + "/chat/completions", json=body, headers=hdrs,
            timeout=httpx.Timeout(connect=15, read=120, write=180, pool=15))
        # 串行门（0924 审计 L-3 改造）：等待轮到本请求 → 发起 → 响应头一到
        # 立即放行下一个。mark 必须在 finally 里，send 抛异常也不能把门卡死。
        cline.wait_for_slot(model)
        try:
            return client.send(req, stream=is_stream or force_stream)
        finally:
            cline.mark_slot_done(model)

    def _classify(ch, status, snippet):
        """非 200 响应的通道级处置；返回 True = 换号/重试有益。"""
        if status == 401:
            # token 被轮换/失效：作废缓存（下次强制重读文件/自刷新）再试一次
            ch.invalidate()
            tried.discard(ch.label)  # 允许同通道刷新后重试（attempts 兜底防死循环）
            return True
        if status == 429:
            ch.set_cooldown(cline.parse_cooldown_seconds(snippet, 429), snippet[:120])
            return True
        if status >= 500 and "empty response content" in snippet:
            ch.set_cooldown(cline.ERROR_COOLDOWN_S, "empty response content")
            return True
        return False

    # ── 流式 ─────────────────────────────────────────────────
    if is_stream:
        client_key_label = getattr(g, "api_key_label", None) if has_request_context() else None
        client = httpx.Client()
        r = None
        ch = None
        last_status, last_snippet = 0, ""
        try:
            for _attempt in range(attempts):
                ch = _pick()
                if ch is None:
                    break
                tried.add(ch.label)
                try:
                    token = ch.get_token()
                except Exception as e:
                    log.warning("[cline] %s 凭证不可用，换下一个通道: %s", ch.label, e)
                    ch.set_cooldown(cline.ERROR_COOLDOWN_S, str(e)[:120])
                    continue
                try:
                    r = _send(client, ch, token)
                except Exception as e:
                    # 连接层失败（SSL 瞬断/超时）多为可重试瞬时故障：
                    # 退避 1.5~2.5s 后重试同通道（attempts 兜底防死循环）
                    log.warning("[cline] %s 请求发起失败（第 %d 次），退避重试: %s",
                                ch.label, _attempt + 1, e)
                    tried.discard(ch.label)
                    time.sleep(1.5 + time.time() % 1)
                    continue
                if r.status_code == 200:
                    break
                status = r.status_code
                try:
                    snippet = r.read()[:800].decode("utf-8", errors="replace")
                except Exception:
                    snippet = ""
                r.close()
                last_status, last_snippet = status, snippet
                if _classify(ch, status, snippet):
                    log.warning("[cline] %s 流式返回 %s，换通道重试: %s",
                                ch.label, status, snippet[:200])
                    r = None
                    continue
                # 换号无益的错误（400/403/404…）：如实透传
                log.error("[cline] 上游流式返回 %s: %s", status, snippet)
                client.close()
                return _openai_error(
                    f"upstream returned {status} for model '{model}': {snippet[:300]}",
                    "proxy_upstream_error", f"upstream_{status}", status)
            if r is None or r.status_code != 200:
                client.close()
                activity.add("error", f"{model} · {pname} 上游不可用（{last_status or '全冷却'}）",
                             model=model, user_id=pname,
                             latency_ms=int((time.perf_counter() - started) * 1000),
                             tokens=0, status=last_status or 503)
                kdb.record_error(pname, model, last_status or 503, "cline_stream_no_target",
                                 (last_snippet or "no usable channel")[:200])
                if last_status:
                    return _openai_error(
                        f"upstream returned {last_status} for model '{model}': {last_snippet[:300]}",
                        "proxy_upstream_error", f"upstream_{last_status}", last_status)
                return _openai_error(
                    f"no usable cline credential for provider '{pname}' (cooling down)",
                    "proxy_upstream_error", "external_credentials_unavailable", 503)
            key_label = ch.label if len(channels) > 1 else pname
            load_key = f"cl:{pname}|{key_label}"
            return Response(
                _stream_body(_UpstreamOpen(status=200,
                                           resp=cline.ClineStreamWrap(r, model),
                                           client=client),
                             model, key_label, started, None, client_key_label,
                             load_key=load_key),
                content_type="text/event-stream")
        except Exception as e:
            client.close()
            log.error("[cline] %s 请求失败: %s", pname, e)
            return jsonify({"error": f"{pname} 请求失败: {e}"}), 502

    # ── 非流式（免费通道：上游强制流式再聚合；空 content 换号重试） ──
    # 0924 审计 M-1：整段异常兜底（聚合/解析/记账异常不再炸成 Flask 500），
    # 错误口径与流式分支一致（OpenAI 错误 JSON + activity/kdb 记录）。
    result = None
    best_result = None
    ch_used = None
    last_status, last_snippet = 0, ""
    try:
        with httpx.Client() as client:
            for _attempt in range(attempts):
                ch = _pick()
                if ch is None:
                    break
                tried.add(ch.label)
                ch_used = ch
                try:
                    token = ch.get_token()
                except Exception as e:
                    log.warning("[cline] %s 凭证不可用，换下一个通道: %s", ch.label, e)
                    ch.set_cooldown(cline.ERROR_COOLDOWN_S, str(e)[:120])
                    continue
                try:
                    r = _send(client, ch, token)
                except Exception as e:
                    # 连接层失败（SSL 瞬断/超时）多为可重试瞬时故障：
                    # 退避 1.5~2.5s 后重试同通道（attempts 兜底防死循环）
                    log.warning("[cline] %s 请求发起失败（第 %d 次），退避重试: %s",
                                ch.label, _attempt + 1, e)
                    tried.discard(ch.label)
                    time.sleep(1.5 + time.time() % 1)
                    continue
                status = r.status_code
                if status == 200:
                    try:
                        ct = r.headers.get("content-type") or ""
                        if force_stream and "text/event-stream" in ct:
                            result = cline.aggregate_stream(r, model)
                        else:
                            result = cline.unwrap_data(r.json())
                            if isinstance(result, dict):
                                result["model"] = model
                            else:
                                result = None  # 200 但结构非预期 → 按坏响应重试
                    except Exception as parse_err:
                        # 解析失败（SSE 读中断/非 JSON 体）按坏响应换号重试，不炸 500
                        log.warning("[cline] %s 200 响应解析失败，按坏响应重试: %s",
                                    ch.label, parse_err)
                        result = None
                    finally:
                        r.close()
                    if result is None:
                        ch.set_cooldown(cline.EMPTY_COOLDOWN_S, "unparseable/empty 200 body")
                        continue
                    # content 兼容 str / list[{text}] / None（防上游畸形体 AttributeError）
                    msg = ((result or {}).get("choices") or [{}])[0].get("message") or {}
                    _c = msg.get("content")
                    _ctext = _c if isinstance(_c, str) else (
                        "".join(p.get("text", "") for p in _c if isinstance(p, dict))
                        if isinstance(_c, list) else "")
                    has_tool_calls = isinstance(msg.get("tool_calls"), list) and bool(msg["tool_calls"])
                    if has_tool_calls or (_ctext.strip() and not result.get("reasoning_used_as_content")):
                        break  # 有正式 content 或完整工具调用：好响应
                    # 200 但 content 全空（免费通道概率性坏响应）：短冷却换号重试
                    best_result = result  # 兜底留着（重试全空时至少返回 reasoning）
                    ch.set_cooldown(cline.EMPTY_COOLDOWN_S, "empty content")
                    log.warning("[cline] %s 返回空 content，冷却后重试", ch.label)
                    result = None
                    continue
                try:
                    snippet = r.read()[:800].decode("utf-8", errors="replace")
                except Exception:
                    snippet = ""
                r.close()
                last_status, last_snippet = status, snippet
                if _classify(ch, status, snippet):
                    log.warning("[cline] %s 返回 %s，换通道重试: %s",
                                ch.label, status, snippet[:200])
                    continue
                log.error("[cline] 上游返回 %s: %s", status, snippet)
                activity.add("error", f"{model} · {pname} 上游返回 {status}",
                             model=model, user_id=pname,
                             latency_ms=int((time.perf_counter() - started) * 1000),
                             tokens=0, status=status)
                return Response(snippet.encode("utf-8"), status=status,
                                content_type="application/json")
        if result is None:
            result = best_result
        if result is None:
            activity.add("error", f"{model} · {pname} 无可用通道/全冷却",
                         model=model, user_id=pname,
                         latency_ms=int((time.perf_counter() - started) * 1000),
                         tokens=0, status=last_status or 503)
            kdb.record_error(pname, model, last_status or 503, "cline_nonstream_no_target",
                             (last_snippet or "no usable channel")[:200])
            if last_status:
                return Response(last_snippet.encode("utf-8"), status=last_status,
                                content_type="application/json")
            return _openai_error(
                f"no usable cline credential for provider '{pname}' (cooling down)",
                "proxy_upstream_error", "external_credentials_unavailable", 503)
        latency_ms = int((time.perf_counter() - started) * 1000)
        usage = result.get("usage") if isinstance(result, dict) else None
        key_label = (ch_used.label if ch_used and len(channels) > 1 else pname)
        if usage:
            token_stats.record(key_label, model, usage)
            _record_key_usage(model, usage)
        water.observe(pname, model, result.get("model", model) if isinstance(result, dict) else model,
                      usage, ttft_ms=latency_ms, total_ms=latency_ms)
        activity.add("ok", f"{model} · {key_label}", model=model, user_id=key_label,
                     latency_ms=latency_ms,
                     tokens=(usage or {}).get("total_tokens", 0), status=200)
        kdb.record_request(
            user_id=pname, model_requested=model,
            model_served=result.get("model", model) if isinstance(result, dict) else model,
            status_code=200,
            prompt_tokens=(usage or {}).get("prompt_tokens", 0),
            completion_tokens=(usage or {}).get("completion_tokens", 0),
            total_tokens=(usage or {}).get("total_tokens", 0),
            duration_ms=latency_ms, is_stream=False,
        )
        return jsonify(result)
    except Exception as e:
        # 0924 审计 M-1：兜底（记账/写盘等未预期异常），口径同流式分支
        log.error("[cline] %s 非流式请求失败: %s", pname, e)
        activity.add("error", f"{model} · {pname} 异常: {e}", model=model, user_id=pname,
                     latency_ms=int((time.perf_counter() - started) * 1000),
                     tokens=0, status="异常")
        kdb.record_error(pname, model, 502, "cline_nonstream_exception", str(e)[:200])
        return _openai_error(f"cline provider error: {e}", "proxy_upstream_error",
                             "upstream_exception", 502)


# ───────── MiniCode（mcode / MiniMax Code CLI）官方通道转发（2026-10-01 接入） ─────

def _forward_minicode(data: dict, model: str, prov: dict):
    """转发到 MiniCode 官方通道（mcode 登录态，Anthropic Messages 协议）。

    与 cline 通道同框架：凭证/刷新/冷却由 proxy_core.minicode 管理，这里只做
    OpenAI → Anthropic 请求体转换 + Anthropic → OpenAI 响应回转：
    - 模型别名（mcode-m3 → MiniMax-M3）在上游体里改写，记账用客户端请求名；
    - 401 凭证失效 → invalidate 重读（官方客户端/本代理刷新后同通道再试一次）；
    - 429 额度限流 → 解析等待时长入冷却，换通道；5xx → 短冷却换通道；
    - 流式经 MiniCodeStreamWrap 转成 OpenAI SSE，_stream_body 统一记账/注水检测；
    - 非流式 200 但 content/tool_calls 全空且无 thinking → 视为坏响应换通道重试，
      只有 thinking 时按 reasoning 兜底返回（防误伤正常的纯思考响应）。
    """
    started = time.perf_counter()
    _sanitize_for_external(data)
    pname = prov["name"]
    channels = minicode.channels_for(pname)
    if not channels:
        return jsonify({"error": f"MiniCode 通道 {pname} 未配置任何凭证",
                        "hint": "请先运行 mcode login（或配置 auth_path）"}), 503
    is_stream = bool(data.get("stream"))
    _amap = prov.get("model_aliases") or {}
    upstream_model = _amap.get(model, model)
    ch0 = channels[0]
    body = minicode.build_upstream_body(data, upstream_model, ch0.max_tokens_default)
    is_stream = bool(body.get("stream"))
    attempts = max(3, len(channels))
    tried = set()
    # 2026-10-01 实测：MiniMax 网关偶发瞬时 400 "tool id not found (2013)"
    # （同请求体直投/复跑均 200，属网关工具 id 登记异步抖动）——限次同通道重试。
    transient_400_left = {"n": 1}

    def _pick():
        return minicode.pick_channel(pname, exclude=tried or None)

    def _send(client, ch, token):
        hdrs = ch.request_headers(token)
        if is_stream:
            hdrs["Accept"] = "text/event-stream"
        return client.send(
            client.build_request(
                "POST", ch.api_base + "/messages", json=body, headers=hdrs,
                timeout=httpx.Timeout(connect=15, read=300, write=180, pool=15)),
            stream=is_stream)

    def _classify(ch, status, snippet):
        """可重试分类（True = 换通道/同通道再试），False = 透传错误给客户端。"""
        if status == 401:
            # token 失效：丢弃缓存强制重读（官方客户端/刷新链路可能已恢复），
            # 同通道放回再试一次（attempts 追加循环上限内）。
            ch.invalidate()
            tried.discard(ch.label)
            return True
        if status == 402:
            # 2026-10-01 实测（insufficient balance 1008）：mcode 账号 token plan
            # 窗口/活动额度耗尽。重试与换通道均无意义——挂长冷却防调用方高频
            # 重打，透传原始错误给上层（agent/人工），提示"等待窗口恢复或官网
            # 领取额度"；中转内绝不静默改投其他模型。
            ch.set_cooldown(600, f"额度耗尽(402): {snippet[:80]}")
            return False
        if status == 429:
            ch.set_cooldown(minicode.parse_cooldown_seconds(snippet, 429), snippet[:120])
            return True
        if status == 400 and "tool id" in snippet and "not found" in snippet \
                and transient_400_left["n"] > 0:
            # 网关瞬时抖动：同通道原样重发一次（限 1 次，防真坏请求反复打上游）
            transient_400_left["n"] -= 1
            tried.discard(ch.label)
            return True
        if status >= 500:
            ch.set_cooldown(minicode.ERROR_COOLDOWN_S, f"upstream {status}")
            return True
        return False

    # ─── 流式 ─────────────────────────────────────────────────────────────────
    if is_stream:
        client_key_label = getattr(g, "api_key_label", None) if has_request_context() else None
        client = httpx.Client()
        r = None
        ch = None
        last_status, last_snippet = 0, ""
        try:
            for _attempt in range(attempts):
                ch = _pick()
                if ch is None:
                    break
                tried.add(ch.label)
                try:
                    token = ch.get_token()
                except Exception as e:
                    log.warning("[minicode] %s 凭证不可用，换下一通道: %s", ch.label, e)
                    ch.set_cooldown(minicode.ERROR_COOLDOWN_S, str(e)[:120])
                    continue
                try:
                    r = _send(client, ch, token)
                except Exception as e:
                    # 连接层失败（SSL 瞬断/超时）：当前通道不算死，短退避后可再试
                    log.warning("[minicode] %s 发送失败（第 %d 次，瞬时退避）: %s",
                                ch.label, _attempt + 1, e)
                    tried.discard(ch.label)
                    time.sleep(1.5 + time.time() % 1)
                    continue
                if r.status_code == 200:
                    break
                status = r.status_code
                try:
                    snippet = r.read()[:800].decode("utf-8", errors="replace")
                except Exception:
                    snippet = ""
                r.close()
                last_status, last_snippet = status, snippet
                if _classify(ch, status, snippet):
                    log.warning("[minicode] %s 流式错误 %s，换通道重试: %s",
                                ch.label, status, snippet[:200])
                    r = None
                    continue
                log.error("[minicode] 流式错误 %s: %s", status, snippet)
                client.close()
                return _openai_error(
                    f"upstream returned {status} for model '{model}': {snippet[:300]}",
                    "proxy_upstream_error", f"upstream_{status}", status)
            if r is None or r.status_code != 200:
                client.close()
                activity.add("error", f"{model} @ {pname} 多通道不可用（{last_status or '全部冷却'}）",
                             model=model, user_id=pname,
                             latency_ms=int((time.perf_counter() - started) * 1000),
                             tokens=0, status=last_status or 503)
                kdb.record_error(pname, model, last_status or 503, "minicode_stream_no_target",
                                 (last_snippet or "no usable channel")[:200])
                if last_status:
                    return _openai_error(
                        f"upstream returned {last_status} for model '{model}': {last_snippet[:300]}",
                        "proxy_upstream_error", f"upstream_{last_status}", last_status)
                return _openai_error(
                    f"no usable minicode credential for provider '{pname}' (cooling down)",
                    "proxy_upstream_error", "external_credentials_unavailable", 503)
            key_label = ch.label if len(channels) > 1 else pname
            load_key = f"mc:{pname}|{key_label}"
            return Response(
                _stream_body(_UpstreamOpen(status=200,
                                           resp=minicode.MiniCodeStreamWrap(r, model),
                                           client=client),
                             model, key_label, started, None, client_key_label,
                             load_key=load_key),
                content_type="text/event-stream")
        except Exception as e:
            client.close()
            log.error("[minicode] %s 流式失败: %s", pname, e)
            return jsonify({"error": f"{pname} 流式失败: {e}"}), 502

    # ─── 非流式（流式网关强制流式时聚合兜底：MiniCode 端点两态均支持，直接非流式请求）───
    result = None
    best_result = None
    ch_used = None
    last_status, last_snippet = 0, ""
    try:
        with httpx.Client() as client:
            for _attempt in range(attempts):
                ch = _pick()
                if ch is None:
                    break
                tried.add(ch.label)
                ch_used = ch
                try:
                    token = ch.get_token()
                except Exception as e:
                    log.warning("[minicode] %s 凭证不可用，换下一通道: %s", ch.label, e)
                    ch.set_cooldown(minicode.ERROR_COOLDOWN_S, str(e)[:120])
                    continue
                try:
                    r = _send(client, ch, token)
                except Exception as e:
                    log.warning("[minicode] %s 发送失败（第 %d 次，瞬时退避）: %s",
                                ch.label, _attempt + 1, e)
                    tried.discard(ch.label)
                    time.sleep(1.5 + time.time() % 1)
                    continue
                status = r.status_code
                if status == 200:
                    try:
                        ct = r.headers.get("content-type") or ""
                        if "text/event-stream" in ct:
                            # 网关偶发强制 SSE：聚合为单响应
                            result = minicode.aggregate_stream(r, model)
                        else:
                            result = minicode.message_to_chat(r.json(), model)
                    except Exception as parse_err:
                        log.warning("[minicode] %s 200 响应解析失败，按坏响应处理: %s",
                                    ch.label, parse_err)
                        result = None
                    finally:
                        r.close()
                    if result is None:
                        ch.set_cooldown(minicode.ERROR_COOLDOWN_S, "unparseable 200 body")
                        continue
                    msg = (result.get("choices") or [{}])[0].get("message") or {}
                    has_tools = isinstance(msg.get("tool_calls"), list) and bool(msg["tool_calls"])
                    has_text = isinstance(msg.get("content"), str) and bool(msg["content"].strip())
                    has_reasoning = bool((msg.get("reasoning_content") or "").strip())
                    if has_tools or has_text or has_reasoning:
                        break  # 有效响应，收工
                    # 200 但三无（无正文/无工具/无思考）：按坏响应换通道重试
                    best_result = result
                    ch.set_cooldown(minicode.ERROR_COOLDOWN_S, "empty 200 body")
                    log.warning("[minicode] %s 空响应体，冷却换通道", ch.label)
                    result = None
                    continue
                try:
                    snippet = r.read()[:800].decode("utf-8", errors="replace")
                except Exception:
                    snippet = ""
                r.close()
                last_status, last_snippet = status, snippet
                if _classify(ch, status, snippet):
                    log.warning("[minicode] %s 错误 %s，换通道重试: %s",
                                ch.label, status, snippet[:200])
                    continue
                log.error("[minicode] 非流式错误 %s: %s", status, snippet)
                log.error("[minicode] 400 时上游请求体: %s",
                          json.dumps(body, ensure_ascii=False)[:1200])
                activity.add("error", f"{model} @ {pname} 非流式错误 {status}",
                             model=model, user_id=pname,
                             latency_ms=int((time.perf_counter() - started) * 1000),
                             tokens=0, status=status)
                return Response(snippet.encode("utf-8"), status=status,
                                content_type="application/json")
        if result is None:
            result = best_result
        if result is None:
            activity.add("error", f"{model} @ {pname} 无可用通道/全部冷却",
                         model=model, user_id=pname,
                         latency_ms=int((time.perf_counter() - started) * 1000),
                         tokens=0, status=last_status or 503)
            kdb.record_error(pname, model, last_status or 503, "minicode_nonstream_no_target",
                             (last_snippet or "no usable channel")[:200])
            if last_status:
                return Response(last_snippet.encode("utf-8"), status=last_status,
                                content_type="application/json")
            return _openai_error(
                f"no usable minicode credential for provider '{pname}' (cooling down)",
                "proxy_upstream_error", "external_credentials_unavailable", 503)
        latency_ms = int((time.perf_counter() - started) * 1000)
        usage = result.get("usage") if isinstance(result, dict) else None
        key_label = (ch_used.label if ch_used and len(channels) > 1 else pname)
        if usage:
            token_stats.record(key_label, model, usage)
            _record_key_usage(model, usage)
        water.observe(pname, model, result.get("model", model) if isinstance(result, dict) else model,
                      usage, ttft_ms=latency_ms, total_ms=latency_ms)
        activity.add("ok", f"{model} @ {key_label}", model=model, user_id=key_label,
                     latency_ms=latency_ms,
                     tokens=(usage or {}).get("total_tokens", 0), status=200)
        kdb.record_request(
            user_id=pname, model_requested=model,
            model_served=result.get("model", model) if isinstance(result, dict) else model,
            status_code=200,
            prompt_tokens=(usage or {}).get("prompt_tokens", 0),
            completion_tokens=(usage or {}).get("completion_tokens", 0),
            total_tokens=(usage or {}).get("total_tokens", 0),
            duration_ms=latency_ms, is_stream=False,
        )
        return jsonify(result)
    except Exception as e:
        log.error("[minicode] %s 非流式失败: %s", pname, e)
        activity.add("error", f"{model} @ {pname} 异常: {e}", model=model, user_id=pname,
                     latency_ms=int((time.perf_counter() - started) * 1000),
                     tokens=0, status="异常")
        kdb.record_error(pname, model, 502, "minicode_nonstream_exception", str(e)[:200])
        return _openai_error(f"minicode provider error: {e}", "proxy_upstream_error",
                             "upstream_exception", 502)


def _forward_external(data: dict, model: str, prov: dict):
    """转发到外部 OpenAI 兼容 Provider（静态 API Key，不占主账号池）。

    type 为 atomcode 的 provider：凭证与 sign_v1 签名头由 proxy_core.atomcode
    动态生成（读官方客户端 auth.toml），请求体先序列化成字节再签名转发。

    普通 provider 支持 key 池（config.json api_keys 数组）：每次请求按
    "最少进行中请求优先 + 同负载轮转" 选 key；401/402/403/429（key 失效/
    配额/限流）在未向客户端输出任何字节前自动换下一个 key 重试。
    """
    if providers.is_cline(prov):
        return _forward_cline(data, model, prov)
    if providers.is_minicode(prov):
        return _forward_minicode(data, model, prov)
    started = time.perf_counter()
    _sanitize_for_external(data)
    # 0928: model_aliases 别名命中时把请求体重写为上游真实模型 ID；
    # body_kwargs 持有同一 dict 引用，httpx 构建请求时才序列化，
    # 故此处改写对流式/非流式均生效。记账仍用原始 model（别名）。
    _amap = prov.get("model_aliases") or {}
    if data.get("model") in _amap:
        data["model"] = _amap[data["model"]]
    is_stream = data.get("stream", False)
    pname = prov["name"]
    # 0929：全 key 配额耗尽冷却中 → 快速失败，不再逐 key 白打上游
    _cd_until = _prov_quota_cooldown.get(pname)
    if _cd_until and time.time() < _cd_until:
        left = max(1, int(_cd_until - time.time()))
        log.warning("[%s] 全 key 配额冷却中，剩 %d 秒（快速失败不打上游）", pname, left)
        return _openai_error(
            f"provider '{pname}' quota cooldown, retry in {left}s",
            "rate_limit_error", "provider_quota_cooldown", 503, left)
    url = f"{prov['base_url']}/chat/completions"
    body_kwargs = {"json": data}
    is_ac = providers.is_atomcode(prov)
    n_keys = 0 if is_ac else len(prov.get("keys") or [])
    retryable = {401, 402, 403, 429}
    attempts = max(1, n_keys)

    if not is_ac and n_keys == 0:
        return jsonify({"error": f"{pname} 未配置 API Key"}), 503

    if is_ac:
        # AtomCode：签名覆盖请求体原始字节 → 必须自己序列化并按字节发送。
        # 多账号：签名挪进重试循环，每个通道独立签名、独立记账（label）。
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        sign_path = urlparse(url).path or "/chat/completions"
        n_channels = len(channels_for(pname))
        if n_channels == 0:
            return jsonify({"error": f"AtomCode 通道 {pname} 未配置任何凭证",
                            "hint": "请上传凭证或登录官方 AtomCode 客户端"}), 503
        attempts = max(1, n_channels)
        # 签名覆盖序列化后的字节 → 流式/非流式都必须按原始字节发送。
        # json=data 会让 httpx 用默认 ensure_ascii=True 重新序列化，请求体含
        # 中文时发送字节与签名字节不一致 → 上游 403 ATOMCODE_SIG_INVALID。
        body_kwargs = {"content": body}

    def _headers_for(entry: dict) -> dict:
        h = {
            "Authorization": f"Bearer {entry['key']}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if is_stream else "application/json",
        }
        return h

    try:
        if is_stream:
            # 生成器运行在请求上下文之外，客户端 key 标签必须提前捕获（按 key 记账）
            client_key_label = getattr(g, "api_key_label", None) if has_request_context() else None

            def _open_stream():
                """开闸：建连 + 状态码判定 + 换 key/账号重试，全程未向客户端吐字节。

                返回 (resp, key_label, client)，client 的生命周期交由吐流阶段收尾；
                凭证全部不可用（签名失败 / 每个 key 都被拒）返回 None。
                """
                client = httpx.Client()
                tried = set()
                key_label = pname
                try:
                    for attempt in range(attempts):
                        if is_ac:
                            ch = pick_channel(pname, exclude=tried or None)
                            if ch is None:
                                break
                            tried.add(ch.label)
                            key_label = ch.label if n_channels > 1 else pname
                            try:
                                hdrs = ch.sign_headers("POST", sign_path, body)
                            except Exception as e:
                                # 凭证不可用（过期且刷新失败/文件丢失）→ 换下一个账号
                                log.warning("[atomcode] %s 签名失败，换下一个账号: %s",
                                            ch.label, e)
                                continue
                            hdrs["Accept"] = "text/event-stream"
                        else:
                            entry = providers.pick_key(prov, exclude=tried or None)
                            if entry is None:
                                break
                            tried.add(entry["key"])
                            key_label = entry["label"] if n_keys > 1 else pname
                            hdrs = _headers_for(entry)
                        req = client.build_request(
                            "POST", url, **body_kwargs, headers=hdrs,
                            timeout=httpx.Timeout(connect=15, read=120, write=180, pool=15))
                        r = client.send(req, stream=True)
                        # 200 / 换号无益的状态 / 已是最后一次尝试 → 交给上层定夺
                        if r.status_code == 200 or r.status_code not in retryable \
                                or attempt == attempts - 1:
                            return r, key_label, client
                        err_body = r.read()[:400].decode("utf-8", errors="replace")
                        log.warning("[%s] key %s 流式连接返回 %s，换下一个 key: %s",
                                    pname, key_label, r.status_code, err_body[:200])
                        r.close()
                    client.close()
                    return None
                except Exception:
                    client.close()
                    raise

            result = _open_stream()
            if result is None:
                return _openai_error(
                    f"no usable credential for provider '{pname}'",
                    "proxy_upstream_error", "external_credentials_unavailable", 503)
            r, key_label, client = result
            status = r.status_code
            if status != 200:
                # 一个字节都还没吐给客户端：状态码改得动，如实回真实码
                try:
                    snippet = r.read()[:800].decode("utf-8", errors="replace")
                except Exception:
                    snippet = ""
                log.error("[%s] 上游流式返回 %s: %s", pname, status, snippet)
                activity.add("error", f"{model} · {key_label} 上游返回 {status}",
                             model=model, user_id=key_label,
                             latency_ms=int((time.perf_counter() - started) * 1000),
                             tokens=0, status=status)
                opened = _UpstreamOpen(status=status, resp=r, client=client)
                opened.close()
                # 0929：全部 key 均配额耗尽 → provider 级冷却，后续请求快速失败
                if status == 429 and ("quota" in snippet.lower() or not snippet):
                    _prov_quota_cooldown[pname] = time.time() + PROVIDER_QUOTA_COOLDOWN_S
                    log.warning("[%s] 全部 key 配额耗尽(429 quota)，provider 冷却 %.0f 分钟",
                                 pname, PROVIDER_QUOTA_COOLDOWN_S / 60)
                return _openai_error(
                    f"upstream returned {status} for model '{model}'",
                    "proxy_upstream_error", f"upstream_{status}", status)
            # 负载键加前缀隔离命名空间：与 pick_key/pick_channel 的 load_of 查询键一致
            _prov_quota_cooldown.pop(pname, None)   # 0929：任何 key 成功即解除冷却
            load_key = f"key:{pname}|{key_label}" if not is_ac else f"ac:{pname}|{key_label}"
            return Response(
                _stream_body(_UpstreamOpen(status=200, resp=r, client=client),
                             model, key_label, started, None, client_key_label,
                             load_key=load_key),
                content_type="text/event-stream")

        # 非流式：key 池/账号池逐个尝试（401/402/403/429 换下一个）
        r = None
        tried = set()
        key_label = pname
        with httpx.Client() as client:
            for attempt in range(attempts):
                if is_ac:
                    ch = pick_channel(pname, exclude=tried or None)
                    if ch is None:
                        break
                    tried.add(ch.label)
                    key_label = ch.label if n_channels > 1 else pname
                    try:
                        hdrs = ch.sign_headers("POST", sign_path, body)
                    except Exception as e:
                        log.warning("[atomcode] %s 签名失败，换下一个账号: %s", ch.label, e)
                        continue
                else:
                    entry = providers.pick_key(prov, exclude=tried or None)
                    if entry is None:
                        break
                    tried.add(entry["key"])
                    key_label = entry["label"] if n_keys > 1 else pname
                    hdrs = _headers_for(entry)
                r = client.post(url, **body_kwargs, headers=hdrs, timeout=300)
                if r.status_code == 200 or r.status_code not in retryable \
                        or attempt == attempts - 1:
                    break
                log.warning("[%s] key %s 返回 %s，换下一个 key", pname, key_label, r.status_code)
        if r is None:
            if is_ac:
                return jsonify({"error": f"AtomCode 所有账号凭证均不可用",
                                "hint": "请上传凭证或登录官方 AtomCode 客户端"}), 503
            kdb.record_error(pname, model, 503, "all_keys_exhausted",
                             f"{n_keys} keys tried")
            return jsonify({"error": f"{pname} 所有 API Key 均不可用"}), 503
        latency_ms = int((time.perf_counter() - started) * 1000)
        if r.status_code != 200:
            log.error("[%s] 上游返回 %s: %s", pname, r.status_code, r.text[:800])
            activity.add("error", f"{model} · {key_label} 上游返回 {r.status_code}",
                         model=model, user_id=key_label, latency_ms=latency_ms,
                         tokens=0, status=r.status_code)
            # 0929：全部 key 均配额耗尽 → provider 级冷却，后续请求快速失败
            if r.status_code == 429 and ("quota" in r.text.lower() or not r.text):
                _prov_quota_cooldown[pname] = time.time() + PROVIDER_QUOTA_COOLDOWN_S
                log.warning("[%s] 全部 key 配额耗尽(429 quota)，provider 冷却 %.0f 分钟",
                             pname, PROVIDER_QUOTA_COOLDOWN_S / 60)
            return Response(r.content, status=r.status_code,
                            content_type=r.headers.get("content-type", "application/json"))
        _prov_quota_cooldown.pop(pname, None)   # 0929：任何 key 成功即解除冷却
        resp_json = r.json()
        usage = resp_json.get("usage")
        if usage:
            token_stats.record(key_label, model, usage)
            _record_key_usage(model, usage)
        # 注水检测：外部源同样核对模型名
        water.observe(pname, model, resp_json.get("model"), usage,
                      ttft_ms=latency_ms, total_ms=latency_ms)
        activity.add("ok", f"{model} · {key_label}",
                     model=model, user_id=key_label, latency_ms=latency_ms,
                     tokens=(usage or {}).get("total_tokens", 0), status=200)
        # 知识库记录（0905）：外部 provider 非流式成功
        kdb.record_request(
            user_id=pname, model_requested=model,
            model_served=resp_json.get("model", model), status_code=200,
            prompt_tokens=(usage or {}).get("prompt_tokens", 0),
            completion_tokens=(usage or {}).get("completion_tokens", 0),
            total_tokens=(usage or {}).get("total_tokens", 0),
            duration_ms=latency_ms, is_stream=False,
        )
        return jsonify(resp_json)
    except Exception as e:
        log.error("[%s] 请求失败: %s", pname, e)
        activity.add("error", f"{model} · {pname} 异常: {e}", model=model, user_id=pname,
                     latency_ms=int((time.perf_counter() - started) * 1000),
                     tokens=0, status="异常")
        kdb.record_error(pname, model, 502, "external_exception", str(e)[:200])
        return jsonify({"error": f"{pname} 请求失败: {e}"}), 502


@bp.route("/v1/chat/completions", methods=["POST"])
@require_api_key
def v1_chat():
    data = request.get_json(force=True, silent=True) or {}
    # 调试：记录请求体关键字段（帮助排查 qidicode 发来的异常请求）
    req_model = data.get("model", "")
    req_keys = sorted(data.keys())
    msg_count = len(data.get("messages", []))
    log.info("v1_chat 收到请求: model=%s, keys=%s, messages=%d, stream=%s",
             req_model, req_keys, msg_count, data.get("stream", False))
    # 400 调试：完整请求体写入临时文件（0905 审计修正：默认关闭，config 加 debug_last_request=true 开启）
    if load_config().get("debug_last_request"):
        import os, json as _json
        debug_path = os.path.join(os.path.dirname(__file__), "..", "last_request.json")
        with open(debug_path, "w", encoding="utf-8") as f:
            _json.dump(data, f, ensure_ascii=False, indent=2)
    return _proxy_chat(data)


# ===================== Responses API 完整适配层（0906 重写） =====================
# OpenClaw / 新版 OpenAI SDK 走 /v1/responses。此层把 Responses 方言完整翻译成
# chat/completions：instructions、tools、function_call(_output) 循环、流式事件桥接。

def _responses_to_chat(data: dict) -> dict:
    """Responses 请求 → chat completions 请求体。"""
    msgs = []
    if data.get("instructions"):
        msgs.append({"role": "system", "content": data["instructions"]})
    inp = data.get("input")
    items = [{"type": "message", "role": "user", "content": inp}] if isinstance(inp, str) else (
        inp if isinstance(inp, list) else [])
    for it in items:
        if not isinstance(it, dict):
            msgs.append({"role": "user", "content": str(it)})
            continue
        t = it.get("type")
        if t == "function_call":
            msgs.append({"role": "assistant", "content": None, "tool_calls": [{
                "id": it.get("call_id") or it.get("id") or ("call_" + uuid.uuid4().hex[:8]),
                "type": "function",
                "function": {"name": it.get("name", ""),
                             "arguments": it.get("arguments") or "{}"}}]})
        elif t == "function_call_output":
            out = it.get("output")
            if not isinstance(out, str):
                out = json.dumps(out, ensure_ascii=False) if out is not None else ""
            msgs.append({"role": "tool", "tool_call_id": it.get("call_id", ""), "content": out})
        else:
            role = it.get("role", "user")
            content = it.get("content")
            if isinstance(content, list):
                parts = []
                for p in content:
                    if isinstance(p, dict) and p.get("type") in ("input_text", "output_text", "text", "summary_text"):
                        parts.append(p.get("text", ""))
                    elif isinstance(p, dict) and p.get("type") == "input_image":
                        parts.append("[图片已存档]")
                    elif isinstance(p, str):
                        parts.append(p)
                content = "\n".join(x for x in parts if x)
            elif content is None:
                content = ""
            msgs.append({"role": role, "content": content})
    chat = {"model": data.get("model", ""), "messages": msgs,
            "max_tokens": data.get("max_output_tokens") or 4096,
            "stream": bool(data.get("stream"))}
    rt = data.get("tools")
    if isinstance(rt, list) and rt:
        tools = []
        for t in rt:
            if not isinstance(t, dict):
                continue
            if t.get("type") == "function":
                fn = t.get("function") if isinstance(t.get("function"), dict) else t
                tools.append({"type": "function", "function": {
                    "name": fn.get("name", ""), "description": fn.get("description", ""),
                    "parameters": fn.get("parameters") or {"type": "object", "properties": {}}}})
            else:
                tools.append(t)
        if tools:
            chat["tools"] = tools
            if data.get("tool_choice") is not None:
                chat["tool_choice"] = data["tool_choice"]
            if data.get("parallel_tool_calls") is not None:
                chat["parallel_tool_calls"] = data["parallel_tool_calls"]
    for k in ("temperature", "top_p"):
        if data.get(k) is not None:
            chat[k] = data[k]
    return chat


def _chat_to_response(chat_resp: dict, req_model: str) -> dict:
    """chat completion 响应 → Responses API 对象（含 function_call 输出项）。"""
    choice = (chat_resp.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    out_items = []
    text = msg.get("content") or ""
    if text:
        out_items.append({"id": "msg_0", "type": "message", "role": "assistant",
                          "status": "completed",
                          "content": [{"type": "output_text", "text": text, "annotations": []}]})
    for i, tc in enumerate(msg.get("tool_calls") or []):
        fn = tc.get("function") or {}
        out_items.append({"id": f"fc_{i}", "type": "function_call", "status": "completed",
                          "call_id": tc.get("id") or f"call_{i}", "name": fn.get("name", ""),
                          "arguments": fn.get("arguments") or "{}"})
    usage = chat_resp.get("usage") or {}
    return {"id": "resp_" + str(chat_resp.get("id", "")), "object": "response",
            "created_at": chat_resp.get("created"), "status": "completed",
            "model": req_model, "output": out_items, "output_text": text,
            "usage": {"input_tokens": usage.get("prompt_tokens", 0),
                      "output_tokens": usage.get("completion_tokens", 0),
                      "total_tokens": usage.get("total_tokens", 0)}}


class _RespSSE:
    """chat SSE → Responses SSE 事件桥接（标准 OpenAI Responses 事件协议）。"""

    # 上游错误原文里的 Team=<id> 是内部标识：原文只进日志，客户端整段打码
    _TEAM_LEAK_RE = re.compile(r"Team=[0-9a-zA-Z-]*")

    def __init__(self, req_model: str):
        self.req_model = req_model
        self.rid = "resp_" + uuid.uuid4().hex[:24]
        self.seq = 0
        self.out = []          # 完整 output items（completed 用）
        self.usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        self.msg_state = {"started": False, "text": ""}
        self.fc_state = {}     # tool index → {"started", "args", "name", "call_id"}
        self.failed = False    # 上游流内已报错：finish 必须如实终结为 failed
        self.error_msg = ""    # 上游错误原文（只进日志，不原样回客户端）
        self.error_code = ""   # 上游错误 code（可空，缺省补 upstream_error）

    def ev(self, etype, payload):
        self.seq += 1
        payload = dict(payload)
        payload["type"] = etype
        payload["sequence_number"] = self.seq
        return f"event: {etype}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

    def base_resp(self, status):
        return {"id": self.rid, "object": "response",
                "created_at": int(__import__("time").time()), "status": status,
                "model": self.req_model, "output": self.out, "usage": self.usage}

    def feed_line(self, raw: str):
        """喂一行 chat SSE data（不含前缀）；产出若干 Responses 事件文本。"""
        events = []
        if not raw or raw == "[DONE]":
            return events
        try:
            chunk = json.loads(raw)
        except Exception:
            return events
        if isinstance(chunk.get("usage"), dict) and chunk["usage"]:
            # usage 提取必须在 error 判断之前：上游把 usage 与 error 放在
            # 同一帧时，failed 事件载荷也要带上真实用量（展示用）
            u = chunk["usage"]
            self.usage = {"input_tokens": u.get("prompt_tokens", 0),
                          "output_tokens": u.get("completion_tokens", 0),
                          "total_tokens": u.get("total_tokens", 0)}
        err = chunk.get("error")
        if isinstance(err, dict) and err:
            # 上游流内报错若被静默丢弃，finish 会照发 response.completed，
            # 客户端拿到"成功"的空回复（静默假活）。如实记录后本帧不再产出内容事件。
            self.failed = True
            self.error_msg = str(err.get("message") or err.get("type")
                                 or err.get("code") or "upstream error")
            self.error_code = str(err.get("code") or "")
            log.error("Responses 桥上游流内错误（%s）: %s", self.rid, self.error_msg)
            return events
        for ch in chunk.get("choices") or []:
            delta = ch.get("delta") or {}
            txt = delta.get("content")
            if txt:
                if not self.msg_state["started"]:
                    self.msg_state["started"] = True
                    item = {"id": "msg_0", "type": "message", "role": "assistant",
                            "status": "in_progress", "content": []}
                    events.append(self.ev("response.output_item.added",
                                          {"output_index": 0, "item": item}))
                    events.append(self.ev("response.content_part.added",
                                          {"item_id": "msg_0", "output_index": 0, "content_index": 0,
                                           "part": {"type": "output_text", "text": "", "annotations": []}}))
                self.msg_state["text"] += txt
                events.append(self.ev("response.output_text.delta",
                                      {"item_id": "msg_0", "output_index": 0,
                                       "content_index": 0, "delta": txt}))
            for tc in delta.get("tool_calls") or []:
                idx = tc.get("index", 0)
                st = self.fc_state.setdefault(idx, {"started": False, "args": "",
                                                    "name": "", "call_id": ""})
                if not st["started"]:
                    st["started"] = True
                    st["name"] = (tc.get("function") or {}).get("name", st["name"])
                    st["call_id"] = tc.get("id") or ("call_" + uuid.uuid4().hex[:8])
                    events.append(self.ev("response.output_item.added",
                                          {"output_index": idx + 1,
                                           "item": {"id": f"fc_{idx}", "type": "function_call",
                                                    "status": "in_progress", "call_id": st["call_id"],
                                                    "name": st["name"], "arguments": ""}}))
                argd = (tc.get("function") or {}).get("arguments")
                if argd:
                    st["args"] += argd
                    events.append(self.ev("response.function_call_arguments.delta",
                                          {"item_id": f"fc_{idx}", "output_index": idx + 1,
                                           "delta": argd}))
        return events

    def finish(self):
        """产出全部 done 事件 + response.completed；上游已报错则改发 response.failed。"""
        events = []
        if self.failed:
            # 已失败就不再补 done/completed 事件——把部分内容标成"已完成"同样是谎报
            resp = self.base_resp("failed")
            resp["error"] = {
                # code 与 message 同样脱敏（原文只进日志）；先脱敏后截断，
                # 保证替换后长度仍 ≤64
                "code": self._TEAM_LEAK_RE.sub(
                    "[redacted]", self.error_code or "upstream_error")[:64],
                "message": self._TEAM_LEAK_RE.sub("[redacted]",
                                                  self.error_msg[:300]),
            }
            events.append(self.ev("response.failed", {"response": resp}))
            return events
        if self.msg_state["started"]:
            events.append(self.ev("response.output_text.done",
                                  {"item_id": "msg_0", "output_index": 0, "content_index": 0,
                                   "text": self.msg_state["text"]}))
            events.append(self.ev("response.content_part.done",
                                  {"item_id": "msg_0", "output_index": 0, "content_index": 0,
                                   "part": {"type": "output_text", "text": self.msg_state["text"],
                                            "annotations": []}}))
            events.append(self.ev("response.output_item.done",
                                  {"output_index": 0,
                                   "item": {"id": "msg_0", "type": "message", "role": "assistant",
                                            "status": "completed",
                                            "content": [{"type": "output_text",
                                                         "text": self.msg_state["text"],
                                                         "annotations": []}]}}))
            self.out.append({"id": "msg_0", "type": "message", "role": "assistant",
                             "status": "completed",
                             "content": [{"type": "output_text", "text": self.msg_state["text"],
                                          "annotations": []}]})
        for idx, st in self.fc_state.items():
            if not st["started"]:
                continue
            events.append(self.ev("response.function_call_arguments.done",
                                  {"item_id": f"fc_{idx}", "output_index": idx + 1,
                                   "arguments": st["args"]}))
            item = {"id": f"fc_{idx}", "type": "function_call", "status": "completed",
                    "call_id": st["call_id"], "name": st["name"], "arguments": st["args"] or "{}"}
            events.append(self.ev("response.output_item.done", {"output_index": idx + 1, "item": item}))
            self.out.append(item)
        events.append(self.ev("response.completed", {"response": self.base_resp("completed")}))
        return events


@bp.route("/v1/responses", methods=["POST"])
@require_api_key
def v1_responses():
    """OpenAI Responses API 完整兼容端点（0906 重写：工具调用 + 流式事件桥接）。"""
    data = request.get_json(force=True, silent=True) or {}
    chat_data = _responses_to_chat(data)
    if not chat_data["messages"]:
        return jsonify({"error": {"message": "Missing 'input' parameter.",
                                  "type": "invalid_request_error", "param": "input",
                                  "code": "missing_input"}}), 400
    req_model = data.get("model", "")
    if chat_data["stream"]:
        inner = _flask_response(_proxy_chat(chat_data))
        if inner.status_code != 200:
            # 守卫 404 / 池耗尽 503 / 上游失败透传：原样返回结构化错误，
            # 绝不进 SSE 生成器（否则元组被当 Response 用 → 流中途断链）
            return inner

        def gen():
            bridge = _RespSSE(req_model)
            yield bridge.ev("response.created", {"response": bridge.base_resp("in_progress")})
            try:
                for piece in inner.response:
                    for line in piece.decode("utf-8", errors="replace").splitlines():
                        line = line.strip()
                        if not line.startswith("data:"):
                            continue
                        payload = line[5:].strip()
                        if payload == "[DONE]":
                            break
                        for ev in bridge.feed_line(payload):
                            yield ev
            finally:
                for ev in bridge.finish():
                    yield ev
            yield "data: [DONE]\n\n"

        return Response(stream_with_context(gen()), content_type="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    resp = _flask_response(_proxy_chat(chat_data))
    chat_json = resp.get_json(silent=True) or {}
    if "choices" not in chat_json:
        # 透传上游错误（含 404 守卫/429/401 等原样语义）
        return resp
    return jsonify(_chat_to_response(chat_json, req_model))


# ===================== Anthropic Messages API 适配端点（0924 新增） =====================


def _anthropic_upstream_error(resp):
    """把内部 OpenAI 风格错误响应转成 Anthropic 风格，状态码保持不变。"""
    payload = resp.get_json(silent=True) or {}
    body = anthropic.error_from_openai(payload, resp.get_data(as_text=True)[:300])
    return jsonify(body), resp.status_code


# 官方 Messages API 的请求体上限是 32MB，超限的载荷上游本来也会拒。
# 只给 Anthropic 这两个入口按请求设上限（不动 /v1/chat/completions 的既有口径）：
# count_tokens 的估算成本与体积成正比，先拒掉必然失败的请求即可封顶。
ANTHROPIC_MAX_BODY_BYTES = 32 * 1024 * 1024


def _anthropic_too_large(actual: int):
    """413 响应；actual 用于日志（Content-Length 或实际读到的字节数）。"""
    log.warning("Anthropic 请求体超限: %s bytes > %s",
                actual, ANTHROPIC_MAX_BODY_BYTES)
    limit_mb = max(1, ANTHROPIC_MAX_BODY_BYTES // (1024 * 1024))
    return jsonify(anthropic.error_payload(
        f"Request body exceeds the {limit_mb}MB limit of this relay.",
        "invalid_request_error")), 413


def _anthropic_request_body():
    """读 Anthropic 入口的请求体；返回 (data, None) 或 (None, (json, 413))。

    两条超限路径都要接住，不能让 werkzeug 的口径差变成误导性的 400：
    - 带 Content-Length：建流时按头预检，超限直接抛 RequestEntityTooLarge。
    - chunked（无 Content-Length）：预检看不到长度，LimitedStream 只会静默截到
      上限就正常返回（readall 到 limit 即停，不调 on_exhausted）。截断的 JSON
      若落到下面的 except，客户端收到的就是"缺 messages"这种 400。

    判据是"实际读到多少字节"，并为此多给 1 字节的读取额度：被截断的读永远到不了
    cap 以上，所以恰好等于 cap 的合法请求不会被误拒，越界的仍然拦在解析之前。
    """
    request.max_content_length = ANTHROPIC_MAX_BODY_BYTES + 1
    try:
        raw = request.get_data()
    except RequestEntityTooLarge:
        return None, _anthropic_too_large(request.content_length or 0)
    except Exception:
        # 断连（ClientDisconnected）等读取异常一律按"没有可用请求体"走视图的
        # 400 口径，保持与改动前一致，不回一页 HTML 错误页
        return {}, None
    if len(raw) > ANTHROPIC_MAX_BODY_BYTES:
        return None, _anthropic_too_large(len(raw))
    try:
        data = json.loads(raw)
    except Exception:
        return {}, None
    # 合法 JSON 但不是对象（null / [] / "str" / 5）：按缺参数口径拒，
    # 不能让视图里的 data.get(...) 抛 AttributeError 回一页 HTML 500
    return (data if isinstance(data, dict) else {}), None


def _anthropic_model_alias(model: str) -> str:
    """Anthropic 端点专用模型别名：config.json 的 anthropic_model_aliases 显式收编。

    客户端（Claude Code / Cline 的 Anthropic Provider 模式）内置发送的是官方型号名
    （claude-sonnet-4-5-20250929 等），本中转没有这些上游。映射规则：

    - 键为精确名（大小写不敏感）或尾部带 * 的前缀（如 "claude-*"）
    - 命中 → 换成中转真实模型名；未命中 → 原样返回，交回模型名守卫按 404 拒绝

    只在 /v1/messages 生效：OpenAI 端点（/v1/chat/completions）的模型名语义不动，
    避免别名把上游特征暴露口径搞乱。与 guard_rewrite_aliases 一致——收编必须
    写进 config，绝不靠猜（死模型要死得响亮，不许静默改写假活）。
    """
    if not model:
        return model
    aliases = load_config().get("anthropic_model_aliases") or {}
    if not isinstance(aliases, dict):
        return model
    low = model.lower()
    target = None
    for pattern, value in aliases.items():
        if not isinstance(pattern, str) or not isinstance(value, str):
            continue
        p, v = pattern.strip().lower(), value.strip()
        if not p or not v:
            continue
        if (p.endswith("*") and low.startswith(p[:-1])) or (not p.endswith("*") and low == p):
            target = v
            break
    if not target or target == model:
        return model
    log.info("Anthropic 模型别名: %s → %s（config.anthropic_model_aliases）", model, target)
    activity.add("info", f"Anthropic 别名 {model}→{target}", model=target, status="rewrite")
    return target


@bp.route("/v1/messages", methods=["POST"])
@require_api_key
def v1_anthropic_messages():
    """Anthropic Messages API 兼容入口（文本/图片/工具调用/流式）。

    适配层只翻译协议，实际仍走 _proxy_chat：因此角色路由、视觉代理、Cline
    官方通道、主账号池、外部 Provider、用量统计与冷却策略全部复用。
    """
    data, too_large = _anthropic_request_body()
    if too_large is not None:
        return too_large
    req_model = str(data.get("model") or "")
    if not req_model:
        return jsonify(anthropic.error_payload(
            "Missing required parameter: model", "invalid_request_error")), 400
    if not isinstance(data.get("messages"), list) or not data["messages"]:
        return jsonify(anthropic.error_payload(
            "Missing required parameter: messages", "invalid_request_error")), 400

    # 型号名别名（config.anthropic_model_aliases）→ 中转真实模型名；
    # 响应仍回显客户端请求的名字，客户端侧的模型校验不会因改写而失败
    mapped_model = _anthropic_model_alias(req_model)
    if mapped_model != req_model:
        data["model"] = mapped_model

    chat_data = anthropic.anthropic_to_chat(data)
    if not chat_data["messages"]:
        return jsonify(anthropic.error_payload(
            "messages contains no convertible content", "invalid_request_error")), 400

    if chat_data["stream"]:
        inner = _flask_response(_proxy_chat(chat_data))
        content_type = (inner.content_type or "").lower()
        if inner.status_code != 200 or "text/event-stream" not in content_type:
            return _anthropic_upstream_error(inner)

        def gen():
            bridge = anthropic.AnthropicSSEBridge(req_model)
            yield bridge.start()
            buf, done = "", False
            try:
                for piece in inner.response:
                    buf += piece.decode("utf-8", errors="replace")
                    while "\n" in buf:
                        line, buf = buf.split("\n", 1)
                        line = line.strip()
                        if not line.startswith("data:"):
                            continue
                        payload = line[5:].strip()
                        if payload == "[DONE]":
                            done = True
                            break
                        for ev in bridge.feed_payload(payload):
                            yield ev
                    if done:
                        break
            finally:
                try:
                    inner.close()
                except Exception:
                    pass
                for ev in bridge.finish():
                    yield ev

        return Response(stream_with_context(gen()), content_type="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    resp = _flask_response(_proxy_chat(chat_data))
    chat_json = resp.get_json(silent=True) or {}
    if resp.status_code != 200 or "choices" not in chat_json:
        return _anthropic_upstream_error(resp)
    return jsonify(anthropic.chat_to_anthropic(chat_json, req_model))


@bp.route("/v1/messages/count_tokens", methods=["POST"])
@require_api_key
def v1_anthropic_count_tokens():
    """Anthropic 计数端点（本地估算，不打上游、不记账）。

    Claude Code / Anthropic SDK 每次请求前都会调它做上下文预算；缺这个端点会
    404，客户端只能退化成保守截断甚至直接报错。上游没有 tokenizer 接口，故按
    anthropic.estimate_input_tokens 的口径本地估算（宁高不低）。
    """
    data, too_large = _anthropic_request_body()
    if too_large is not None:
        return too_large
    if not isinstance(data.get("messages"), list) or not data["messages"]:
        return jsonify(anthropic.error_payload(
            "Missing required parameter: messages", "invalid_request_error")), 400
    return jsonify({"input_tokens": anthropic.estimate_input_tokens(data)})


# ===================== /v1/embeddings 转发（白泽自研向量模型，2026-08-27） =====================
# qidi 的记忆向量检索（cf-memory）跟随默认模型走本中转，此路由把 /v1/embeddings
# 转发给本地白泽 S5 服务（.qidi/services/baize-embed/server.py，127.0.0.1:8800）。
# 上游地址在 config.json 的 "embedding_upstream" 配置；服务不可达时 qidi 自动降级 FTS。
# 切换到笔记本部署：把 base_url 改成 http://<笔记本IP>:8800/v1 即可。

EMBED_CFG = load_config().get("embedding_upstream") or {}


@bp.route("/v1/embeddings", methods=["POST"])
@require_api_key
def v1_embeddings():
    """转发到白泽 embedding 服务（qidi 记忆向量检索专用）。"""
    if not EMBED_CFG.get("base_url"):
        return jsonify({"error": {"message": "embedding upstream not configured",
                                  "type": "invalid_request_error"}}), 404
    data = request.get_json(force=True, silent=True) or {}
    upstream = EMBED_CFG["base_url"].rstrip("/") + "/embeddings"
    headers = {"Content-Type": "application/json"}
    if EMBED_CFG.get("api_key"):
        headers["Authorization"] = "Bearer " + EMBED_CFG["api_key"]
    try:
        with httpx.Client() as client:
            r = client.post(upstream, json=data, headers=headers,
                            timeout=EMBED_CFG.get("timeout", 60))
        return Response(r.content, status=r.status_code,
                        content_type="application/json")
    except Exception as e:
        log.warning("embedding upstream failed: %s", e)
        return jsonify({"error": {"message": f"embedding upstream failed: {e}",
                                  "type": "api_error"}}), 502

