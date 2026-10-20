"""1.4.4 批次回归：六个 issue 的判据（#37 #38 #39 #40 #41 #42）。

    python tests/_test_issues_1_4_4.py

判据都落在**能被该缺陷直接决定的那一层**：
  #42 anthropic 流末 message_delta.usage 的完整字段（不是 chat_usage 函数）；
  #37 内层 arguments 非法的协议回声不再泄漏、不再普通 stop；
  #38 排队信号被解析成等待（而不是当错误返回）；
  #39 共享额度池被纳入 packages（上游返回时）；
  #40 403 降级（桌面头重试 + 区域标记错误提示）+ 签到 403 的可读原因；
  #41 改密码时原密码错 -> 403（不再是 401 弹登录）。
"""
import os
import sys
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _suite_head import *          # noqa: F401,F403

P, A = bootstrap()
declare_suite(__file__, ["qoder_proxy.py", "qoder_accounts.py",
                         "qoder_anthropic.py", "dashboard.html"])
import qoder_anthropic as AN        # noqa: E402

ROOT_SRC = os.path.join(ROOT, "qoder_proxy.py")
ACC_SRC = os.path.join(ROOT, "qoder_accounts.py")


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


# ---------------------------------------------------------------- #42
print("[42] anthropic 流式 message_delta.usage 完整字段")
_frames = [
    b'data: {"choices":[{"index":0,"delta":{"role":"assistant",'
    b'"content":"hi"},"finish_reason":null}]}\n\n',
    b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n',
]
_holder = {"usage": {"prompt_tokens": 100, "completion_tokens": 20,
                     "prompt_cache_hit_tokens": 60}}
_events = [e.decode("utf-8") for e in
           AN.stream_anthropic_events(iter(_frames), model="m", holder=_holder)]
_delta = [e for e in _events if e.startswith("event: message_delta")]
check("#42 流末有且仅有一条 message_delta", len(_delta) == 1, _delta[:1])
_payload = json.loads(_delta[0].split("data: ", 1)[1]) if _delta else {}
_u = _payload.get("usage") or {}
check("#42 message_delta.usage 含 input_tokens（=prompt-cached）",
      _u.get("input_tokens") == 40, _u)
check("#42 message_delta.usage 含 cache_read_input_tokens",
      _u.get("cache_read_input_tokens") == 60, _u)
check("#42 message_delta.usage 含 cache_creation_input_tokens（恒 0，不编造）",
      _u.get("cache_creation_input_tokens") == 0, _u)
check("#42 message_delta.usage 含 output_tokens 与 reasoning 明细",
      _u.get("output_tokens") == 20
      and isinstance(_u.get("output_tokens_details"), dict), _u)
check("#42 守恒：input_tokens + cache_read == prompt_tokens",
      _u.get("input_tokens", 0) + _u.get("cache_read_input_tokens", 0) == 100, _u)

# ---------------------------------------------------------------- #37
print("[37] 内层 arguments 非法的协议回声")
_GOOD = json.dumps({"file_path": "D:/x.txt", "content": "ok"},
                   ensure_ascii=False)
_BAD_LF = '{"file_path":"D:/x.txt","content":"line one\nline two"}'
_BAD_QUOTE = '{"file_path":"D:/x.txt","content":"say "hi""}'
_MK = P.LEAK_MARKER


def _echo(args):
    return _MK + "\n" + json.dumps([{"name": "Write", "arguments": args}],
                                   ensure_ascii=False)


check("#37 合法参数：不判为 bad-args",
      not P._echo_with_bad_args(_echo(_GOOD), {"Write"}))
check("#37 内层真实 LF：判为 bad-args",
      P._echo_with_bad_args(_echo(_BAD_LF), {"Write"}))
check("#37 内层未转义引号：判为 bad-args",
      P._echo_with_bad_args(_echo(_BAD_QUOTE), {"Write"}))
check("#37 内层截断（缺右花括号）：判为 bad-args",
      P._echo_with_bad_args(_echo('{"file_path":"D:/x.txt"'), {"Write"}))
