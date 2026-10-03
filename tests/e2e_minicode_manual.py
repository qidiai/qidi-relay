# -*- coding: utf-8 -*-
"""MiniCode 通道端到端手动回归（需要先起开发实例：RELAY_PORT=8770 python main.py）。

非 pytest 自动用例（文件名不带 test_ 前缀，pytest 不会收集）。
覆盖：模型目录 / 非流式 / 流式 / 工具调用 / 多轮工具回传 / MiniMax-M3 别名防冲突。

用法：python tests/e2e_minicode_manual.py
"""
import json
import sys
import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8770"
KEY = ""  # 8770 若配置了 api_keys 则填
H = {"Content-Type": "application/json"}
if KEY:
    H["Authorization"] = f"Bearer {KEY}"


def check(name, ok, detail=""):
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {name} {detail}")
    return ok


fails = 0

# 1) /v1/models 应列出 mcode-* 模型
r = httpx.get(BASE + "/v1/models", timeout=15)
ids = [m["id"] for m in r.json().get("data", [])] if r.status_code == 200 else []
mc = [m for m in ids if m.startswith("mcode-")]
print(f"/v1/models HTTP {r.status_code}, mcode 模型: {mc}")
fails += not check("模型目录含 mcode-*", bool(mc), str(mc))

# 2) 非流式
r = httpx.post(BASE + "/v1/chat/completions", headers=H, timeout=120, json={
    "model": "mcode-m3.1-flash",
    "messages": [{"role": "system", "content": "你是测试助手"},
                 {"role": "user", "content": "只回复四个字：非流式通"}],
    "max_tokens": 512,
})
ok = r.status_code == 200
out = r.json() if ok else {}
msg = (out.get("choices") or [{}])[0].get("message", {})
content = msg.get("content")
usage = out.get("usage")
print(f"非流式 HTTP {r.status_code} content={content!r} usage={usage}")
fails += not check("非流式 200 且有 content", ok and bool(content))
fails += not check("非流式 usage 记账", bool(usage and usage.get("total_tokens", 0) > 0), str(usage))

# 3) 流式（SSE）
text_parts = []
final_usage = None
with httpx.stream("POST", BASE + "/v1/chat/completions", headers=H, timeout=180, json={
    "model": "mcode-m3",
    "messages": [{"role": "user", "content": "只回复四个字：流式成功"}],
    "max_tokens": 1024, "stream": True,
}) as r:
    print(f"流式 HTTP {r.status_code}")
    buf = ""
    for chunk in r.iter_text():
        buf += chunk
        while "\n" in buf:
            line, buf = buf.split("\n", 1)
            s = line.strip()
            if s.startswith("data:"):
                p = s[5:].strip()
                if p == "[DONE]":
                    continue
                try:
                    o = json.loads(p)
                    d = (o.get("choices") or [{}])[0].get("delta", {})
                    if d.get("content"):
                        text_parts.append(d["content"])
                    if o.get("usage"):
                        final_usage = o["usage"]
                except Exception:
                    pass
text = "".join(text_parts)
print(f"流式聚合 content={text!r} usage={final_usage}")
fails += not check("流式 200 且有 content", bool(text.strip()))
fails += not check("流式 usage 末块", bool(final_usage and final_usage.get("total_tokens", 0) > 0), str(final_usage))

# 4) 工具调用（非流式）
r = httpx.post(BASE + "/v1/chat/completions", headers=H, timeout=120, json={
    "model": "mcode-m3",
    "messages": [{"role": "user", "content": "用工具查北京天气，工具返回后直接说：北京晴"}],
    "tools": [{"type": "function", "function": {
        "name": "get_weather", "description": "查询城市当前天气",
        "parameters": {"type": "object", "properties": {"city": {"type": "string"}},
                       "required": ["city"]}}}],
    "max_tokens": 1024,
})
out = r.json() if r.status_code == 200 else {}
msg = (out.get("choices") or [{}])[0].get("message", {})
tcs = msg.get("tool_calls") or []
finish = (out.get("choices") or [{}])[0].get("finish_reason")
print(f"工具调用 HTTP {r.status_code} finish={finish} tool_calls={tcs}")
fails += not check("工具调用返回 tool_calls", bool(tcs) and finish == "tool_calls")

# 5) 多轮工具结果回传
if tcs:
    tc = tcs[0]
    msgs = [
        {"role": "user", "content": "用工具查北京天气"},
        {"role": "assistant", "content": msg.get("content") or "",
         "tool_calls": [tc]},
        {"role": "tool", "tool_call_id": tc["id"], "content": "晴，25 度"},
    ]
    r = httpx.post(BASE + "/v1/chat/completions", headers=H, timeout=120, json={
        "model": "mcode-m3", "messages": msgs, "max_tokens": 512})
    out2 = r.json() if r.status_code == 200 else {}
    c2 = (out2.get("choices") or [{}])[0].get("message", {}).get("content")
    print(f"工具回传 HTTP {r.status_code} content={c2!r}")
    fails += not check("多轮工具回传 200 且有 content", r.status_code == 200 and bool(c2))

# 6) 别名防冲突：MiniMax-M3 应仍路由到 intern（原语义），不进 minicode
r = httpx.post(BASE + "/v1/chat/completions", headers=H, timeout=120, json={
    "model": "MiniMax-M3",
    "messages": [{"role": "user", "content": "只回复两个字：别名"}],
    "max_tokens": 64})
print(f"MiniMax-M3（intern 别名通道）HTTP {r.status_code}")
fails += not check("MiniMax-M3 别名仍可用（intern 通道路由不回归）", r.status_code == 200)

print("\n===== 总结 =====")
print("全部通过" if fails == 0 else f"{fails} 项失败")
sys.exit(1 if fails else 0)
