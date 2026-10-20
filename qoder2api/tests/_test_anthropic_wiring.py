# -*- coding: utf-8 -*-
"""Anthropic 接线套件（task-78 接线轮）。

    python tests/_test_anthropic_wiring.py

钉住三件事：
  1) x-api-key 鉴权（唯一会让功能完全不可用的单点）；
  2) 形态决策**单点**（_qd_structured 下传，两处同源）；
  3) 流式守卫链 + **泄漏回归**（#9/#11 样本走新入口不得漏）。
"""
import json
import os
import sys
import threading
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("ACCOUNTS_DIR", os.path.join(ROOT, "_acc"))
import qoder_anthropic as AN
import qoder_proxy as P

PASS = FAIL = SKIP = 0


def check(label, cond, extra=None):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] " + label)
    else:
        FAIL += 1
        print("  [FAIL] %s  %r" % (label, extra))


# ---- 指纹行（与前端 _test_dashboard_*.js 同格式；实现见 tests/_suite_head.py）----
import os as _os_fp
import sys as _sys_fp
_sys_fp.path.insert(0, _os_fp.path.dirname(_os_fp.path.abspath(__file__)))
from _suite_head import declare_suite
declare_suite(__file__, ["qoder_proxy.py", "qoder_anthropic.py"])
print("[A] x-api-key 鉴权 + 路由接线（HTTP 层）")


class _Acct:
    uid = "u1"
    nickname = "t"
    realm = "cn"
    enabled = True
    access_token = "dt-x"
    credits = None


class _Pool:
    accounts = [_Acct()]

    def get(self, uid):
        return self.accounts[0] if uid else None


P.POOL = _Pool()
P.API_KEY = "k"
P.configured_keys = lambda: []
P.auth_required = lambda: True
_srv = P.ThreadingHTTPServer(("127.0.0.1", 0), P.Handler)
_port = _srv.server_address[1]
threading.Thread(target=_srv.serve_forever, daemon=True).start()


def _post(path, headers, payload=None):
    data = json.dumps(payload or {"model": "m", "max_tokens": 8,
                                  "messages": [{"role": "user",
                                                "content": "hi"}]}).encode()
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (_port, path),
                                 data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


_AK = {"x-api-key": "k", "anthropic-version": "2023-06-01"}
_st1, _b1 = _post("/v1/messages/count_tokens", _AK)
check("count_tokens 用 x-api-key 能过鉴权（200）且返回 Anthropic 形状",
      _st1 == 200 and json.loads(_b1).get("input_tokens", 0) > 0, (_st1, _b1))
_st2, _b2 = _post("/v1/messages", _AK)
check("messages 用 x-api-key 不再是 401/404（已过鉴权与路由，进入上游打开）",
      _st2 not in (401, 404), (_st2, _b2[:120]))
check("无凭据 -> 401；错 key -> 401",
      _post("/v1/messages", {})[0] == 401
      and _post("/v1/messages", {"x-api-key": "WRONG"})[0] == 401)
check("_is_panel_route(/v1/messages) 必须为 False（不要求 X-Panel-Token）",
      P.Handler._is_panel_route("/v1/messages") is False)
_srv.shutdown()
_srv.server_close()

print()
print("[B] 形态决策单点（_qd_structured 下传，两处同源）")


class _Acc:
    user_type = "personal_standard"
    realm = "cn"
    uid = "u"
    nickname = "n"
    access_token = "dt-x"
    expires_at = 0


def _body_tool_shape(model, qd_structured):
    payload = {"model": model, "messages": [
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "terminal", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "name": "terminal",
         "content": "ok"}]}
    if qd_structured is not None:
        payload["_qd_structured"] = qd_structured
    body = P.build_qoder_body(payload, _Acc(), "qwen3.8-flash", realm="cn")
    blob = json.dumps(body.get("messages") or [], ensure_ascii=False)
    if P.TOOL_RESULT_MARKER in blob:
        return "text"
    if "\"role\": \"tool\"" in blob:
        return "structured"
    return "?"


check("自动判定（qwen+cn 无 _qd_structured）-> 结构化",
      _body_tool_shape("qwen3.8-flash", None) == "structured")