check("#37 尾部多余文本：判为 bad-args",
      P._echo_with_bad_args(_echo(_GOOD + " trailing"), {"Write"}))
check("#37 未声明工具名：不判（避免把普通示例当错误）",
      not P._echo_with_bad_args(
          _MK + "\n" + json.dumps([{"name": "Nope",
                                    "arguments": _BAD_LF}],
                                  ensure_ascii=False), {"Write"}))
check("#37 普通散文讨论 marker：不判",
      not P._echo_with_bad_args("说明 " + _MK + " 的用法", {"Write"}))
check("#37 合法 JSON 示例（无 marker）：不判",
      not P._echo_with_bad_args('A JSON example: [{"content":"probe"}]',
                                {"Write"}))
# fail-closed（v1.5.1）：坏参数必须抛协议错误，而不是「说明 + 普通 stop」
_stream = _echo(_BAD_LF)
_frames37 = [("data: " + json.dumps(
    {"id": "c", "model": "m", "choices": [{"index": 0,
     "delta": {"content": _stream}, "finish_reason": "stop"}]},
    ensure_ascii=False) + "\n\n").encode("utf-8")]
_out37 = []
_raised37 = None
try:
    for f in P.recover_leaked_tool_calls(iter(_frames37), {"Write"}):
        d = P.strip_data_prefix(f.decode("utf-8", "replace"))
        if d and d != "[DONE]":
            _out37.append(json.loads(d))
except P.InvalidToolArguments as exc:
    _raised37 = exc
_txt37 = "".join(((_o.get("choices") or [{}])[0].get("delta") or {})
                 .get("content") or "" for _o in _out37)
_calls37 = [c for _o in _out37
            for c in (((_o.get("choices") or [{}])[0].get("delta") or {})
                      .get("tool_calls") or [])]
_fins37 = [(_o.get("choices") or [{}])[0].get("finish_reason") for _o in _out37]
check("#37 流末：坏参数抛 InvalidToolArguments（status 502）",
      isinstance(_raised37, P.InvalidToolArguments)
      and _raised37.status == 502, _raised37)
check("#37 流末：抛出前零 tool_calls（不伪造可执行调用）",
      _calls37 == [], _calls37)
check("#37 流末：抛出前协议文本零泄漏（marker/arguments 都不出现）",
      _MK not in _txt37 and "file_path" not in _txt37, _txt37[:80])
check("#37 流末：抛出前没有正常结束帧（不以 stop/tool_calls 结束）",
      not any(f in ("stop", "tool_calls") for f in _fins37 if f), _fins37)
check("#37 错误详情是固定串（不含参数/路径/正文）",
      _raised37 is not None
      and _raised37.detail == P.BAD_ARGS_ERROR_DETAIL
      and "file_path" not in _raised37.detail, 
      getattr(_raised37, "detail", None))

# ---------------------------------------------------------------- #38
print("[38] 排队信号 -> 等待重试")
check("#38 队列体（10605+retryAfterSeconds）解析出秒数",
      P._queue_wait_seconds(
          '{"code":"10605","isQueued":true,"retryAfterSeconds":30}') == 30)
check("#38 只给 isQueued 无时长 -> 保守 30s",
      P._queue_wait_seconds('{"isQueued":true}') == 30)
check("#38 非排队 403 -> 0（走原有错误分类）",
      P._queue_wait_seconds('{"code":"FORBIDDEN"}') == 0)
check("#38 三档可调常量存在且为正",
      P.QUEUE_MAX_ROUNDS > 0 and P.QUEUE_WAIT_MAX_SECS > 0
      and P.QUEUE_WAIT_BUDGET_SECS > 0)
_src38 = _read(ROOT_SRC)
_iq = _src38.find("qwait = _queue_wait_seconds(detail)")
_ib = _src38.find("if exc.code in (429, 401, 403):", _iq if _iq > 0 else 0)
check("#38 排队判定位于「429/401/403 立即失败」之前（先等再放弃）",
      0 < _iq < _ib, (_iq, _ib))

