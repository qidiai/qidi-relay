# -*- coding: utf-8 -*-
"""Cline 通道核心逻辑的本地回归测试（不访问真实上游）。"""

import json
import pathlib
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from proxy_core import cline  # noqa: E402


class _SSEResponse:
    def __init__(self, lines):
        self._lines = lines

    def iter_lines(self):
        return iter(self._lines)


def test_aggregate_stream_preserves_tool_calls_and_reasoning_content():
    """免费通道“非流式→上游流式→聚合”不得丢 tool_calls / reasoning_content。"""
    chunks = [
        'data: {"id":"chat-1","choices":[{"index":0,"delta":{"role":"assistant",'
        '"tool_calls":[{"index":0,"id":"call_1","type":"function",'
        '"function":{"name":"read_file","arguments":"{\\"path\\""}}],'
        '"reasoning_content":"先想"}}]}',
        'data: {"id":"chat-1","choices":[{"index":0,"delta":{"tool_calls":['
        '{"index":0,"function":{"arguments":":\\"a.txt\\"}"}}]}}]}',
        'data: {"id":"chat-1","choices":[{"index":0,"finish_reason":"tool_calls"}],'
        '"usage":{"prompt_tokens":7,"completion_tokens":3,"total_tokens":10}}',
        'data: [DONE]',
    ]
    result = cline.aggregate_stream(_SSEResponse(chunks), "cline-free/test")
    choice = result["choices"][0]
    msg = choice["message"]
    assert choice["finish_reason"] == "tool_calls"
    assert msg["reasoning"] == "先想"
    assert msg["reasoning_content"] == "先想"
    assert msg["tool_calls"] == [{
        "id": "call_1",
        "type": "function",
        "function": {"name": "read_file", "arguments": '{"path":"a.txt"}'},
    }]
    assert result["usage"]["total_tokens"] == 10
    assert result["reasoning_used_as_content"] is False


def test_aggregate_stream_reasoning_content_fallback_still_marks_bad_response():
    """只有 reasoning_content 没有 content 时，仍按坏响应标记供路由层换号。"""
    chunks = [
        'data: {"id":"chat-2","choices":[{"delta":{"reasoning_content":"只有思考"}}]}',
        'data: [DONE]',
    ]
    result = cline.aggregate_stream(_SSEResponse(chunks), "cline-free/test")
    msg = result["choices"][0]["message"]
    assert msg["content"] == "只有思考"
    assert msg["reasoning_content"] == "只有思考"
    assert result["reasoning_used_as_content"] is True


def test_refresh_is_serialized_across_threads(tmp_path, monkeypatch):
    """并发发现 token 过期时只允许一次 refresh，避免 refreshToken 双刷轮换。"""
    auth = tmp_path / "providers.json"
    auth.write_text(json.dumps({
        "providers": {"cline": {"settings": {"auth": {
            "accessToken": "workos:old-token",
            "refreshToken": "old-refresh",
            "expiresAt": int(time.time() * 1000) - 1000,
        }}}}
    }), encoding="utf-8")

    calls = []

    class _RefreshResponse:
        status_code = 200
        text = "{}"

        def json(self):
            return {"data": {
                "accessToken": "workos:new-token",
                "refreshToken": "new-refresh",
                "expiresAt": int((time.time() + 3600) * 1000),
            }}

    def fake_post(*args, **kwargs):
        calls.append((args, kwargs))
        time.sleep(0.08)  # 放大并发窗口；无互斥时第二个线程也会进这里
        return _RefreshResponse()

    import httpx
    monkeypatch.setattr(httpx, "post", fake_post)

    ch = cline.ClineChannel("cline", {"auth_path": str(auth)}, "主号")
    barrier = threading.Barrier(2)

    def worker():
        barrier.wait(timeout=2)
        return ch.get_token()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _i: worker(), range(2)))

    assert results == ["new-token", "new-token"]
    assert len(calls) == 1
    saved = json.loads(auth.read_text(encoding="utf-8"))
    node = saved["providers"]["cline"]["settings"]["auth"]
    assert node["accessToken"] == "workos:new-token"
    assert node["refreshToken"] == "new-refresh"