check("显式 False 覆盖自动判定（证明 flatten 采信下传值，而不是各判一次）",
      _body_tool_shape("qwen3.8-flash", False) == "text")
check("显式 True 覆盖自动判定（DeepSeek 也会走结构化：仅用于验证单点）",
      _body_tool_shape("deepseek-v3", True) == "structured")
_src = open(os.path.join(ROOT, "qoder_proxy.py"), encoding="utf-8").read()
check("接线点存在：_handle_anthropic 里写入 _qd_structured 且 build_qoder_body 优先读它",
      'chat_req["_qd_structured"] = structured_tool_history_enabled(' in _src
      and 'use_structured = payload.get("_qd_structured")' in _src)

print()
print("[C] 流式守卫链 + 泄漏回归（#9/#11 样本走新入口）")


def _raw(content=None, fin=None):
    delta = {} if content is None else {"content": content}
    inner = {"id": "c", "model": "m", "created": 1,
             "choices": [{"index": 0, "delta": delta, "finish_reason": fin}]}
    return ("data: " + json.dumps(inner, ensure_ascii=False) + "\n\n").encode()


def _env(content=None, fin=None):
    inner = json.loads(_raw(content, fin)[6:])
    return ("data: " + json.dumps({"statusCodeValue": 200,
                                   "body": json.dumps(inner, ensure_ascii=False)},
                                  ensure_ascii=False) + "\n\n").encode()


class _Resp:
    def __init__(self, items):
        self.items = list(items)

    def __iter__(self):
        return iter(self.items)

    def close(self):
        pass


def _run_new_entry(frames, allowed):
    """复刻 _handle_anthropic 的流式管线（顺序与参数逐字一致）。"""
    holder = {"usage": None, "allowed_names": allowed}
    inner = P.sse_with_heartbeat(
        P.recover_leaked_tool_calls(
            P.iter_inner_sse(_Resp(frames), holder=holder),
            allowed_names=allowed),
        lambda b: None)
    return "".join(f.decode("utf-8")
                   for f in AN.stream_anthropic_events(inner, "m", holder))


_M = P.LEAK_MARKER
_TRUNC9 = _M + "\n" + '[{"name": "terminal", "arguments": "{\\"cmd\\": \\"ls'
_TR11 = ("[工具结果]" + chr(10) + '{"output": "ok"}' + chr(10)
         + "[工具结果结束]" + chr(10) + "<system_warning>x</system_warning>")
_FULL8 = _M + "\n" + json.dumps(
    [{"name": "terminal", "arguments": json.dumps({"cmd": "ls"})}],
    ensure_ascii=False)


def _split_frames(text, cuts=2):
    step = max(1, len(text) // cuts)
    frames = [_env(text[i * step:(i + 1) * step]) for i in range(cuts - 1)]
    frames.append(_env(text[(cuts - 1) * step:]))
    frames.append(_env("", "stop"))
    return frames


_out9 = _run_new_entry(_split_frames(_TRUNC9), {"terminal"})
check("泄漏回归 · issue#9（截断的 marker+JSON 回声）-> 事件流里无标记",
      _M not in _out9 and "工具结果" not in _out9)
_out11 = _run_new_entry(_split_frames(_TR11), {"terminal"})
check("泄漏回归 · issue#11（[工具结果] 回声块）-> 事件流里无标记",
      "[工具结果" not in _out11 and _M not in _out11)
_out8 = _run_new_entry(_split_frames(_FULL8), {"terminal"})
check("泄漏回归 · issue#8（完整 marker+数组）-> 还原为 Anthropic tool_use，且无标记",
      _M not in _out8 and "\"type\": \"tool_use\"" in _out8)
check("管线逐字一致（顺序 + allowed_names 取 chat_req）——静态钉住",
      "sse_with_heartbeat(" in _src
      and "recover_leaked_tool_calls(" in _src
      and "allowed_names=_tool_names_from_payload(chat_req))," in _src)

print()
print("SUMMARY: TOTAL %d checks, %d passed, %d skipped"
      % (PASS + FAIL + SKIP, PASS, SKIP))
print("RESULT: %s (exit %d)" % ("GREEN" if FAIL == 0 else "RED",
                                1 if FAIL else 0))
sys.exit(1 if FAIL else 0)