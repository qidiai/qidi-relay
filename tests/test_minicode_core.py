# -*- coding: utf-8 -*-
"""MiniCode（mcode）通道核心逻辑的本地回归测试（不打真实上游）。"""

import json
import pathlib
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from proxy_core import minicode  # noqa: E402


def _auth_doc(access="mmoat_aaa", refresh="mmort_rrr", exp_ms=None):
    return {
        "schemaVersion": 1,
        "records": {
            "com.minimax.mcode.oauth.prod.cn\u0000abc": {
                "schemaVersion": 1,
                "accessToken": access,
                "refreshToken": refresh,
                "tokenType": "Bearer",
                "clientId": "mcode-public",
                "scopes": ["agent.default"],
                "audience": "agent-backend",
                "expiresAtMs": exp_ms if exp_ms is not None
                else int((time.time() + 3600) * 1000),
                "generation": 1,
                "loginEpoch": "epoch-1",
            },
        },
    }


class _BytesResponse:
    """模拟 httpx 流式响应（iter_bytes 逐块吐）。"""

    def __init__(self, chunks):
        self._chunks = chunks

    def iter_bytes(self):
        return iter(self._chunks)

    def close(self):
        pass


class _LinesResponse:
    """按 SSE 行吐字节的响应。"""

    def __init__(self, lines):
        self._lines = lines

    def iter_bytes(self):
        for l in self._lines:
            yield (l + "\n").encode("utf-8")

    def close(self):
        pass


# ─── 凭证解析 / 过期判定 ─────────────────────────────────────────────────────────

def test_parse_auth_json(tmp_path):
    p = tmp_path / "auth.json"
    p.write_text(json.dumps(_auth_doc()), encoding="utf-8")
    cred = minicode.parse_auth_json(str(p))
    assert cred.access_token == "mmoat_aaa"
    assert cred.refresh_token == "mmort_rrr"
    assert cred.record_key == "com.minimax.mcode.oauth.prod.cn\u0000abc"
    assert not cred.expired


def test_parse_auth_json_rejects_missing_tokens(tmp_path):
    doc = _auth_doc()
    doc["records"]["com.minimax.mcode.oauth.prod.cn\u0000abc"]["accessToken"] = ""
    p = tmp_path / "auth.json"
    p.write_text(json.dumps(doc), encoding="utf-8")
    try:
        minicode.parse_auth_json(str(p))
        assert False, "应抛 ValueError"
    except ValueError:
        pass


def test_credential_expired_respects_margin():
    future = int((time.time() * 1000) + minicode.TOKEN_EXPIRY_MARGIN_MS / 2)
    cred = minicode.MiniCodeCredential("a", "b", future)
    assert cred.expired  # 边际内视为已过期
    far = int((time.time() + 3600) * 1000)
    assert not minicode.MiniCodeCredential("a", "b", far).expired


# ─── 刷新：单飞 + 写回（generation 等字段原样保留） ─────────────────────────────

def test_refresh_serialized_and_writeback_preserves_fields(tmp_path, monkeypatch):
    """并发过期只放一个 refresh；写回只动三字段，generation/loginEpoch 不变。"""
    auth = tmp_path / "auth.json"
    doc = _auth_doc(access="mmoat_old", refresh="mmort_old",
                    exp_ms=int(time.time() * 1000) - 1000)  # 已过期
    auth.write_text(json.dumps(doc, indent=2), encoding="utf-8")

    calls = []

    class _Resp:
        status_code = 200
        text = "{}"

        def json(self):
            return {"access_token": "mmoat_new", "refresh_token": "mmort_new",
                    "expires_in": 3600}

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        time.sleep(0.08)
        assert url == minicode.TOKEN_URL_CN
        form = kwargs.get("data") or {}
        assert form["grant_type"] == "refresh_token"
        assert form["refresh_token"] == "mmort_old"
        assert form["client_id"] == "mcode-public"
        assert form["audience"] == "agent-backend"
        return _Resp()

    import httpx
    monkeypatch.setattr(httpx, "post", fake_post)

    ch = minicode.MiniCodeChannel("minicode", {"auth_path": str(auth)}, "主号")
    barrier = threading.Barrier(2)

    def worker(_i):
        barrier.wait(timeout=2)
        return ch.get_token()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(worker, range(2)))

    assert sorted(set(results)) == ["mmoat_new"]
    assert len(calls) == 1  # 单飞：只发一次刷新
    saved = json.loads(auth.read_text(encoding="utf-8"))
    rec = saved["records"]["com.minimax.mcode.oauth.prod.cn\u0000abc"]
    assert rec["accessToken"] == "mmoat_new"
    assert rec["refreshToken"] == "mmort_new"
    assert rec["generation"] == 1           # 不动
    assert rec["loginEpoch"] == "epoch-1"   # 不动
    assert saved["schemaVersion"] == 1      # 顶层结构不变
    assert rec["expiresAtMs"] > int(time.time() * 1000)


