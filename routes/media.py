"""媒体透传路由：图像生成 / 视频生成 / 视频轮询。

统一让所有 Agnes 系媒体调用（文本/图像/视频）走云端 relay，
按 key 的 RPM 智能切换，避免打到限流。

端点
====
- ``POST /v1/images/generations``  图像生成（透传上游 body，不改写 extra_body/size/mode）
- ``POST /v1/videos``              视频生成
- ``GET  /agnesapi``              视频任务轮询（query 的 video_id / model_name 原样带上）

鉴权：复用 ``routes.proxy.require_api_key``（与 chat 一致）。
模型 → 渠道：``providers.match(model)``。
key 选择：``proxy_core.key_scheduler.scheduler``（RPM 滑动窗口 + 429 冷却）。
上游 429：换下一把 key 重试（最多 len(keys) 次）；全 key 429 → 本端 429。
超时：images 180s / videos 120s / agnesapi 60s。
日志：``media <path> model=<> key=<> status=<> ms=``
"""

import json
import logging
import time

import httpx
from flask import Blueprint, Response, jsonify, request

from proxy_core import providers
from proxy_core.key_scheduler import scheduler
from proxy_core.settings import load_config
from routes.proxy import require_api_key

log = logging.getLogger("qidi-relay")

bp = Blueprint("media", __name__)

# 各端点超时（秒）
_IMAGES_TIMEOUT_S = 180
_VIDEOS_TIMEOUT_S = 120
_AGNESAPI_TIMEOUT_S = 60

# 429 响应 body
_RATE_LIMIT_BODY = {
    "error": {
        "message": "all keys rate-limited",
        "code": "key_rate_limited",
    }
}


def _openai_error_body(message: str, err_type: str, code: str, status: int = 404) -> Response:
    """OpenAI 兼容错误响应（与 routes/proxy._openai_error 同形状，独立实现避免跨模块私有导入）。"""
    resp = jsonify({
        "error": {
            "message": message,
            "type": err_type,
            "param": None,
            "code": code,
        }
    })
    resp.status_code = status
    return resp


# ── 错误码归一化（2026-10-22）────────────────────────────────
# 依据 Agnes 官方 docs/ERROR_CODES.md：上游的错误语义与 OpenAI 并不完全一致
# （例如 401 表示「我们配置的 provider key 失效」，对客户端应是网关错误 502，
# 而不是 401 让客户端怀疑自己的 key）。这里做一层收敛，同时**保留原始信息**
# 在 error.upstream 里，便于排查且不做静默改写。
_UPSTREAM_ERROR_MAP = {
    400: (400, "invalid_request_error", "invalid_request_error"),
    401: (502, "provider_error", "provider_auth_failed"),
    402: (502, "provider_error", "provider_quota_exceeded"),
    403: (502, "provider_error", "provider_access_denied"),
    404: (404, "invalid_request_error", "model_or_task_not_found"),
    429: (429, "rate_limit_error", "key_rate_limited"),
    500: (502, "provider_error", "provider_internal_error"),
    503: (502, "provider_error", "provider_unavailable"),
}


def normalize_upstream_error(status: int, body: bytes) -> tuple:
    """把上游错误体归一化为 OpenAI 风格。

    Returns:
        (out_status, out_body_bytes, out_content_type)

    规则：
    - status < 400 → 原样返回
    - 非 JSON 体 → 原样返回（不做臆测解析）
    - JSON 体 → 输出
      ``{"error": {"message","type","code","upstream": {"status","code","message"}}}``
      其中 ``upstream`` 保留 Agnes 原始 code/message；对外状态码按上表收敛
      （401/402/403/5xx → 502，避免误导客户端）。
    """
    if status < 400:
        return status, body, "application/json"

    try:
        payload = json.loads(body.decode("utf-8", "replace"))
    except Exception:
        return status, body, "application/json"

    err = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(err, dict):
        return status, body, "application/json"

    orig_code = err.get("code")
    orig_msg = err.get("message") or ""

    if status == 404 and isinstance(orig_msg, str) and "任务不存在" in orig_msg:
        out_status, err_type, code = 404, "invalid_request_error", "task_not_found"
    else:
        out_status, err_type, code = _UPSTREAM_ERROR_MAP.get(
            status, (502 if status >= 500 else status, "provider_error", "upstream_error"))

    normalized = {
        "error": {
            "message": orig_msg or ("upstream returned %d" % status),
            "type": err_type,
            "param": None,
            "code": code,
            "upstream": {
                "provider": "agnes",
                "status": status,
                "code": orig_code,
                "message": orig_msg,
            },
        }
    }
    return out_status, json.dumps(normalized, ensure_ascii=False).encode("utf-8"), "application/json"


