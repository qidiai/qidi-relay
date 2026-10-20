# -*- coding: utf-8 -*-
"""antigravity2api 桥的协议回归测试（零网络）。

背景：2026-10-20 晚 Google 改口径，桥的旧请求体（model 字段）被上游 400；
同时流式路径有两个致命 bug（`with resp:` TypeError、client 提前关闭）。
本文件钉住三条**不能回退**的契约：

1. 请求体必须是 agent + environment.type=remote，且非流式显式 stream:false
   （写成 model 字段或省略 environment 都会被上游 400/500）；
2. to_openai 能把 Interactions 的 steps 形态翻译成 OpenAI chat.completion；
3. 流式分支的源码结构不得再出现 `with resp:` / `with httpx.Client(`（
   这两个写法在流式场景下必炸）——用源码断言防回归。
"""

import pathlib
import sys

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import antigravity2api as ag  # noqa: E402


# ── 1. 请求体契约 ─────────────────────────────────────────────────────
def test_upstream_body_uses_agent_and_environment():
    body = ag.build_upstream_body("antigravity-preview-latest", [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "hi"},
    ])
    assert body["agent"] == "antigravity-preview-latest"
    assert "model" not in body, "antigravity-preview-* 是 agent，不能放 model 字段"
    assert body["environment"] == {"type": "remote"}
    assert body["stream"] is False, "非流式必须显式 stream:false（省略会偶发 500）"
    assert "system: be brief" in body["input"] and "user: hi" in body["input"]


def test_upstream_body_stream_true():
    assert ag.build_upstream_body("antigravity-preview-09-2026", [], stream=True)["stream"] is True


def test_flatten_multimodal_parts_and_default():
    flat = ag.flatten([{"role": "user", "content": [
        {"type": "text", "text": "看图"},
        {"type": "image_url", "image_url": {"url": "data:x"}},
    ]}])
    assert "看图" in flat
    assert ag.flatten([]) == "hi"      # 空输入兜底


# ── 2. 响应翻译契约 ───────────────────────────────────────────────────
def test_to_openai_maps_steps_and_usage():
    resp = {
        "id": "v1_abc",
        "model": None,
        "status": "completed",
        "usage": {"total_input_tokens": 4891, "total_output_tokens": 1, "total_tokens": 4892},
        "steps": [
            {"type": "model_output", "content": [{"type": "text", "text": "OK"}]},
            {"type": "model_output", "content": [{"type": "text", "text": "!"}]},
        ],
        "agent": "antigravity-preview-latest",
    }
    out = ag.to_openai(resp, "antigravity-preview-latest")
    assert out["object"] == "chat.completion"
    assert out["model"] == "antigravity-preview-latest"
    assert out["choices"][0]["message"]["content"] == "OK!"
    assert out["choices"][0]["finish_reason"] == "stop"
    assert out["usage"] == {"prompt_tokens": 4891, "completion_tokens": 1, "total_tokens": 4892}


def test_to_openai_ignores_non_text_steps():
    out = ag.to_openai({"steps": [{"type": "tool_call"}, {"type": "model_output", "content": []}]}, "m")
    assert out["choices"][0]["message"]["content"] == ""


# ── 3. 流式分支源码守卫（防两个旧 bug 回归）────────────────────────────
def test_stream_branch_has_no_context_manager_bugs():
    src = (BASE / "antigravity2api.py").read_text(encoding="utf-8")
    # 只截流式分支本体：非流式的 upstream() 用 with httpx.Client 是**正确**的
    # （响应用完即关），风险只在"边读流边关连接"的流式路径。
    start = src.index('if data.get("stream"):')
    end = src.index('return Response(gen(), content_type="text/event-stream")')
    branch = src[start:end]
    # 修复说明的注释里会引用旧写法作反例，只检查代码行
    code = "\n".join(ln for ln in branch.splitlines() if not ln.strip().startswith("#"))
    assert "with resp:" not in code, "httpx.Response 不是上下文管理器"
    assert "with httpx.Client(" not in code, "流式时 client 提前关闭会让 iter_lines 炸"
    assert "client.close()" in code, "必须显式关闭流式 client"
    assert 'event == "step.delta"' in code
