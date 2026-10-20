"""#50 后台常驻 / #51 DSML 三形态 / #52 连接错误可读化。

    python tests/_test_issue50_51_52.py
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _suite_head import *          # noqa: F401,F403

P, A = bootstrap()
declare_suite(__file__, ["qoder_proxy.py", "start-qoder-proxy.bat",
                         "stop-qoder-proxy.bat"])

VB = chr(0xFF5C)


def frame(delta, finish=None):
    return ("data: " + json.dumps({"id": "c", "choices": [{"index": 0,
             "delta": delta, "finish_reason": finish}]},
             ensure_ascii=False) + "\n\n").encode()


def frames(text, n):
    out = [frame({"content": text[i:i + n]}) for i in range(0, len(text), n)]
    out.append(frame({}, "stop"))
    return out


def run_stream(text, n, names):
    calls, txt, fins = [], [], []
    for f in P.recover_leaked_tool_calls(iter(frames(text, n)), names):
        d = P.strip_data_prefix(f.decode("utf-8", "replace"))
        if not d or d == "[DONE]":
            continue
        o = json.loads(d)
        ch = (o.get("choices") or [{}])[0]
        dd = ch.get("delta") or {}
        calls.extend(dd.get("tool_calls") or [])
        txt.append(dd.get("content") or "")
        if ch.get("finish_reason"):
            fins.append(ch["finish_reason"])
    return calls, "".join(txt), fins


# ---------------------------------------------------------------- #51
print("[51] DSML / 闭标签 / 杂字 三形态")
A_TXT = ('</|DSML|tool_calls> [{"name": "pwsh", "arguments": '
         '"{\\"command\\":\\"root = x\\"}"}]')
B_TXT = ('<|DSML|tool_calls> <|DSML|invoke name="pwsh"> '
         '<|DSML|parameter name="command">root = "C:\\Users"</|DSML|parameter> '
         '</|DSML|invoke> </|DSML|tool_calls>')
C_TXT = ('[assistant 请求调用工具]">{"name": "pwsh", "arguments": "{}"}] '
         '</| DSML | parameter> </| DSML | invoke> </| DSML | calls>')

for _name, _txt in (("A 闭标签+数组", A_TXT), ("B 完整DSML", B_TXT),
                    ("C marker+杂字", C_TXT)):
    for _n in (1, 8, 64, 9999):
        _calls, _body, _fins = run_stream(_txt, _n, {"pwsh"})
        _leak = ("DSML" in _body) or ("请求调用工具" in _body)
        check("#51 %s n=%d：恢复 1 调用、零泄漏、finish=tool_calls"
              % (_name, _n),
              len(_calls) == 1 and not _leak and _fins == ["tool_calls"],
              (len(_calls), _leak, _fins))

check("#51 纯 DSML 无 calls 包裹也能解析",
      bool(P.parse_dsml_tool_calls(
          '<|DSML|invoke name="x"> <|DSML|parameter name="a">1'
          '</|DSML|parameter> </|DSML|invoke>')[0]))
check("#51 普通散文（无 DSML/marker）字节级不受影响",
      run_stream("hello world", 4, {"pwsh"})[1] == "hello world")
check("#51 非 DSML 的 < 文本不被吞",
      run_stream("a < b and c > d", 3, {"pwsh"})[1].strip() == "a < b and c > d")

# ---------------------------------------------------------------- #52
print("[52] 连接类错误可读化")
_m = P._friendly_net_error(
    "<urlopen error [WinError 10061] 由于目标计算机积极拒绝，无法连接。>",
    "https://api1.qoder.sh")
check("#52 10061 -> 中文可操作提示（含端口/代理/地址三项检查）",
      "10061" in _m and "HTTP_PROXY" in _m and "api1.qoder.sh" in _m, _m[:80])
check("#52 10060 -> 超时提示",
      "连接超时" in P._friendly_net_error("timed out", "x"), None)
check("#52 10054 -> 重置提示",
      "连接被重置" in P._friendly_net_error(
          "<urlopen error [WinError 10054] 远程主机强迫关闭了一个现有的连接>", "x"))
check("#52 其它异常原样返回（截断 300）",
      P._friendly_net_error("boom") == "boom")

# ---------------------------------------------------------------- #50
print("[50] 后台常驻 + stop.bat")
_root = ROOT
_start = open(os.path.join(_root, "start-qoder-proxy.bat"), encoding="utf-8").read()
_stop = open(os.path.join(_root, "stop-qoder-proxy.bat"), encoding="utf-8").read()
check("#50 start.bat 用 Start-Process 分离启动（关窗口不停）",
      "Start-Process" in _start and "-WindowStyle Hidden" in _start)
check("#50 start.bat 传 --pid-file", "--pid-file" in _start)
check("#50 start.bat 日志重定向到文件", "RedirectStandardOutput" in _start)
check("#50 stop.bat 读 pid 文件并校验命令行（不误杀别的 python）",
      "qoder_proxy" in _stop and "CommandLine" in _stop
      and "Stop-Process" in _stop)
_src = open(os.path.join(_root, "qoder_proxy.py"), encoding="utf-8").read()
check("#50 服务端支持 --pid-file 并在退出时删除",
      "--pid-file" in _src and "atexit.register" in _src)

try:
    _help = subprocess.run([sys.executable, os.path.join(_root, "qoder_proxy.py"),
                            "--help"], capture_output=True, text=True, timeout=60)
    _ok = "--pid-file" in (_help.stdout or "")
except Exception as exc:
    _ok = False
check("#50 --help 里能看到 --pid-file", _ok)

# ---------------------------------------------------------------- #51 补充
print("[51b] 信封层排队（10605 / RESOURCE_LIMIT）")
_DETAIL = json.dumps({"code": "403", "message": json.dumps({
    "code": "10605", "message": json.dumps({
        "isQueued": True, "retryAfterSeconds": 30,
        "serviceAvailable": False, "subCode": "RESOURCE_LIMIT"}),
    "subCode": "RESOURCE_LIMIT"})})
_exc = P.UpstreamStatus(403, _DETAIL)
check("#51b 嵌套 detail 里的 retryAfterSeconds 能解析出来",
      P._queue_wait_seconds(_DETAIL) == 30, P._queue_wait_seconds(_DETAIL))
check("#51b 信封层识别排队信号", P._envelope_queue_wait(_exc) == 30)
check("#51b 排队时允许多等几轮（attempt 0/1/2 可重试，3 停止）",
      [P.should_retry_envelope(_exc, 0, a) for a in range(4)]
      == [True, True, True, False])
check("#51b 重开前按 retryAfterSeconds 等待（30+1=31s）",
      P._envelope_retry_wait(_exc, 1) == 31, P._envelope_retry_wait(_exc, 1))
check("#51b 已向客户端吐过字节就不再重开",
      P.should_retry_envelope(_exc, 10, 0) is False)
_plain = P.UpstreamStatus(403, "forbidden")
check("#51b 普通 403 的重试轮次不变（不因排队逻辑放宽）",
      [P.should_retry_envelope(_plain, 0, a) for a in range(3)]
      == [True, True, False])
_msg, _type = P.friendly_upstream_error(403, _DETAIL)
check("#51b 排队错误给可读文案（含 10605/RESOURCE_LIMIT 与稍后重试）",
      _type == "upstream_queued" and "排队" in _msg, (_type, _msg[:40]))
_src51 = open(os.path.join(ROOT, "qoder_proxy.py"), encoding="utf-8").read()
check("#51b 非流式聚合也留出排队轮次",
      "TRANSIENT_MAX_RETRIES + 1 + QUEUE_MAX_ROUNDS" in _src51)

finish()