def test_refresh_locked_reuses_fresh_disk_cred(tmp_path, monkeypatch):
    """_refresh_locked 拿到过期的 old_rt，但磁盘凭证已被先到者/官方客户端刷新：
    直接复用磁盘凭证，不发 HTTP（防拿旧 refresh_token 撞轮换）。"""
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps(_auth_doc(access="mmoat_fresh")), encoding="utf-8")

    import httpx
    monkeypatch.setattr(httpx, "post", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("不应发起 HTTP 刷新")))

    ch = minicode.MiniCodeChannel("minicode", {"auth_path": str(auth)}, "主号")
    ch._load_cred()
    new = ch._refresh_locked("mmort_OUTDATED")  # 等锁者手里的旧 refresh_token
    assert new.access_token == "mmoat_fresh"


def test_mtime_reload_follows_external_refresh(tmp_path):
    """官方客户端刷新了文件（mtime 变化）：通道自动跟进新凭证。"""
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps(_auth_doc(access="mmoat_v1")), encoding="utf-8")
    ch = minicode.MiniCodeChannel("minicode", {"auth_path": str(auth)}, "主号")
    assert ch.get_token() == "mmoat_v1"
    # 外部（官方客户端）改写凭证
    time.sleep(0.02)
    doc = _auth_doc(access="mmoat_v2")
    auth.write_text(json.dumps(doc), encoding="utf-8")
    assert ch.get_token() == "mmoat_v2"


# ─── 请求体转换：OpenAI → Anthropic ────────────────────────────────────────────

def test_build_upstream_body_system_and_alias():
    data = {
        "model": "mcode-m3",
        "messages": [
            {"role": "system", "content": "你是助手"},
            {"role": "user", "content": "你好"},
        ],
        "max_tokens": 512,
    }
    body = minicode.build_upstream_body(data, "MiniMax-M3")
    assert body["model"] == "MiniMax-M3"
    assert body["system"] == "你是助手"
    assert body["max_tokens"] == 512
    assert body["messages"] == [{"role": "user",
                                "content": [{"type": "text", "text": "你好"}]}]


def test_build_upstream_body_tool_roundtrip_structure():
    data = {
        "model": "mcode-m3",
        "messages": [
            {"role": "user", "content": "查天气"},
            {"role": "assistant", "content": "好的",
             "tool_calls": [{"id": "call_1", "type": "function",
                             "function": {"name": "get_weather",
                                          "arguments": "{\"city\":\"北京\"}"}}]},
            {"role": "tool", "tool_call_id": "call_1", "content": "晴 25 度"},
            {"role": "user", "content": "谢谢"},
        ],
        "tools": [{"type": "function", "function": {
            "name": "get_weather", "description": "查天气",
            "parameters": {"type": "object", "properties": {"city": {"type": "string"}}}}}],
        "tool_choice": "auto",
    }
    body = minicode.build_upstream_body(data, "MiniMax-M3")
    msgs = body["messages"]
    # assistant: text + tool_use
    assert msgs[1]["role"] == "assistant"
    kinds = [b["type"] for b in msgs[1]["content"]]
    assert kinds == ["text", "tool_use"]
    assert msgs[1]["content"][1]["id"] == "call_1"
    assert msgs[1]["content"][1]["input"] == {"city": "北京"}
    # tool 结果并进下一条 user 的 tool_result，再接普通 user 追问
    assert msgs[2]["role"] == "user"
    assert msgs[2]["content"][0] == {"type": "tool_result", "tool_use_id": "call_1",
                                     "content": "晴 25 度"}
    assert msgs[2]["content"][1]["text"] == "谢谢"
    assert body["tools"][0]["name"] == "get_weather"
    assert body["tool_choice"] == {"type": "auto"}
    assert body["max_tokens"] == minicode.MAX_TOKENS_DEFAULT  # 未传时的默认


