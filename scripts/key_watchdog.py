#!/usr/bin/env python3
"""静态 key 自愈看守：额度耗尽被停用的 key，额度恢复后自动重新启用。

背景（2026-10-20）：ModelScope 魔搭 API-Inference 的免费额度是**按日刷新**
（每天 12 点恢复新额度），额度用尽时上游返回 429 `insufficient balance`。
面板上把这种 key 停用（F14 语义：保留 key 本体、不参与轮换）是对的，但
「等额度重置再启用」若靠人记着，必然漏。本脚本把这一步自动化。

判定口径（宁可不启用，不可误启用）：
- 探测请求用**调用方点名的模型**、max_tokens=1，成功（2xx）才启用；
- 429 且带额度字样 → 仍耗尽，保持停用；
- 401/403 → 凭证问题（过期/欠权），保持停用并在日志里点名；
- 其它 4xx/5xx → 未知，保持停用（下一次巡检再试）。

安全纪律：
- **只处理 `--provider` 显式点名的渠道**——不扫描全部渠道，避免把人工
  故意停用的 key 自动打开；
- 不读写 key 本体，只调 relay 自带的 `/api/provider/key/toggle`（该端点
  写盘前自动备份 config.json）；
- 管理口令不落进本文件：运行时从 `config.json` 的 `admin_password` 读取；
- 探测本身消耗额度，因此**只在存在停用 key 时才发探测**（正常态零成本）。

用法：
    python3 scripts/key_watchdog.py \\
        --config /opt/codely-relay/config.json \\
        --provider modelscope --probe-model Qwen/Qwen3.8-27B \\
        --admin-url https://luyou.qidiai.ltd:8443

退出码恒为 0（cron 友好）；异常写 stderr。
"""

from __future__ import annotations

import argparse
import json
import ssl
import sys
import urllib.error
import urllib.request

TIMEOUT = 90
# 「额度耗尽」的上游文案（ModelScope 实测 "insufficient balance"；其余为
# 常见变体，宁可多识别为耗尽——耗尽只是不启用，代价是等下一次巡检）
_QUOTA_WORDS = (
    "insufficient balance",
    "insufficient_quota",
    "quota exceeded",
    "quota_exceeded",
    "额度",
    "余额不足",
)


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def key_entries(prov: dict) -> list:
    """把 provider 的 api_key / api_keys 归一化为条目列表。

    与 relay `providers._parse_keys` 同口径的最小实现：单 `api_key` 与
    数组 `api_keys` 均可；条目可为裸字符串或 {"key","label","disabled"}。
    缺省 label 用 "keyN"（与该模块一致，便于人在面板上核对）。
    """
    out = []
    single = prov.get("api_key")
    if isinstance(single, str) and single.strip():
        out.append({"key": single.strip(), "label": "主key", "disabled": False})
    arr = prov.get("api_keys")
    if isinstance(arr, list):
        for idx, e in enumerate(arr):
            if isinstance(e, str):
                if e.strip():
                    out.append({"key": e.strip(), "label": f"key{idx + 1}", "disabled": False})
            elif isinstance(e, dict):
                k = e.get("key")
                if isinstance(k, str) and k.strip():
                    out.append({
                        "key": k.strip(),
                        "label": str(e.get("label") or f"key{idx + 1}"),
                        "disabled": bool(e.get("disabled")),
                    })
    # 单值与数组重复的 key 去重（保留首次出现）
    seen, uniq = set(), []
    for e in out:
        if e["key"] in seen:
            continue
        seen.add(e["key"])
        uniq.append(e)
    return uniq


def disabled_entries(prov: dict) -> list:
    return [e for e in key_entries(prov) if e["disabled"]]


def classify(status: int, body: str) -> str:
    """探测结果分类：recovered / exhausted / unauthorized / unknown。"""
    text = (body or "").lower()
    if 200 <= status < 300:
        return "recovered"
    if status == 429 or any(w in text for w in _QUOTA_WORDS):
        return "exhausted"
    if status in (401, 403):
        return "unauthorized"
    return "unknown"


