"""_RespSSE 流内错误翻译回归测试（直接驱动事件桥，全程无网络）。

跑法：py -3.12 -m pytest tests/test_responses_error.py -v
"""

import json
import os
import pathlib
import sys
import tempfile

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
os.environ.setdefault("CODELY_CLI_VERSION", "0.0.0-test")  # 免 subprocess 探测 CLI 版本

TMP = pathlib.Path(tempfile.mkdtemp(prefix="qidi-relay-test-"))
(TMP / "config.json").write_text(json.dumps({
    "port": 0,
    "pool_models": ["GLM-5.3", "KIMI-K3"],
    "model_groups": {},
    "providers": {},
    "roles": {},
    "vision_model": "GLM-5.3",
    "vision_capable_models": [],
    "pricing": {},
}), encoding="utf-8")
(TMP / "accounts.json").write_text(json.dumps({"accounts": [
    {"user_id": uid, "username": f"t{uid}", "org_id": "0", "access_token": "a.b.c",
     "enabled": True, "has_glm53": False, "budget_exceeded": False, "group": ""}
    for uid in ("9001", "9002", "9003")]}), encoding="utf-8")

# 路径常量必须在实例化单例（proxy_core.state）之前替换掉，否则会写进真实数据文件
import proxy_core.settings as S  # noqa: E402

S.BASE_DIR = TMP
S.CONFIG_FILE = TMP / "config.json"
S.ACCOUNTS_FILE = TMP / "accounts.json"
S.STATS_FILE = TMP / "token_stats.json"
S.WATER_FILE = TMP / "water_check.json"
S.LOG_FILE = TMP / "proxy.log"

import proxy_core.accounts as A  # noqa: E402
import proxy_core.stats as ST  # noqa: E402
import proxy_core.water as WA  # noqa: E402
import proxy_core.providers as PV  # noqa: E402

A.ACCOUNTS_FILE = S.ACCOUNTS_FILE
ST.STATS_FILE = S.STATS_FILE
WA.WATER_FILE = S.WATER_FILE
PV.CONFIG_FILE = S.CONFIG_FILE

import routes.proxy as RP  # noqa: E402


def _run(bridge, lines):
    """按 gen() 的顺序驱动桥：逐行 feed，最后 finish。"""
    events = []
    for ln in lines:
        events.extend(bridge.feed_line(ln))
    events.extend(bridge.finish())
    return events


def _payload(events, etype):
    """从事件列表里取指定事件类型的 data 载荷（要求恰好一个）。"""
    hits = [e for e in events if e.startswith("event: " + etype)]
    assert len(hits) == 1, "expected exactly one %s, got %d" % (etype, len(hits))
    return json.loads(hits[0].split("data: ", 1)[1])


# ─── 用例 ────────────────────────────────────────────────────────────


def test_stream_error_yields_response_failed_not_completed():
    """上游流内 error 帧 → response.failed，且绝不再发 response.completed。"""
    # 错误帧本身：解析成功但产出零事件（静默丢弃即原 bug）
    b0 = RP._RespSSE("GLM-5.3")
    assert b0.feed_line(json.dumps({"error": {"message": "boom"}})) == []
    assert b0.failed is True

    b = RP._RespSSE("GLM-5.3")
    evs = _run(b, [
        json.dumps({"choices": [{"delta": {"content": "he"}}]}),
        json.dumps({"error": {"message": "boom", "type": "server_error",
                              "code": "upstream_boom"}}),
    ])
    joined = "\n".join(evs)
    assert "event: response.failed" in joined
    assert "response.completed" not in joined
    data = _payload(evs, "response.failed")
    assert data["type"] == "response.failed"
    assert data["response"]["status"] == "failed"
    assert data["response"]["error"] == {"code": "upstream_boom",
                                         "message": "boom"}
    assert data["sequence_number"] >= 1


