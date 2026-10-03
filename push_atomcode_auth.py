# -*- coding: utf-8 -*-
"""AtomCode 凭证推送脚本：在有 AtomCode 客户端登录的电脑上运行，
把本机 auth.toml 推送到局域网代理，代理自动注册为多账号之一。

用法（在这台电脑上、Python 3.8+，无需装任何包）：
  py -3 push_atomcode_auth.py --to http://192.168.1.<IP>:8765 --label 小号
  py -3 push_atomcode_auth.py --to http://192.168.1.<IP>:8765 --label 小号 --pwd 管理密码
  py -3 push_atomcode_auth.py --to http://192.168.1.<IP>:8765 --label 小号 --file "C:/Users/我/.atomcode/auth.toml"

说明：
- --to    代理地址（v3 控制台里看得到的那台机器）
- --label 账号备注名（在代理面板里怎么显示这个账号）
- --pwd   代理管理密码（面板登录密码；未设密码可省略）
- --file  auth.toml 路径，缺省 ~/.atomcode/auth.toml（官方客户端登录后的默认位置）
- 推送一次即可：之后代理会用 refresh_token 自动续期，不用反复推。
- 建议推送完成后，这台电脑上的官方客户端可以退出登录（避免两边各自刷新 token 打架）。
"""
import argparse
import io
import json
import sys
import urllib.request
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")


def post_json(url: str, payload: dict, cookie: str = "", timeout: int = 20) -> tuple:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 **({"Cookie": cookie} if cookie else {})},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8")), r.headers
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8")), e.headers
        except Exception:
            return e.code, {}, e.headers


def main():
    ap = argparse.ArgumentParser(description="推送本机 AtomCode auth.toml 到局域网代理")
    ap.add_argument("--to", required=True, help="代理地址，如 http://192.168.1.<IP>:8765")
    ap.add_argument("--label", default="", help="账号备注名（如：小号）")
    ap.add_argument("--pwd", default="", help="代理管理密码（未设密码可省略）")
    ap.add_argument("--file", default="", help="auth.toml 路径（缺省 ~/.atomcode/auth.toml）")
    args = ap.parse_args()

    base = args.to.rstrip("/")
    f = Path(args.file) if args.file else Path.home() / ".atomcode" / "auth.toml"
    if not f.exists():
        print(f"[错误] 找不到 {f}")
        print("请先在这台电脑上登录官方 AtomCode 客户端（登录后会生成该文件）。")
        sys.exit(1)
    content = f.read_text(encoding="utf-8")
    if "access_token" not in content:
        print("[错误] 文件里没有 access_token，不像 auth.toml。请确认路径。")
        sys.exit(1)

    # 1. 登录换 cookie（代理没设密码时直接跳过）
    cookie = ""
    if args.pwd:
        st, d, hdrs = post_json(f"{base}/api/login", {"password": args.pwd})
        if st != 200 or not d.get("ok"):
            print(f"[错误] 管理密码校验失败（HTTP {st}）: {d.get('error', '')}")
            sys.exit(1)
        setc = hdrs.get("Set-Cookie", "")
        if "admin_pwd=" in setc:
            cookie = setc.split("admin_pwd=")[1].split(";")[0]
            cookie = f"admin_pwd={cookie}"

    # 2. 推送凭证
    st, d, _ = post_json(f"{base}/api/atomcode/upload",
                         {"label": args.label, "content": content}, cookie=cookie)
    if st == 200 and not d.get("error"):
        verb = "更新" if d.get("updated") else "添加"
        print(f"[成功] 已{verb}账号「{d.get('label')}」（user={d.get('user_id')}，"
              f"代理现有 {d.get('account_count')} 个账号）")
        print("代理面板的「外部账号」卡片里可以看到它，请求会自动在账号间轮换。")
    else:
        print(f"[失败] HTTP {st}: {d.get('error', '未知错误')}")
        sys.exit(1)


if __name__ == "__main__":
    main()