def _model_not_found_response(model: str) -> Response:
    """未知模型 404（与 chat 的 model_not_found 风格一致）。"""
    return _openai_error_body(
        f"The model '{model}' does not exist on this relay. "
        "Available models: see GET /v1/models.",
        "invalid_request_error",
        "model_not_found",
        status=404,
    )


def _get_model_from_body() -> str:
    """POST body 的 model 字段。"""
    data = request.get_json(force=True, silent=True) or {}
    return str(data.get("model") or "")


def _get_model_from_query() -> str:
    """GET query 的 model_name 字段（/agnesapi 用）。"""
    return str(request.args.get("model_name") or "")


def _provider_keys(prov: dict) -> list:
    """取 provider 的 key 池（providers._parse_keys 产物，已是 dict 列表）。"""
    return prov.get("keys") or []


def _pick_next_key(provider_name: str, keys: list, exclude: set) -> dict | None:
    """从调度器选下一把可用 key；exclude 里已试过的跳过。"""
    candidates = [k for k in keys if k.get("key") not in exclude]
    if not candidates:
        return None
    return scheduler.pick(provider_name, candidates)


def _upstream_request(
    method: str,
    url: str,
    *,
    prov: dict,
    entry: dict,
    body: dict | None,
    timeout_s: int,
) -> httpx.Response:
    """发起一次上游请求（非流式，返回完整 Response 供原样回传）。"""
    client = providers.get_client(prov["name"], prov.get("proxy_url"))
    headers = {
        "Authorization": f"Bearer {entry['key']}",
        "Content-Type": "application/json",
    }
    if method.upper() == "GET":
        return client.get(url, headers=headers, timeout=timeout_s)
    return client.post(url, json=body, headers=headers, timeout=timeout_s)


def _rate_limited_response() -> Response:
    """全部 key 限流时返回的 429。"""
    resp = jsonify(_RATE_LIMIT_BODY)
    resp.status_code = 429
    resp.headers["Retry-After"] = "5"
    return resp