def http_post_json(url: str, payload: dict, headers: dict | None = None,
                   timeout: int = TIMEOUT, insecure: bool = False) -> tuple:
    """POST JSON，返回 (status, body_text)。HTTP 错误按状态码返回，不抛。"""
    data = json.dumps(payload).encode()
    hdrs = {"Content-Type": "application/json"}
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=hdrs)
    ctx = None
    if insecure:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as ex:
        return ex.code, ex.read().decode("utf-8", "replace")
    except Exception as ex:                      # 网络层失败 → 未知，不影响其他 key
        return 0, f"{type(ex).__name__}: {ex}"


def probe_key(base_url: str, key: str, model: str, insecure: bool = False) -> tuple:
    url = base_url.rstrip("/") + "/chat/completions"
    return http_post_json(url, {
        "model": model,
        "messages": [{"role": "user", "content": "ok"}],
        "max_tokens": 1,
    }, headers={"Authorization": "Bearer " + key}, insecure=insecure)


def toggle_key(admin_url: str, admin_cred: str, provider: str, label: str,
               disabled: bool, insecure: bool = False) -> tuple:
    url = admin_url.rstrip("/") + "/api/provider/key/toggle"
    return http_post_json(url, {
        "provider": provider, "label": label, "disabled": disabled,
    }, headers={"Cookie": "admin_pwd=" + admin_cred}, insecure=insecure)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="静态 key 自愈看守（额度恢复后自动启用）")
    ap.add_argument("--config", required=True, help="relay config.json 路径")
    ap.add_argument("--provider", required=True, help="只处理该渠道（显式点名，安全阀）")
    ap.add_argument("--probe-model", required=True, help="探测用模型（须该 key 有权访问）")
    ap.add_argument("--admin-url", required=True, help="relay 面板根地址")
    ap.add_argument("--dry-run", action="store_true", help="只探测不启用")
    ap.add_argument("--insecure", action="store_true", help="跳过 TLS 校验（证书异常时应急）")
    args = ap.parse_args(argv)

    try:
        cfg = load_config(args.config)
    except Exception as ex:
        print(f"[watchdog] 读配置失败: {ex}", file=sys.stderr)
        return 0
    prov = (cfg.get("providers") or {}).get(args.provider)
    if not isinstance(prov, dict):
        print(f"[watchdog] 渠道不存在: {args.provider}", file=sys.stderr)
        return 0
    base_url = prov.get("base_url") or ""
    if not base_url:
        print(f"[watchdog] 渠道缺 base_url: {args.provider}", file=sys.stderr)
        return 0
    admin_cred = cfg.get("admin_password") or ""
    if not admin_cred:
        print("[watchdog] config 缺 admin_password，无法启用 key", file=sys.stderr)
        return 0

    pending = disabled_entries(prov)
    if not pending:
        print(f"[watchdog] {args.provider}: 无停用 key，跳过（零探测成本）")
        return 0

    print(f"[watchdog] {args.provider}: {len(pending)} 把停用 key 待探测 "
          f"(model={args.probe_model})")
    enabled = 0
    for entry in pending:
        label = entry["label"]
        status, body = probe_key(base_url, entry["key"], args.probe_model, args.insecure)
        verdict = classify(status, body)
        snippet = " ".join((body or "").split())[:110]
        if verdict == "recovered":
            if args.dry_run:
                print(f"  [dry-run] {label}: 探测 {status} 已恢复，未启用")
                continue
            t_status, t_body = toggle_key(args.admin_url, admin_cred, args.provider,
                                          label, False, args.insecure)
            if 200 <= t_status < 300:
                enabled += 1
                print(f"  ✓ {label}: 探测 {status} 恢复 → 已重新启用")
            else:
                print(f"  ✗ {label}: 探测恢复但启用失败 HTTP {t_status} "
                      f"{' '.join((t_body or '').split())[:100]}")
        elif verdict == "exhausted":
            print(f"  · {label}: 额度仍耗尽（{status}）{snippet}")
        elif verdict == "unauthorized":
            print(f"  ! {label}: 凭证失效/无权限（{status}）{snippet}")
        else:
            print(f"  ? {label}: 探测未知结果（{status}）{snippet}")
    print(f"[watchdog] 完成：重新启用 {enabled} 把")
    return 0


if __name__ == "__main__":
    sys.exit(main())
