# -*- coding: utf-8 -*-
"""antigravity2api.py — Google Antigravity Interactions API → OpenAI 兼容桥

协议（2026-10-20 实测逆向；当日 21:00 上游改口径后修订）：
  请求：POST https://generativelanguage.googleapis.com/v1beta/interactions
        {"agent": "antigravity-preview-latest",
         "environment": {"type": "remote"},
         "input": "<text>",
         "stream": false}
        ⚠ 2026-10-20 晚修订：antigravity-preview-* 是 **agent** 而非 model。
        旧写法 {"model": ...} 上游 400：
          "'antigravity-preview-latest' refers to an agent, but was provided in
           the 'model' field. Please use the 'agent' field instead."
        改 agent 后又要求 environment（EnvironmentConfig），且 sandbox 不支持：
          "Provide a 'type' field (for EnvironmentConfig) at 'environment'"
          "Supported values: 'remote'."
        结论：agent + environment.type=remote 是必需三件套。
        另注：非流式必须**显式** stream:false —— 省略时偶发 HTTP 500
        "Failed to provision environment"（显式流式反而正常）。
  响应（非流式）：{"id","status":"completed","usage","environment_id",
        "steps":[{"type":"model_output","content":[{"type":"text","text":"..."}]}],
        "agent"}
  响应（SSE）：interaction.created / status_update / step.start /
        step.delta{"delta":{"text"}} / step.stop / interaction.completed / done
成本注意：agent 自带约 4.9K prompt tokens 脚手架（实测 input_tokens≈4891，
即使输入只有 "OK"）。纯问答优先走 gs 通道（AI Studio 直连），本桥只用于
agent 专属能力。
上游 flaky（约四成断连/500/环境供给失败），内置重试。
出口：Google 国内直连不通，流量走 socks5://127.0.0.1:1080（首尔）。
"""
import io, json, os, sys, time, uuid

import httpx
from flask import Flask, Response, jsonify, request

BASE = os.path.dirname(os.path.abspath(__file__))
KEY_FILE = os.path.join(BASE, "antigravity_key.txt")
LOG_FILE = os.path.join(BASE, "antigravity2api.log")
API_URL = "https://generativelanguage.googleapis.com/v1beta/interactions"
PROXY_URL = "socks5://127.0.0.1:1080"
PORT = 8791
RETRIES = 4
MODELS = ["antigravity-preview-latest",
          "antigravity-preview-09-2026",
          "antigravity-preview-05-2026"]


def log(msg):
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + msg
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def api_key():
    k = os.environ.get("ANTIGRAVITY_API_KEY", "")
    if not k and os.path.exists(KEY_FILE):
        k = io.open(KEY_FILE, encoding="utf-8").read().strip()
    return k


def flatten(messages):
    """OpenAI messages → 单串 input（带角色标签，保留多轮上下文）。"""
    parts = []
    for m in messages or []:
        role = m.get("role", "user")
        content = m.get("content")
        if isinstance(content, list):  # 多模态分段取文本
            content = "".join(p.get("text", "") for p in content
                              if isinstance(p, dict))
        if content:
            parts.append("%s: %s" % (role, str(content).strip()))
    return "\n\n".join(parts) or "hi"


def upstream(body, timeout):
    """带重试的上游调用（preview 期 flaky）。返回 httpx.Response 或抛异常。"""
    h = {"Content-Type": "application/json"}
    key = api_key()
    if key:
        h["x-goog-api-key"] = key
    last = None
    for i in range(RETRIES):
        try:
            with httpx.Client(proxy=PROXY_URL, timeout=timeout,
                              trust_env=False) as c:
                r = c.post(API_URL, headers=h, json=body)
            # 2026-10-20 晚补：环境供给失败/上游 5xx 属 flaky，重试而非直接判负
            # （实测同一请求第二次即 200）。4xx 属请求形态问题，不重试。
            if r.status_code in (429, 500, 502, 503) or \
                    "Failed to provision environment" in r.text:
                log("retry %d: HTTP %s %s"
%                    (i + 1, r.status_code, " ".join(r.text.split())[:120]))
                last = RuntimeError("HTTP %s" % r.status_code)
                time.sleep(2 + i * 2)
                continue
            return r
        except Exception as e:
            last = e
            log("retry %d: %s" % (i + 1, str(e)[:80]))
            time.sleep(2 + i * 2)
    raise last


def to_openai(resp_json, model):
    """Interactions 非流式响应 → OpenAI chat.completion。"""
    text = ""
    for st in resp_json.get("steps") or []:
        if st.get("type") != "model_output":
            continue
        for p in st.get("content") or []:
            if isinstance(p, dict) and p.get("type") == "text":
                text += p.get("text", "")
    u = resp_json.get("usage") or {}
    return {
        "id": "chatcmpl-" + str(resp_json.get("id") or uuid.uuid4().hex)[:24],
        "object": "chat.completion",
        "created": int(time.time()),
        "model": resp_json.get("model") or model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": text},
            "finish_reason": "stop",
        }],
        "usage": {
            "prompt_tokens": u.get("total_input_tokens", 0),
            "completion_tokens": u.get("total_output_tokens", 0),
            "total_tokens": u.get("total_tokens", 0),
        },
    }