def _forward_media(
    upstream_path: str,
    model: str,
    body: dict | None,
    timeout_s: int,
    is_get: bool = False,
    query_string: str = "",
):
    """通用媒体转发逻辑（图像/视频 POST、轮询 GET 共用）。

    Returns:
        Flask Response（原样回传上游 status / Content-Type / body），
        或本地错误响应（404 未知模型 / 429 全 key 限流）。
    """
    started = time.perf_counter()
    prov = providers.match(model)
    if prov is None:
        return _model_not_found_response(model)

    pname = prov["name"]
    keys = _provider_keys(prov)
    if not keys:
        # provider 无 key 池（如 atomcode/cline 通道）——媒体调用不适用，直接 503
        log.warning("media %s model=%s 无可用 key 池", upstream_path, model)
        return _openai_error_body(
            f"provider '{pname}' has no static key pool for media requests",
            "proxy_config_error", "no_media_keys", status=503,
        )

    base_url = prov["base_url"].rstrip("/")
    if is_get:
        # GET /agnesapi?video_id=...&model_name=...（query 原样透传）
        url = f"{base_url}/agnesapi{query_string}"
        method = "GET"
    else:
        url = f"{base_url}{upstream_path}"
        method = "POST"

    exclude: set = set()
    last_resp: httpx.Response | None = None
    max_attempts = len(keys)

    for _ in range(max_attempts):
        entry = _pick_next_key(pname, keys, exclude)
        if entry is None:
            # 调度器判定全 key 满（RPM 或冷却）→ 429
            latency_ms = int((time.perf_counter() - started) * 1000)
            log.info("media %s model=%s key=all_limited status=429 ms=%d",
                     upstream_path, model, latency_ms)
            return _rate_limited_response()

        key_label = entry.get("label") or pname
        exclude.add(entry.get("key"))
        try:
            resp = _upstream_request(
                method, url,
                prov=prov, entry=entry, body=body, timeout_s=timeout_s,
            )
        except Exception as e:
            log.error("media %s model=%s key=%s 连接异常: %s",
                      upstream_path, model, key_label, e)
            # 连接异常不计入 429 冷却，只排除该 key 再试下一把
            continue

        last_resp = resp
        status = resp.status_code
        latency_ms = int((time.perf_counter() - started) * 1000)

        # 上游 429：记冷却 + 换下一把 key
        if status == 429:
            scheduler.record(pname, entry, ok=False, status=429)
            log.warning("media %s model=%s key=%s 上游 429，换下一把",
                        upstream_path, model, key_label)
            resp.close()
            continue

        # 其它状态原样回传
        scheduler.record(pname, entry, ok=status < 400)
        log.info("media %s model=%s key=%s status=%d ms=%d",
                 upstream_path, model, key_label, status, latency_ms)

        ct = resp.headers.get("Content-Type") or "application/json"
        resp_body = resp.content
        resp.close()
        # 错误码归一化（成功/非 JSON 原样透传）
        out_status, out_body, out_ct = normalize_upstream_error(status, resp_body)
        return Response(out_body, status=out_status, content_type=out_ct)

    # 所有 key 都 429（或全异常）
    if last_resp is not None and last_resp.status_code == 429:
        latency_ms = int((time.perf_counter() - started) * 1000)
        log.info("media %s model=%s all_keys_429 ms=%d", upstream_path, model, latency_ms)
        last_resp.close()
        return _rate_limited_response()

    # 全 key 连接异常
    latency_ms = int((time.perf_counter() - started) * 1000)
    log.error("media %s model=%s 所有 key 连接异常 ms=%d", upstream_path, model, latency_ms)
    return _openai_error_body(
        f"all keys for provider '{pname}' failed to connect",
        "proxy_upstream_error", "media_connect_failed", status=502,
    )


# ── 路由视图 ──────────────────────────────────────────────────

@bp.route("/v1/images/generations", methods=["POST"])
@require_api_key
def images_generations():
    """图像生成透传：body 原样转发，不改写 extra_body / size / mode。"""
    model = _get_model_from_body()
    if not model:
        return _openai_error_body(
            "Missing required parameter: model",
            "invalid_request_error", "missing_model", status=400,
        )
    body = request.get_json(force=True, silent=True)
    return _forward_media(
        upstream_path="/images/generations",
        model=model,
        body=body,
        timeout_s=_IMAGES_TIMEOUT_S,
    )


@bp.route("/v1/videos", methods=["POST"])
@require_api_key
def videos():
    """视频生成透传。"""
    model = _get_model_from_body()
    if not model:
        return _openai_error_body(
            "Missing required parameter: model",
            "invalid_request_error", "missing_model", status=400,
        )
    body = request.get_json(force=True, silent=True)
    return _forward_media(
        upstream_path="/videos",
        model=model,
        body=body,
        timeout_s=_VIDEOS_TIMEOUT_S,
    )


@bp.route("/agnesapi", methods=["GET"])
@require_api_key
def agnesapi():
    """视频任务轮询：query 的 video_id / model_name 原样带上。"""
    model = _get_model_from_query()
    if not model:
        return _openai_error_body(
            "Missing required query parameter: model_name",
            "invalid_request_error", "missing_model_name", status=400,
        )
    # query 原样透传（保留 video_id、model_name 等所有 query 参数）
    query_string = ("?" + request.query_string.decode("utf-8")) if request.query_string else ""
    return _forward_media(
        upstream_path="/agnesapi",
        model=model,
        body=None,
        timeout_s=_AGNESAPI_TIMEOUT_S,
        is_get=True,
        query_string=query_string,
    )