def test_build_upstream_body_effort_and_thinking():
    # off → thinking disabled（M3 实测可关）
    b1 = minicode.build_upstream_body(
        {"model": "mcode-m3", "messages": [{"role": "user", "content": "x"}],
         "reasoning_effort": "off"}, "MiniMax-M3")
    assert b1["thinking"] == {"type": "disabled"}
    # M3.1-Flash effort 档位 → output_config
    b2 = minicode.build_upstream_body(
        {"model": "mcode-m3.1-flash", "messages": [{"role": "user", "content": "x"}],
         "reasoning_effort": "low"}, "MiniMax-M3.1-Flash-Preview")
    assert b2.get("output_config") == {"effort": "low"}
    # M3 不发 output_config（目录未登记档位）
    b3 = minicode.build_upstream_body(
        {"model": "mcode-m3", "messages": [{"role": "user", "content": "x"}],
         "reasoning_effort": "low"}, "MiniMax-M3")
    assert "output_config" not in b3
    # 默认：不带 thinking 字段（服务端自适应）
    b4 = minicode.build_upstream_body(
        {"model": "mcode-m3", "messages": [{"role": "user", "content": "x"}]},
        "MiniMax-M3")
    assert "thinking" not in b4 and "output_config" not in b4


def test_build_upstream_body_tool_calls_dict_tolerance():
    """单 tool_call 对象直传 dict（非标准列表）时自动包装，tool_use 块不丢失
    （1001 实测：丢失会导致上游 tool_result 报 "tool id not found (2013)"）。"""
    data = {
        "model": "mcode-m3",
        "messages": [
            {"role": "user", "content": "查天气"},
            {"role": "assistant", "content": "",
             "tool_calls": {"id": "call_1", "type": "function",
                            "function": {"name": "get_weather",
                                          "arguments": "{\"city\":\"北京\"}"}}},
            {"role": "tool", "tool_call_id": "call_1", "content": "晴"},
        ],
    }
    body = minicode.build_upstream_body(data, "MiniMax-M3")
    assistant = body["messages"][1]
    assert assistant["role"] == "assistant"
    tool_uses = [b for b in assistant["content"] if b.get("type") == "tool_use"]
    assert len(tool_uses) == 1
    assert tool_uses[0]["id"] == "call_1"
    tool_results = [b for b in body["messages"][2]["content"]
                    if b.get("type") == "tool_result"]
    assert tool_results[0]["tool_use_id"] == "call_1"


def test_build_upstream_body_leading_assistant_gets_user_prefix():
    data = {"model": "mcode-m3", "messages": [
        {"role": "assistant", "content": "续聊开头"},
        {"role": "user", "content": "继续"},
    ]}
    body = minicode.build_upstream_body(data, "MiniMax-M3")
    assert body["messages"][0]["role"] == "user"
    assert body["messages"][1]["role"] == "assistant"
    assert body["messages"][2]["role"] == "user"


def test_build_upstream_body_image_data_url():
    data = {"model": "mcode-m3", "messages": [
        {"role": "user", "content": [
            {"type": "text", "text": "看图"},
            {"type": "image_url", "image_url": {
                "url": "data:image/png;base64,QUJD"}},
        ]},
    ]}
    body = minicode.build_upstream_body(data, "MiniMax-M3")
    img = body["messages"][0]["content"][1]
    assert img == {"type": "image", "source": {"type": "base64",
                                               "media_type": "image/png", "data": "QUJD"}}


# ─── 响应回转：Anthropic → OpenAI ────────────────────────────────────────────────