def test_normal_stream_still_completed():
    """回归：无 error 的正常流仍逐字节产出 delta + response.completed。"""
    b = RP._RespSSE("GLM-5.3")
    evs = _run(b, [
        json.dumps({"choices": [{"delta": {"content": "hi"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 2,
                              "total_tokens": 3}}),
        "[DONE]",
    ])
    joined = "\n".join(evs)
    assert "event: response.output_item.added" in joined
    assert "event: response.output_text.delta" in joined
    assert "event: response.completed" in joined
    assert "response.failed" not in joined
    data = _payload(evs, "response.completed")
    assert data["response"]["status"] == "completed"
    assert data["response"]["usage"] == {"input_tokens": 1,
                                         "output_tokens": 2,
                                         "total_tokens": 3}
    assert data["response"]["output"][0]["content"][0]["text"] == "hi"


def test_error_message_truncated_to_300():
    """超长上游错误信息：发给客户端的 message 截断到 300 字符。"""
    b = RP._RespSSE("GLM-5.3")
    evs = _run(b, [json.dumps({"error": {"message": "x" * 800}})])
    msg = _payload(evs, "response.failed")["response"]["error"]["message"]
    assert len(msg) == 300
    assert msg == "x" * 300


def test_team_id_not_leaked_to_client():
    """上游原文带 Team= 内部标识：客户端 message 不得包含该子串。"""
    raw = ("Budget has been exceeded! Team=secret-uuid-1234 "
           "Current cost: 9999, Max budget: -1.0")
    b = RP._RespSSE("GLM-5.3")
    evs = _run(b, [json.dumps({"error": {"message": raw, "code": "402"}})])
    err = _payload(evs, "response.failed")["response"]["error"]
    assert "Team=" not in err["message"]
    assert "secret-uuid-1234" not in err["message"]
    # 脱敏是打码而非整段丢弃，其余文案仍可见
    assert "Budget has been exceeded" in err["message"]
    assert err["code"] == "402"


def test_error_frame_fallbacks_and_empty_dict():
    """缺省链路：message → type → code → 'upstream error'；空 dict 不算错误帧。"""
    b = RP._RespSSE("GLM-5.3")
    evs = _run(b, [json.dumps({"error": {"type": "rate_limited"}})])
    err = _payload(evs, "response.failed")["response"]["error"]
    assert err["message"] == "rate_limited"
    assert err["code"] == "upstream_error"

    b = RP._RespSSE("GLM-5.3")
    evs = _run(b, [json.dumps({"error": {}})])      # 空 dict：非错误帧
    joined = "\n".join(evs)
    assert "response.failed" not in joined
    assert "event: response.completed" in joined

    b = RP._RespSSE("GLM-5.3")
    evs = _run(b, [json.dumps({"error": {"code": "E500"}})])
    err = _payload(evs, "response.failed")["response"]["error"]
    assert err["message"] == "E500"               # message/type 皆缺 → code
    assert err["code"] == "E500"


def test_error_frame_same_frame_usage_captured():
    """error 帧自带 usage：同帧 usage 必须进 failed 事件载荷（不为 0）。"""
    b = RP._RespSSE("GLM-5.3")
    evs = _run(b, [
        json.dumps({"error": {"message": "quota exceeded", "code": "429"},
                    "usage": {"prompt_tokens": 7, "completion_tokens": 3,
                              "total_tokens": 10}}),
    ])
    resp = _payload(evs, "response.failed")["response"]
    assert resp["usage"] == {"input_tokens": 7, "output_tokens": 3,
                               "total_tokens": 10}


def test_error_code_sanitized_and_capped():
    """error.code 与 message 同样脱敏：Team= 不外泄且 ≤64 字符。"""
    evil_code = "Team=abc123-" + "y" * 100
    b = RP._RespSSE("GLM-5.3")
    evs = _run(b, [json.dumps({"error": {"message": "m",
                                            "code": evil_code}})])
    err = _payload(evs, "response.failed")["response"]["error"]
    assert "Team=" not in err["code"]
    assert "abc123" not in err["code"]
    assert len(err["code"]) <= 64


def test_gen_stream_contract_done_after_finish():
    """gen() 层契约：failed 之后流仍以 data: [DONE] 收尾。

    弱契约 pin：gen() 是 v1_responses 路由内的闭包，依赖 flask
    request 上下文与上游 Response 对象，无法从模块外引用并真
    驱动；故用 inspect.getsource 锁定"finish() 之后必 yield
    data: [DONE]"的源码顺序契约（真驱动不可行的说明在此）。
    """
    import inspect
    src = inspect.getsource(RP.v1_responses)
    assert "bridge.finish()" in src          # finally 中产出终结事件
    assert '"data: [DONE]' in src           # 流尾契约（字面量在源码中）
    # 顺序契约：[DONE] 必须在 finish() 之后 yield
    assert src.index("bridge.finish()") < src.index('"data: [DONE]')
