# -*- coding: utf-8 -*-
"""_diag_gateway.py —— 网关存活与流式自检（一条命令回答“为什么 harness 不吐字”）

检查顺序（任一环节失败即给出针对性提示）：
  1) 端口是否有进程在监听（8790，IPv4/IPv6 都查）
  2) GET /health 是否返回 qoder-proxy
  3) GET /v1/models 是否 200（含当前 realm 与模型数）
  4) （--chat）真实流式对话：是否 200、是否收到首字节、是否 [DONE] 收尾
     —— 这一步能区分“网关没起” / “起了但不能流式” / “上游内容审核拒绝”

用法：
    python _diag_gateway.py                 # 只做 1-3
    python _diag_gateway.py --chat          # 追加真实流式自检
    python _diag_gateway.py --port 8790 --model Qwen3.8-Flash --key <API_KEY>
退出码：0=全部通过；1=有环节失败。
"""
import argparse
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

OK = "[ OK ]"
BAD = "[FAIL]"


def check_port(host, port):
    """TCP 可连接性（IPv4 优先，回退 IPv6）。"""
    for family, addr in ((socket.AF_INET, host),
                         (socket.AF_INET6, "::1")):
        try:
            infos = socket.getaddrinfo(addr, port, family, socket.SOCK_STREAM)
        except Exception:
            continue
        for af, stype, proto, _, sa in infos:
            s = socket.socket(af, stype, proto)
            s.settimeout(2.0)
            try:
                s.connect(sa)
                s.close()
                return True, "%s:%s" % (addr, port)
            except Exception:
                s.close()
    return False, "%s:%s" % (host, port)


def guard_url(url, allow_remote=False):
    """出站 URL 白名单。

    自检脚本的用途是访问**本机**网关，因此默认只允许回环地址；显式给出
    --allow-remote 时才按服务端同款规则校验（仅 http/https，且 host 不得
    解析到本机/私有/保留网段），避免把自检脚本变成任意请求跳板。
    """
    parsed = urllib.parse.urlsplit(str(url or ""))
    if parsed.scheme not in ("http", "https"):
        raise ValueError("only http/https URLs are allowed")
    host = (parsed.hostname or "").lower()
    if not allow_remote:
        if host not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError(
                "host %r is not loopback; pass --allow-remote to probe a "
                "remote gateway" % host)
        return url
    from qoder_accounts import validate_public_http_url
    return validate_public_http_url(url)


def http(url, key=None, timeout=10, data=None, allow_remote=False):
    url = guard_url(url, allow_remote=allow_remote)
    headers = {"Accept": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET",
                                 headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read().decode("utf-8", "replace")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8790)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--key", default=None,
                    help="API key（面板设置里那个；本地无 Key 时可省略）")
    ap.add_argument("--model", default="Qwen3.8-Flash")
    ap.add_argument("--chat", action="store_true",
                    help="追加真实流式对话自检（会消耗极少量额度）")
    ap.add_argument("--allow-remote", action="store_true",
                    help="允许探测非本机网关（默认只允许回环地址）")
    args = ap.parse_args()

    base = "http://%s:%d" % (args.host, args.port)
    failures = []
    print("=" * 64)
    print("qoder-proxy 自检 @ %s" % base)
    print("=" * 64)

    # 1) 端口
    up, where = check_port(args.host, args.port)
    if up:
        print(OK, "端口可连接（%s）" % where)
    else:
        print(BAD, "端口无人响应（%s）——网关进程没在跑" % where)
        if os.name == "nt":
            print("       -> 双击 start-qoder-proxy.bat（窗口必须保持开启）")
        else:
            print("       -> 运行 ./start-qoder-proxy.sh（终端必须保持开启）")
        print("       -> harness 会表现为“一直工作中但不吐字”，正是本项失败")
        failures.append("port")

    # 2) /health
    if not failures:
        try:
            st, body = http(base + "/health", key=args.key, timeout=6,
                 allow_remote=args.allow_remote)
            info = json.loads(body)
            print(OK, "/health 200 service=%s version=%s realm=%s accounts=%s ready=%s"
                  % (info.get("service"), info.get("version"), info.get("realm"),
                     info.get("accounts"), info.get("accounts_ready")))
            if info.get("accounts_ready", 0) == 0:
                print(BAD, "没有可用账号 -> 看板「扫描本地凭证」或用 OAuth/PAT 导入")
                failures.append("accounts")
        except Exception as exc:
            print(BAD, "/health 失败：%s" % exc)
            failures.append("health")

    # 3) /v1/models
    if not failures:
        try:
            st, body = http(base + "/v1/models", key=args.key, timeout=30,
                 allow_remote=args.allow_remote)
            data = json.loads(body)
            print(OK, "/v1/models 200 realm=%s models=%d"
                  % (data.get("realm"), len(data.get("data") or [])))
        except urllib.error.HTTPError as exc:
            print(BAD, "/v1/models HTTP %d（401 表示需要 API Key，用 --key 传入；"
                       "也用看板设置里那个 Key）" % exc.code)
            failures.append("models")
        except Exception as exc:
            print(BAD, "/v1/models 失败：%s" % exc)
            failures.append("models")

    # 4) 真实流式对话
    if args.chat and not failures:
        print("-" * 64)
        print("流式自检：model=%s stream=true" % args.model)
        payload = json.dumps({"model": args.model, "stream": True,
                              "messages": [{"role": "user",
                                            "content": "reply with: ok"}]}).encode()
        req = urllib.request.Request(
            guard_url(base + "/v1/chat/completions",
                      allow_remote=args.allow_remote),
            data=payload, method="POST",
            headers={"Content-Type": "application/json",
                     **({"Authorization": "Bearer " + args.key}
                        if args.key else {})})
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                first = None
                n_line = 0
                buf = b""
                for line in resp:
                    if first is None:
                        first = int((time.time() - t0) * 1000)
                    n_line += 1
                    buf += line
            done = b"[DONE]" in buf
            head = buf[:160].decode("utf-8", "replace")
            print(OK, "流式 200 ttft=%sms lines=%d done=%s" % (first, n_line, done))
            if not done:
                print(BAD, "未见 [DONE] 收尾（流被截断）")
                failures.append("stream")
            if '"error"' in head or "content_policy" in buf.decode("utf-8", "replace"):
                msg = buf.decode("utf-8", "replace")[:300]
                print(BAD, "上游返回业务错误（非网关问题）：%s" % msg)
                failures.append("upstream")
        except urllib.error.HTTPError as exc:
            detail = exc.read(400).decode("utf-8", "replace")
            print(BAD, "流式 HTTP %d：%s" % (exc.code, detail[:240]))
            failures.append("stream-http")
        except Exception as exc:
            print(BAD, "流式异常：%s" % exc)
            failures.append("stream-exc")

    print("=" * 64)
    if failures:
        print("结果：存在问题 -> %s" % ", ".join(failures))
        return 1
    print("结果：全部通过（网关在线且可流式；若 harness 仍不吐字，"
          "请把 --chat 输出与其日志一并提供）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
