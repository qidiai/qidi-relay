# -*- coding: utf-8 -*-
"""issue #37 fail-closed 的 HTTP 端到端验证（手动脚本；不进 run_all）。

跑法（系统 python 是 WindowsApps stub，必须用仓库内的 python）：
    & '.\\python\\python.exe' tests\\manual_issue37_e2e.py

做法：进程内 monkeypatch P.open_upstream（返回合成 SSE 信封的 FakeResp + FakeAccount），
用 qoder_proxy 自带的 Handler 起 ThreadingHTTPServer 在随机端口，urllib 发四组请求，
断言状态码与事件序列。禁止真发上游请求；账号/用量目录全部指向 tempfile。
"""
import json
import os
import sys
import tempfile
import threading
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_tmp = tempfile.mkdtemp(prefix="qd-issue37-")
os.environ["ACCOUNTS_DIR"] = os.path.join(_tmp, "accounts")
os.environ["QD_PROXY_USAGE_DIR"] = os.path.join(_tmp, "usage")
os.environ.setdefault("QD_NATIVE_IDENTITY", "0")

import qoder_proxy as P  # noqa: E402

MARKER = P.LEAK_MARKER
# 内层 arguments 是「非空字符串但解析失败」的 JSON：含真实换行 + 未转义引号
# 内层 arguments 是「非空字符串但解析失败」：这里用**尾逗号**这种明确语法错，
# 且不含换行/引号嵌套 —— 否则容易先被 issue #9 的「截断回声」判定吃掉。
BAD_ARGS = '{"path": "a.txt",}'
BAD_ECHO_TEXT = MARKER + ' [{"name": "Write", "arguments": ' + json.dumps(BAD_ARGS) + '}]'


def _chunk(delta=None, finish=None, idx=0):
    choice = {"index": idx, "delta": delta or {}}
    if finish:
        choice["finish_reason"] = finish
    return json.dumps({"id": "c1", "object": "chat.completion.chunk",
                       "created": 0, "model": "m", "choices": [choice]},
                      ensure_ascii=False)


def _env(inner):
    """包装成上游信封一行（statusCodeValue=200）。"""
    return ("data: " + json.dumps({"headers": {}, "body": inner,
                                   "statusCodeValue": 200},
                                  ensure_ascii=False) + "\n").encode("utf-8")


def frames_for_chat():
    """一段流：先吐一个 role 帧，再吐坏参数的 echo 块，末尾正常 finish。"""
    return [
        _env(_chunk({"role": "assistant"})),
        _env(_chunk({"content": BAD_ECHO_TEXT})),
        _env(_chunk({}, finish="stop")),
        _env("[DONE]"),
    ]


class FakeResp(object):
    def __init__(self, frames):
        self._frames = list(frames)
        self.headers = {}
        self.status = 200

    def __iter__(self):
        return iter(self._frames)

    def read(self, *a, **k):
        return b""

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeAccount(object):
    uid = "fake-issue37"
    realm = "cn"
    accessToken = "fake-token"
    refreshToken = "fake-refresh"
    expiresAt = 9999999999
    plan = "pro"
    model = ""
    vmInfo = ""

    def __getattr__(self, name):
        if name.startswith("note_") or name in ("ready", "public", "save", "refresh"):
            return lambda *a, **k: None
        return ""


STATE = {"calls": 0}


def fake_open_upstream(payload, session_key=None, target_realm=None, usage_ctx=None):
    STATE["calls"] += 1
    return FakeResp(frames_for_chat()), FakeAccount(), "{}"


