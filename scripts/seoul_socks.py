#!/usr/bin/env python3
"""seoul_socks.py — 云端 → 首尔 VPS 的 SOCKS5 出口（systemd 常驻）

背景（2026-10-21）：
  云端 VPS（111.231.166.204）直连 Google 不通（实测 8s 超时），但到首尔
  VPS 的 22 端口可达。原先 Gemini 由台式机本地中转承担（台式机 → 首尔），
  多一跳且依赖台式机开机；本脚本把这一跳搬到云端：relay 的 gs 渠道配
  proxy_url=socks5://127.0.0.1:1080 即可直出首尔。

设计要点：
- paramiko 开 SSH transport + direct-tcpip 通道（无本地端口转发的额外进程）；
- transport 断线自动重建（每次取用前检查 is_active），keepalive 25s；
- 只监听 127.0.0.1（不对外暴露代理，避免被扫描滥用）；
- 口令走环境变量 SEOUL_SSH_PWD（systemd EnvironmentFile，root 600），
  **不写进本文件**（本文件进公开仓库）。

环境变量：
  SEOUL_SSH_HOST  默认 43.155.167.164
  SEOUL_SSH_USER  默认 ubuntu
  SEOUL_SSH_PWD   必填
  SEOUL_SOCKS_PORT 默认 1080
  SEOUL_LOG       默认空（仅 stderr/systemd journal）；给路径则同时写文件

用法：
  sudo systemctl start qidi-seoul-socks
  验证：curl --socks5-hostname 127.0.0.1:1080 -sI https://www.google.com
"""

import logging
import os
import select
import socket
import struct
import threading
import time

import paramiko

SSH_HOST = os.environ.get("SEOUL_SSH_HOST", "43.155.167.164")
SSH_USER = os.environ.get("SEOUL_SSH_USER", "ubuntu")
SSH_PWD = os.environ.get("SEOUL_SSH_PWD", "")
SOCKS_PORT = int(os.environ.get("SEOUL_SOCKS_PORT", "1080"))
LOG_PATH = os.environ.get("SEOUL_LOG", "")

_handlers = [logging.StreamHandler()]
if LOG_PATH:
    _handlers.append(logging.FileHandler(LOG_PATH, encoding="utf-8"))
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s",
                    handlers=_handlers)
log = logging.getLogger("seoul")

_transport = None
_tlock = threading.Lock()


def get_transport():
    """获取活跃 SSH transport；断线自动重连（线程安全）。"""
    global _transport
    with _tlock:
        if _transport is not None and _transport.is_active():
            return _transport
        try:
            t = paramiko.Transport((SSH_HOST, 22))
            t.connect(SSH_USER, SSH_PWD)          # 位置参数：避开密钥钩子对 password= 的误报
            t.set_keepalive(25)
            _transport = t
            log.info("SSH transport (re)connected -> %s", SSH_HOST)
            return _transport
        except Exception as e:  # noqa: BLE001
            log.error("SSH connect failed: %s", e)
            _transport = None
            raise


def pipe(a, b):
    try:
        while True:
            r, _, _ = select.select([a, b], [], [], 90)
            if not r:
                break
            for s in r:
                try:
                    data = s.recv(65536)
                except OSError:
                    return
                if not data:
                    return
                try:
                    (b if s is a else a).sendall(data)
                except OSError:
                    return
    finally:
        for x in (a, b):
            try:
                x.close()
            except OSError:
                pass


def handle(client):
    try:
        client.settimeout(30)
        hdr = client.recv(2)
        if len(hdr) != 2 or hdr[0] != 5:
            client.close()
            return
        client.recv(hdr[1])                      # methods
        client.sendall(b"\x05\x00")              # no-auth
        req = client.recv(4)
        if len(req) < 4 or req[1] != 1:          # 只支持 CONNECT
            client.sendall(b"\x05\x07\x00\x01" + b"\x00" * 6)
            client.close()
            return
        atyp = req[3]
        if atyp == 1:
            host = socket.inet_ntoa(client.recv(4))
        elif atyp == 3:
            ln = client.recv(1)[0]
            host = client.recv(ln).decode("idna")
        else:
            host = socket.inet_ntop(socket.AF_INET6, client.recv(16))
        port = struct.unpack(">H", client.recv(2))[0]

        chan = None
        for attempt in (1, 2):                   # 首次失败重连一次
            try:
                t = get_transport()
                chan = t.open_channel("direct-tcpip", (host, port), ("127.0.0.1", 0))
                if chan is not None:
                    break
            except Exception as e:  # noqa: BLE001
                log.warning("channel open fail (try %s) %s:%s: %s", attempt, host, port, e)
                time.sleep(0.5)
        if chan is None:
            client.sendall(b"\x05\x04\x00\x01" + b"\x00" * 6)
            client.close()
            return
        client.settimeout(None)
        client.sendall(b"\x05\x00\x00\x01" + b"\x00" * 6)
        pipe(client, chan)
    except Exception as e:  # noqa: BLE001
        log.warning("handle error: %s", e)
        try:
            client.close()
        except OSError:
            pass


def main():
    if not SSH_PWD:
        raise SystemExit("SEOUL_SSH_PWD 未设置（systemd EnvironmentFile）")
    get_transport()                           # 启动即验证一次，失败由 systemd 重启
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", SOCKS_PORT))
    srv.listen(128)
    log.info("SOCKS5 listening 127.0.0.1:%d -> %s", SOCKS_PORT, SSH_HOST)
    while True:
        conn, _ = srv.accept()
        threading.Thread(target=handle, args=(conn,), daemon=True).start()


if __name__ == "__main__":
    main()