# ---------------------------------------------------------------- #39
print("[39] 共享额度池纳入 packages")
_real_http = A.http_json
try:
    A.http_json = lambda *a, **k: {
        "userQuota": {"total": 100, "used": 10, "remaining": 90},
        "addOnQuota": {"total": 50, "used": 5, "remaining": 45},
        "sharedQuota": {"total": 500, "used": 200, "remaining": 300},
        "isQuotaExceeded": False,
    }
    _acc39 = A.Account({"uid": "u-39", "realm": "cn",
                        "accessToken": "tok"})
    _r39 = _acc39.fetch_credits()
finally:
    A.http_json = _real_http
_cr39 = (_r39 or {}).get("credits") or {}
check("#39 共享额度被记入 credits.shared",
      (_cr39.get("shared") or {}).get("remain") == 300, _cr39.get("shared"))
_names39 = [p.get("name") for p in (_cr39.get("packages") or [])]
check("#39 packages 里出现「共享额度」行",
      "共享额度" in _names39, _names39)
check("#39 套餐总额口径不变（不把共享额度并进 remain）",
      _cr39.get("remain") == 135 and _cr39.get("size") == 150, _cr39)

# ---- #39 后续：报告者提供的真实团队账号响应（orgResourcePackage，cap 而非 total）
_TEAM_QUOTA = {
    "userType": "teams",
    "usageType": "credits",
    "totalUsagePercentage": 1.0,
    "isQuotaExceeded": False,
    "expiresAt": 1794240000000,
    "outerProviders": [],
    "userQuota": {"total": 3000.0, "used": 3000.0, "remaining": 0.0,
                  "percentage": 1.0, "unit": "credits"},
    "orgResourcePackage": {"used": 1548.0, "remaining": 952.0,
                           "percentage": 0.62, "unit": "credits",
                           "cap": 2500.0, "available": True},
    "isPlanQuotaProrated": False,
}


def _quota_with(payload):
    def _fake(*a, **k):
        return dict(payload)
    return _fake


try:
    A.http_json = _quota_with(_TEAM_QUOTA)
    _acc_team = A.Account({"uid": "u-team", "realm": "cn",
                           "accessToken": "tok"})
    _acc_team.reserve_credits = 500
    _r_team = _acc_team.fetch_credits()
finally:
    A.http_json = _real_http
_c_team = (_r_team or {}).get("credits") or {}
_sh_team = _c_team.get("shared") or {}
check("#39b orgResourcePackage 被识别：remain=952 / size=cap=2500",
      _sh_team.get("remain") == 952 and _sh_team.get("size") == 2500
      and _sh_team.get("available") is True, _sh_team)
check("#39b 套餐 remain 仍为 0（口径不变，便于核对）",
      _c_team.get("remain") == 0, _c_team.get("remain"))
check("#39b 有效余额 = 套餐 0 + 共享 952 = 952",
      _c_team.get("remain_effective") == 952, _c_team.get("remain_effective"))
check("#39b packages 出现「共享额度」行且带 cap",
      any(p.get("name") == "共享额度" and p.get("size") == 2500
          for p in (_c_team.get("packages") or [])),
      _c_team.get("packages"))
check("#39b 预留护栏按有效余额判定（reserve 500 < 952 -> 不拦）",
      _acc_team.reserve_blocked() is False)

# available=false：共享池不可用 -> 不计入有效余额
_TEAM_OFF = dict(_TEAM_QUOTA)
_TEAM_OFF["orgResourcePackage"] = dict(_TEAM_QUOTA["orgResourcePackage"],
                                       available=False)
try:
    A.http_json = _quota_with(_TEAM_OFF)
    _acc_off = A.Account({"uid": "u-team-off", "realm": "cn",
                          "accessToken": "tok"})
    _r_off = _acc_off.fetch_credits()
finally:
    A.http_json = _real_http
_c_off = (_r_off or {}).get("credits") or {}
check("#39b available=false：共享额度不计入有效余额",
      _c_off.get("remain_effective") == 0, _c_off.get("remain_effective"))

