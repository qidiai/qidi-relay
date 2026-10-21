#!/usr/bin/env python3
"""baidu_push.py — 百度主动推送（sitemap 增量 → data.zz.baidu.com）

背景（2026-10-21）：
  站点 www.qidiai.ltd 已通过百度 HTML 标签验证，拿到主动推送 token。
  百度给个人站的每日配额很小（常见 10 条/天），所以**只推新增 URL**、
  且每次运行有硬上限，避免把配额烧在重复推送上。

行为：
  1. 拉 sitemap.xml（本地文件或 URL 均可），解析出全部 <loc>；
  2. 与状态文件比对，得到"从未成功推送过"的 URL；
  3. 取前 MAX_PER_RUN 条，POST 到 data.zz.baidu.com/urls（text/plain，一行一个）；
  4. 只有百度返回 success>0 才把对应 URL 落盘（失败下次再试）；
  5. 打印百度原始响应（含 remain 剩余配额）。

环境变量（systemd EnvironmentFile / cron 均可）：
  BAIDU_PUSH_TOKEN  必填
  BAIDU_SITE        默认 https://www.qidiai.ltd
  BAIDU_SITEMAP     默认 {SITE}/sitemap.xml
  BAIDU_PUSH_STATE  默认 /var/lib/qidi-push/pushed.json
  BAIDU_PUSH_MAX    默认 10（单次上限，防超配额）

用法：
  BAIDU_PUSH_TOKEN=xxx python3 scripts/baidu_push.py --dry-run
  BAIDU_PUSH_TOKEN=xxx python3 scripts/baidu_push.py

退出码恒为 0（cron 友好）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request

API = "http://data.zz.baidu.com/urls"
UA = "qidi-baidu-push/1.0"


def fetch_text(url: str, timeout: int = 25) -> str:
    if url.startswith("http://") or url.startswith("https://"):
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")
    with open(url, encoding="utf-8", errors="replace") as f:
        return f.read()


def sitemap_urls(xml_text: str) -> list:
    """解析 sitemap 的 <loc>。不依赖命名空间：直接正则取 loc 内容。

    用正则而非 ElementTree 是为了容忍百度/部分生成器的非标准前缀，
    且 sitemap 结构极简（<url><loc>…</loc></url>），风险可忽略。
    """
    return [m.strip() for m in re.findall(r"<loc>\s*(.*?)\s*</loc>", xml_text, re.S) if m.strip()]


def load_state(path: str) -> set:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        pushed = data.get("pushed") or []
        return set(pushed) if isinstance(pushed, list) else set()
    except FileNotFoundError:
        return set()
    except Exception:
        return set()


def save_state(path: str, pushed: set) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"pushed": sorted(pushed)}, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def pick_new(urls: list, pushed: set, limit: int) -> list:
    out = []
    for u in urls:
        if u not in pushed and u not in out:
            out.append(u)
        if len(out) >= limit:
            break
    return out


def push(urls: list, token: str, site: str, timeout: int = 25) -> tuple:
    """返回 (http_status, body_text)。HTTP 错误也按字符串返回，不抛。"""
    endpoint = "%s?site=%s&token=%s" % (API, site, token)
    body = "\n".join(urls).encode("utf-8")
    req = urllib.request.Request(endpoint, data=body,
                                 headers={"Content-Type": "text/plain",
                                          "User-Agent": UA},
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as ex:
        return ex.code, ex.read().decode("utf-8", "replace")
    except Exception as ex:
        return 0, "%s: %s" % (type(ex).__name__, ex)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="百度主动推送（sitemap 增量）")
    ap.add_argument("--dry-run", action="store_true", help="只打印待推送 URL，不发请求")
    ap.add_argument("--all", action="store_true", help="忽略状态文件，重新推送 sitemap 全部 URL")
    args = ap.parse_args(argv)

    token = os.environ.get("BAIDU_PUSH_TOKEN", "").strip()
    site = os.environ.get("BAIDU_SITE", "https://www.qidiai.ltd").strip()
    sitemap = os.environ.get("BAIDU_SITEMAP", site.rstrip("/") + "/sitemap.xml").strip()
    state = os.environ.get("BAIDU_PUSH_STATE", "/var/lib/qidi-push/pushed.json").strip()
    try:
        limit = max(1, int(os.environ.get("BAIDU_PUSH_MAX", "10")))
    except ValueError:
        limit = 10

    if not token:
        print("[baidu-push] BAIDU_PUSH_TOKEN 未设置，跳过", file=sys.stderr)
        return 0

    try:
        xml_text = fetch_text(sitemap)
    except Exception as ex:
        print("[baidu-push] 拉 sitemap 失败: %s" % ex, file=sys.stderr)
        return 0

    urls = sitemap_urls(xml_text)
    pushed = set() if args.all else load_state(state)
    todo = pick_new(urls, pushed, limit)
    print("[baidu-push] sitemap=%d 条，已推=%d 条，本轮待推=%d 条（上限 %d）"
          % (len(urls), len(pushed), len(todo), limit))
    for u in todo:
        print("   ->", u)
    if not todo:
        print("[baidu-push] 无新增 URL，跳过")
        return 0
    if args.dry_run:
        print("[baidu-push] --dry-run：未发送")
        return 0

    status, body = push(todo, token, site)
    print("[baidu-push] HTTP %s %s" % (status, " ".join(body.split())[:300]))
    try:
        resp = json.loads(body)
    except Exception:
        resp = {}
    ok_n = int(resp.get("success") or 0)
    if ok_n > 0:
        # 百度不返回具体哪些成功，按条数顺序记账（推送顺序即我们给的顺序）
        save_state(state, pushed | set(todo[:ok_n]))
        print("[baidu-push] 已记账 %d 条；剩余配额 %s" % (ok_n, resp.get("remain")))
    else:
        print("[baidu-push] 未成功（error=%s），状态不变，下次重试" % (resp.get("error") or "?"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