app = Flask(__name__)


@app.get("/health")
def health():
    return jsonify({"ok": True, "service": "antigravity2api",
                    "has_key": bool(api_key())})


@app.get("/v1/models")
def list_models():
    return jsonify({"object": "list", "data": [
        {"id": m, "object": "model", "owned_by": "google-antigravity"}
        for m in MODELS]})


def build_upstream_body(model, messages, stream=False):
    """OpenAI 请求 → Interactions 请求体（agent + environment 是必需三件套）。

    2026-10-20 晚上游改口径：antigravity-preview-* 指的是 agent 而非 model，
    写成 model 会被 400 拒绝；environment 必须给 type（唯一支持值 remote）；
    非流式要显式 stream:false（省略时偶发 500 Failed to provision environment）。
    """
    return {
        "agent": model,
        "environment": {"type": "remote"},
        "input": flatten(messages),
        "stream": bool(stream),
    }


@app.post("/v1/chat/completions")
def chat():
    data = request.get_json(force=True, silent=True) or {}
    model = str(data.get("model") or MODELS[0])
    if model not in MODELS:
        model = MODELS[0]
    # 2026-10-20 晚修订：agent + environment（type=remote）是必需三件套；
    # 非流式必须显式 stream:false（省略时上游偶发 500 provision 失败）。
    body = build_upstream_body(model, data.get("messages"))

    if data.get("stream"):
        def gen():
            cid = "chatcmpl-" + uuid.uuid4().hex[:24]

            def sse(obj):
                return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"

            yield sse({"id": cid, "object": "chat.completion.chunk",
                       "created": int(time.time()), "model": model,
                       "choices": [{"index": 0, "delta": {"role": "assistant"},
                                    "finish_reason": None}]})
            body["stream"] = True
            h = {"Content-Type": "application/json"}
            if api_key():
                h["x-goog-api-key"] = api_key()
            # 2026-10-20 晚修（两个旧 bug 同源）：
            #  ① `with resp:` —— httpx.Response 不是上下文管理器，必抛 TypeError，
            #    流式解析永远静默失败（只吐首尾两个空 chunk）；
            #  ② `with httpx.Client(...)` —— send(stream=True) 返回后立刻关客户端，
            #    连接随之关闭，iter_lines 必炸 WinError 10038。
            # 正确做法：client 活到读流结束，finally 里显式 close。
            resp = None
            client = httpx.Client(proxy=PROXY_URL, timeout=300, trust_env=False)
            try:
                for i in range(RETRIES):
                    try:
                        req = client.build_request("POST", API_URL,
                                                   headers=h, json=body)
                        resp = client.send(req, stream=True)
                        if resp.status_code != 200:
                            _code = resp.status_code
                            _body = " ".join((resp.text or "").split())[:160]
                            resp.close()
                            resp = None
                            log("stream upstream HTTP %s %s" % (_code, _body))
                            if _code in (429, 500, 502, 503):
                                time.sleep(2 + i * 2)
                                continue
                            break
                        break
                    except Exception as e:
                        log("stream retry %d: %s" % (i + 1, str(e)[:80]))
                        time.sleep(2 + i * 2)
                if resp is None:
                    yield sse({"error": "antigravity upstream failed"})
                    yield "data: [DONE]\n\n"
                    return
                event = None
                for line in resp.iter_lines():
                    if not line:
                        continue
                    if line.startswith("event: "):
                        event = line[7:].strip()
                    elif line.startswith("data: ") and event == "step.delta":
                        try:
                            d = json.loads(line[6:])
                            txt = ((d.get("delta") or {}).get("text"))
                            if txt:
                                yield sse({"id": cid,
                                           "object": "chat.completion.chunk",
                                           "created": int(time.time()),
                                           "model": model,
                                           "choices": [{"index": 0,
                                                        "delta": {"content": txt},
                                                        "finish_reason": None}]})
                        except ValueError:
                            pass
            except Exception as e:
                log("stream error: %s" % str(e)[:120])
            finally:
                try:
                    if resp is not None:
                        resp.close()
                except Exception:
                    pass
                try:
                    client.close()
                except Exception:
                    pass
            yield sse({"id": cid, "object": "chat.completion.chunk",
                       "created": int(time.time()), "model": model,
                       "choices": [{"index": 0, "delta": {},
                                    "finish_reason": "stop"}]})
            yield "data: [DONE]\n\n"
        return Response(gen(), content_type="text/event-stream")

    r = upstream(body, timeout=300)
    if r.status_code != 200:
        log("chat HTTP %s %s" % (r.status_code, " ".join(r.text.split())[:400]))
        return jsonify({"error": {"message": "antigravity upstream %s"
                                  % r.status_code}}), 502
    return jsonify(to_openai(r.json(), model))


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=PORT, threaded=True)