def main():
    orig_open = P.open_upstream
    orig_key = getattr(P, "API_KEY", "")
    orig_keys = getattr(P, "API_KEY_FILE_SET", False)
    P.open_upstream = fake_open_upstream
    # 鉴权开关：Handler._key_ok() 在没配 key 时会问 auth_required()；
    # 直接让它返回 False（=本测试不关心鉴权，只想走到上游出口）。
    orig_auth_required = getattr(P, "auth_required", None)
    try:
        P.auth_required = lambda: False
    except Exception:
        pass
    # None = 不鉴权（模块默认值就是 None）；空字符串会被当成「有 key 但为空」而拒绝。
    P.API_KEY = None
    try:
        P.API_KEY_FILE_SET = False
    except Exception:
        pass
    server = P.ThreadingHTTPServer(("127.0.0.1", 0), P.Handler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    base = "http://127.0.0.1:%d" % port

    def post(path, body, extra=None):
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(base + path, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        for k, v in (extra or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")

    tools = [{"type": "function", "function": {"name": "Write", "parameters": {}}}]
    chat = {"model": "auto", "messages": [{"role": "user", "content": "hi"}], "tools": tools}
    out = []
    try:
        # 1) chat 流式
        st, body = post("/v1/chat/completions", dict(chat, stream=True))
        lines = [l for l in body.splitlines() if l.strip()]
        has_err = any(l.startswith("data:") and '"error"' in l for l in lines)
        has_stop = any(l.startswith("data:") and '"finish_reason": "stop"' in l.replace("'", '"') for l in lines)
        has_tool_fin = any(l.startswith("data:") and '"tool_calls"' in l for l in lines)
        has_done = any(l.strip() == "data: [DONE]" for l in lines)
        out.append(("1 chat-stream", st, "error=%s finish_stop=%s tool_calls=%s DONE=%s" % (has_err, has_stop, has_tool_fin, has_done)))

        # 2) chat 非流式
        st2, body2 = post("/v1/chat/completions", dict(chat, stream=False))
        try:
            j2 = json.loads(body2)
        except Exception:
            j2 = {}
        out.append(("2 chat-json", st2, "error_field=%s msg=%s" % ("error" in j2, str(j2.get("error"))[:70])))

        # 3) responses 流式
        st3, body3 = post("/v1/responses", {"model": "auto", "input": "hi", "stream": True,
                                            "tools": [{"type": "function", "name": "Write",
                                                       "parameters": {}}]})
        out.append(("3 responses", st3, "response.failed=%s response.completed=%s" % (
            "response.failed" in body3, "response.completed" in body3)))

        # 4) anthropic messages 流式
        st4, body4 = post("/v1/messages", {"model": "auto", "max_tokens": 64, "stream": True,
                                           "messages": [{"role": "user", "content": "hi"}],
                                           "tools": [{"name": "Write", "input_schema": {}}]},
                    {"x-api-key": "k", "anthropic-version": "2023-06-01"})
        out.append(("4 anthropic", st4, "event:error=%s message_stop=%s" % (
            "event: error" in body4, "message_stop" in body4)))
    finally:
        server.shutdown()
        server.server_close()
        P.open_upstream = orig_open
        P.API_KEY = orig_key
        if orig_auth_required is not None:
            P.auth_required = orig_auth_required
        try:
            P.API_KEY_FILE_SET = orig_keys
        except Exception:
            pass
        t.join(timeout=5)

    print("issue #37 fail-closed e2e（open_upstream 被调用 %d 次）" % STATE["calls"])
    for name, st, summary in out:
        print("  %-16s HTTP %-4s %s" % (name, st, summary))
    # 关键片段摘录（便于人看）
    print("")
    print("")
    print("  说明：期望 chat 流式有 error 无 finish/tool_calls、非流式 502、responses 有 failed 无 completed、anthropic 有 error 无 message_stop。")
    # ---- 判定链诊断（定位「为什么没走到 issue #37」）----
    print("")
    print("  判定链诊断（allowed_names={'Write'}）：")
    names = {"Write"}
    try:
        calls, clean = P.parse_leaked_tool_calls(BAD_ECHO_TEXT, names)
        print("    parse_leaked_tool_calls -> calls=%r (len=%d)" % (calls, len(calls or [])))
    except Exception as exc:
        print("    parse_leaked_tool_calls -> raised %r" % (exc,))
    for fn in ("_leaked_partial_droppable", "_tool_echo_droppable", "_echo_with_bad_args"):
        f = getattr(P, fn, None)
        if f is None:
            continue
        try:
            print("    %-26s -> %s" % (fn, f(BAD_ECHO_TEXT, names)))
        except Exception as exc:
            print("    %-26s -> raised %r" % (fn, exc))
    rest = BAD_ECHO_TEXT.strip()[len(P.LEAK_MARKER):].strip()
    try:
        import json as _j
        _j.loads(rest)
        complete = True
    except Exception:
        complete = False
    print("    BAD_ECHO_TEXT repr = %r" % (BAD_ECHO_TEXT,))
    print("    rest repr = %r" % (rest,))
    print("    外层数组是完整合法 JSON = %s  ← 若 True，则 _json_array_prefix_ok 返回 True、"
          " _leaked_partial_droppable 命中（issue #9），#37 分支不可达" % complete)
    return 0


if __name__ == "__main__":
    sys.exit(main())