# 只读汇总（/credits/summary）同样按有效余额
try:
    A.http_json = _quota_with(_TEAM_QUOTA)
    _acc_sum = A.Account({"uid": "u-sum", "realm": "cn",
                          "accessToken": "tok"})
    _acc_sum.fetch_credits()
finally:
    A.http_json = _real_http


class _FakePool:
    def __init__(self, accounts):
        self.accounts = list(accounts)


_real_pool = getattr(P, "POOL", None)
try:
    P.POOL = _FakePool([_acc_sum])
    _sum = P.credits_summary()
finally:
    P.POOL = _real_pool
check("#39b credits_summary 合计用有效余额（952，不是 0）",
      (_sum.get("totals") or {}).get("remain") == 952, _sum.get("totals"))
_row = (_sum.get("accounts") or [{}])[0]
check("#39b 汇总行同时保留原始 remain 与有效余额",
      _row.get("credits_remain") == 0
      and _row.get("credits_remain_effective") == 952
      and _row.get("credits_shared_remain") == 952, _row)

# ---------------------------------------------------------------- #40
print("[40] 403 降级与区域标记错误提示")
_calls40 = []


def _fake403(url, method=None, headers=None, **kw):
    _calls40.append(dict(headers or {}))
    if len(_calls40) == 1:
        raise urllib.error.HTTPError(url, 403, "Forbidden", {},
                                     io.BytesIO(b'{"code":"WAF"}'))
    return {"userQuota": {"total": 10, "used": 0, "remaining": 10},
            "addOnQuota": {"total": 0, "used": 0, "remaining": 0},
            "isQuotaExceeded": False}


try:
    A.http_json = _fake403
    _acc40 = A.Account({"uid": "u-40", "realm": "intl", "accessToken": "tok"})
    _r40 = _acc40.fetch_credits()
finally:
    A.http_json = _real_http
check("#40 403 后用桌面头重试一次并成功",
      bool(_r40.get("ok")) and len(_calls40) == 2
      and _calls40[1].get("User-Agent") == "Qoder", _calls40)


def _fake403_mismatch(url, method=None, headers=None, **kw):
    if "openapi.qoder.sh" in url or "/api/v2/quota" not in url:
        raise urllib.error.HTTPError(url, 403, "Forbidden", {},
                                     io.BytesIO(b'{"code":"WAF"}'))
    # 另一区（国内）可用 -> 说明是区域标记错误
    return {"userQuota": {"total": 1, "used": 0, "remaining": 1},
            "addOnQuota": {"total": 0, "used": 0, "remaining": 0}}


try:
    A.http_json = _fake403_mismatch
    _acc40b = A.Account({"uid": "u-40b", "realm": "intl", "accessToken": "tok"})
    _r40b = _acc40b.fetch_credits()
finally:
    A.http_json = _real_http
check("#40 另一区可用 -> 错误里点明「区域标记错误」",
      (not _r40b.get("ok")) and "区域标记错误" in (_r40b.get("error") or ""),
      _r40b)
_src40 = _read(ACC_SRC)
check("#40 签到 403 不再当硬错误（给可读原因）",
      "上游拒绝访问（HTTP 403）" in _src40)

# ---------------------------------------------------------------- #41
print("[41] 改密码先校验原密码")
_src41 = _read(ROOT_SRC)
_i41 = _src41.find('if path == "/panel/password":')
_blk41 = _src41[_i41:_i41 + 900] if _i41 > 0 else ""
check("#41 原密码错返回 403（不是 401——401 会让前端弹登录）",
      '当前密码不正确' in _blk41 and "_error(403" in _blk41, _blk41[:200])
check("#41 校验发生在 set_panel_password 之前",
      _blk41.find("verify_panel_password") < _blk41.find("set_panel_password")
      if _blk41 else False)
_js41 = _read(os.path.join(ROOT, "dashboard.html"))
check("#41 前端 403 不弹登录遮罩（既有契约，回归钉死）",
      "403" in _js41 and "panelGate" in _js41)

finish()