def test_message_to_chat_full():
    msg = {
        "id": "msg_01", "type": "message", "role": "assistant",
        "model": "MiniMax-M3",
        "content": [
            {"type": "thinking", "thinking": "思考中"},
            {"type": "text", "text": "答案是 42"},
            {"type": "tool_use", "id": "call_9", "name": "calc",
             "input": {"q": "6*7"}},
        ],
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }
    out = minicode.message_to_chat(msg, "mcode-m3")
    m = out["choices"][0]["message"]
    assert m["content"] == "答案是 42"
    assert m["reasoning_content"] == "思考中"
    assert m["tool_calls"][0]["function"]["name"] == "calc"
    assert json.loads(m["tool_calls"][0]["function"]["arguments"]) == {"q": "6*7"}
    assert out["choices"][0]["finish_reason"] == "tool_calls"
    assert out["usage"] == {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    assert out["model"] == "mcode-m3"  # 记账名 = 客户端请求名
    assert out["reasoning_used_as_content"] is False


def test_message_to_chat_thinking_only_fallback():
    msg = {"id": "m1", "content": [{"type": "thinking", "thinking": "只有思考"}],
           "stop_reason": "end_turn", "usage": {"input_tokens": 3, "output_tokens": 4}}
    out = minicode.message_to_chat(msg, "mcode-m3")
    m = out["choices"][0]["message"]
    assert m["content"] == "只有思考"
    assert out["reasoning_used_as_content"] is True


def test_stream_wrap_text_and_thinking():
    lines = [
        'event: message_start',
        'data: {"type":"message_start","message":{"id":"msg_s","model":"MiniMax-M3",'
        '"usage":{"input_tokens":12}}}',
        'data: {"type":"ping"}',
        'data: {"type":"content_block_start","index":0,'
        '"content_block":{"type":"thinking","thinking":""}}',
        'data: {"type":"content_block_delta","index":0,'
        '"delta":{"type":"thinking_delta","thinking":"想一下"}}',
        'data: {"type":"content_block_start","index":1,'
        '"content_block":{"type":"text","text":""}}',
        'data: {"type":"content_block_delta","index":1,'
        '"delta":{"type":"text_delta","text":"你好"}}',
        'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},'
        '"usage":{"output_tokens":7}}',
        'data: {"type":"message_stop"}',
    ]
    raw = b"".join(minicode.MiniCodeStreamWrap(_LinesResponse(lines), "mcode-m3")
                   .iter_bytes()).decode("utf-8")
    datas = [l[6:] for l in raw.split("\n") if l.startswith("data: ")]
    objs = [json.loads(d) for d in datas if d != "[DONE]"]
    assert raw.endswith("data: [DONE]\n\n")
    assert objs[0]["choices"][0]["delta"] == {"role": "assistant"}
    texts = "".join(o["choices"][0]["delta"].get("content", "")
                    for o in objs if o["choices"][0]["delta"].get("content"))
    thinks = "".join(o["choices"][0]["delta"].get("reasoning_content", "")
                     for o in objs if o["choices"][0]["delta"].get("reasoning_content"))
    assert texts == "你好"
    assert thinks == "想一下"
    final = objs[-1]
    assert final["choices"][0]["finish_reason"] == "stop"
    assert final["usage"] == {"prompt_tokens": 12, "completion_tokens": 7, "total_tokens": 19}
    assert all(o["model"] == "mcode-m3" for o in objs)  # 注水检测口径


def test_stream_wrap_tool_use_and_chunked_lines():
    """工具调用事件 + 跨 chunk 撕裂行的容错。"""
    sse = (
        'event: message_start\ndata: {"type":"message_start","message":{"id":"msg_t",'
        '"usage":{"input_tokens":5}}}\n\n'
        'event: content_block_start\ndata: {"type":"content_block_start","index":0,'
        '"content_block":{"type":"tool_use","id":"call_1","name":"get_weather","input":{}}}\n\n'
        'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,'
        '"delta":{"type":"input_json_delta","partial_json":"{\\"city\\":"}}\n\n'
        'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,'
        '"delta":{"type":"input_json_delta","partial_json":"\\"北京\\"}"}}\n\n'
        'event: message_delta\ndata: {"type":"message_delta",'
        '"delta":{"stop_reason":"tool_use"},"usage":{"output_tokens":3}}\n\n'
        'event: message_stop\ndata: {"type":"message_stop"}\n\n'
    )
    # 按 7 字节一块撕裂，验证跨 chunk 行缓冲
    chunks = [sse[i:i + 7].encode("utf-8") for i in range(0, len(sse), 7)]
    raw = b"".join(minicode.MiniCodeStreamWrap(_BytesResponse(chunks), "mcode-m3")
                   .iter_bytes()).decode("utf-8")
    objs = [json.loads(l[6:]) for l in raw.split("\n")
            if l.startswith("data: ") and l[6:] != "[DONE]"]
    tool_chunks = [o["choices"][0]["delta"]["tool_calls"] for o in objs
                   if o["choices"][0]["delta"].get("tool_calls")]
    assert tool_chunks[0][0]["function"]["name"] == "get_weather"
    assert tool_chunks[0][0]["id"] == "call_1"
    args = "".join(tc[0]["function"]["arguments"] for tc in tool_chunks[1:])
    assert json.loads(args) == {"city": "北京"}
    assert objs[-1]["choices"][0]["finish_reason"] == "tool_calls"
    assert objs[-1]["usage"]["total_tokens"] == 8


def test_aggregate_stream_full_round():
    lines = [
        'data: {"type":"message_start","message":{"id":"msg_a","usage":{"input_tokens":9}}}',
        'data: {"type":"content_block_start","index":0,"content_block":{"type":"thinking","thinking":""}}',
        'data: {"type":"content_block_delta","index":0,"delta":{"type":"thinking_delta","thinking":"推理"}}',
        'data: {"type":"content_block_start","index":1,"content_block":{"type":"text","text":""}}',
        'data: {"type":"content_block_delta","index":1,"delta":{"type":"text_delta","text":"回复"}}',
        'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":6}}',
        'data: {"type":"message_stop"}',
    ]
    out = minicode.aggregate_stream(_LinesResponse(lines), "mcode-m3")
    m = out["choices"][0]["message"]
    assert m["content"] == "回复"
    assert m["reasoning_content"] == "推理"
    assert out["usage"] == {"prompt_tokens": 9, "completion_tokens": 6, "total_tokens": 15}
    assert out["model"] == "mcode-m3"
    assert out["choices"][0]["finish_reason"] == "stop"


def test_parse_cooldown_seconds():
    assert minicode.parse_cooldown_seconds("Try again in 2h 51m") == 2 * 3600 + 51 * 60
    assert minicode.parse_cooldown_seconds("Try again in 45s") == 45
    assert minicode.parse_cooldown_seconds("稍后再试", 429) == minicode.DAILY_LIMIT_DEFAULT_S
    assert minicode.parse_cooldown_seconds("再试 1 小时 30 分") == 5400


# ─── 通道注册表 ─────────────────────────────────────────────────────────────────

def test_reload_channels_and_pick(tmp_path):
    a1 = tmp_path / "a1.json"
    a2 = tmp_path / "a2.json"
    a1.write_text(json.dumps(_auth_doc(access="t1")), encoding="utf-8")
    a2.write_text(json.dumps(_auth_doc(access="t2")), encoding="utf-8")
    cfg = {"providers": {"minicode": {
        "type": "minicode",
        "auth_paths": [
            {"path": str(a1), "label": "甲"},
            {"path": str(a2), "label": "乙"},
        ],
        "models": ["mcode-m3"],
    }}}
    minicode.reload_channels_from_config(cfg)
    chs = minicode.channels_for("minicode")
    assert [c.label for c in chs] == ["甲", "乙"]
    picked = minicode.pick_channel("minicode")
    assert picked is not None
    picked.set_cooldown(300, "test")
    assert minicode.pick_channel("minicode").label != picked.label  # 冷却中跳过
    assert minicode.pick_channel("minicode", exclude={"乙"}) is None  # 排除后无可用
