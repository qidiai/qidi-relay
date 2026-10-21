#!/usr/bin/env python3
"""渠道级自愈看守：整渠道被停用（enabled=false）后，额度恢复时自动重新启用。

背景（2026-10-21）：`scripts/key_watchdog.py` 只处理「渠道内某把 key 被停用」。
实测场景是**整个渠道**因额度耗尽被人工停用（`providers.intern.enabled=false`，
8 把 key 本体保留），额度每月初恢复——「等恢复再手动启用」靠人记必漏，
本脚本把这一步自动化。

与 key_watchdog 的分工：
- key_watchdog：渠道在启用态，逐把检查**被停用的 key** → 调
  `/api/provider/key/toggle`（disabled=false）；
- provider_watchdog（本脚本）：渠道在**停用态**，逐把探测**未停用的 key**
  → 调 `/api/providers/<name>/enabled`（enabled=true）。
两者互不重叠，可各自独立 cron。

判定口径（宁可不启用，不可误启用）：
- 探测请求用**调用方点名的模型**（该渠道自己的模型）、max_tokens=1，
  任一把 key 成功（2xx）才算渠道恢复；
- 429 且带额度字样 → 仍耗尽，保持停用；
- 401/403 → 凭证问题，保持停用；
- 其它 4xx/5xx/网络层 → 未知，保持停用（下一次巡检再试）；
- 全部 key 都失败 → 保持停用，逐把打印原因。

安全纪律：
- **只处理 `--provider` 显式点名的渠道**——绝不批量扫描，避免把人工
  故意停用的渠道自动打开；
- 渠道本身已启用时**零网络请求**直接返回（探测本身消耗额度）；
- 不读写 key 本体；不改 `routes/`、`proxy_core/`；
- 管理口令不落进本文件：运行时从 `config.json` 的 `admin_password` 读取。

用法：
    python3 scripts/provider_watchdog.py \\
        --config /opt/codely-relay/config.json \\
        --provider intern --probe-model intern-s1 \\
        --admin-url https://luyou.qidiai.ltd:8443

退出码恒为 0（cron 友好）；异常写 stderr。
"""

from __future__ import annotations

import argparse
import json
import sys
from urllib.parse import quote

try:                                    # 作为包导入（pytest：from scripts import ...）
    from scripts.key_watchdog import TIMEOUT, classify, http_post_json, key_entries
except ImportError:                     # 直接运行（sys.path[0] = scripts/）
    from key_watchdog import TIMEOUT, classify, http_post_json, key_entries

PREFIX = "[provider-watchdog]"


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def active_keys(prov: dict) -> list:
    """渠道内**未停用**的 key 条目（disabled=true 的保留在池中但不探测）。"""
    return [e for e in key_entries(prov) if not e["disabled"]]


def probe_key(base_url: str, key: str, model: str, insecure: bool = False) -> tuple:
    """与 key_watchdog.probe_key 同形态（max_tokens=1 的最小探测）。"""
    url = base_url.rstrip("/") + "/chat/completions"
    return http_post_json(url, {
        "model": model,
        "messages": [{"role": "user", "content": "ok"}],
        "max_tokens": 1,
    }, headers={"Authorization": "Bearer " + key}, insecure=insecure,
        timeout=TIMEOUT)


def enable_provider(admin_url: str, admin_cred: str, provider: str,
                    insecure: bool = False) -> tuple:
    """调 relay 自带渠道启停接口（routes/admin.py:536，蓝图前缀 /api）。

    body {"enabled": true}；鉴权走 admin_pwd cookie（proxy_core/auth.py:52）。
    该接口内部是文本级改写 + 备份 + 原子写，不经过本脚本动 config 本体。
    """
    url = (admin_url.rstrip("/") + "/api/providers/"
           + quote(provider, safe="") + "/enabled")
    return http_post_json(url, {"enabled": True},
                          headers={"Cookie": "admin_pwd=" + admin_cred},
                          insecure=insecure, timeout=TIMEOUT)


def _channel_disabled(prov: dict) -> bool:
    """缺省（无 enabled 成员）= 启用；只有明确的 JSON false 才算停用。"""
    return prov.get("enabled", True) is False


def _snippet(body: str, limit: int = 110) -> str:
    return " ".join((body or "").split())[:limit]


def _run(args) -> int:
    try:
        cfg = load_config(args.config)
    except Exception as ex:
        print(f"{PREFIX} 读配置失败: {ex}", file=sys.stderr)
        return 0

    prov = (cfg.get("providers") or {}).get(args.provider)
    if not isinstance(prov, dict):
        print(f"{PREFIX} 渠道不存在: {args.provider}", file=sys.stderr)
        return 0
    base_url = prov.get("base_url") or ""
    if not base_url:
        print(f"{PREFIX} 渠道缺 base_url: {args.provider}", file=sys.stderr)
        return 0

    if not _channel_disabled(prov):
        print(f"{PREFIX} {args.provider}: 渠道已启用，无需处理（零探测成本）")
        return 0

    keys = active_keys(prov)
    if not keys:
        print(f"{PREFIX} {args.provider}: 无可用 key（全部停用或未配置），"
              f"无法探测，保持停用")
        return 0

    print(f"{PREFIX} {args.provider}: 渠道停用中，{len(keys)} 把 key 待探测 "
          f"(model={args.probe_model})")

    recovered_by = ""
    for entry in keys:
        label = entry["label"]
        try:
            status, body = probe_key(base_url, entry["key"], args.probe_model,
                                     args.insecure)
        except Exception as ex:         # 探测异常绝不影响后续 key / 整体退出码
            status, body = 0, f"{type(ex).__name__}: {ex}"
        verdict = classify(status, body)
        if verdict == "recovered":
            recovered_by = label
            print(f"  ✓ {label}: 探测 {status} 已恢复")
            break
        if verdict == "exhausted":
            print(f"  · {label}: 额度仍耗尽（{status}）{_snippet(body)}")
        elif verdict == "unauthorized":
            print(f"  ! {label}: 凭证失效/无权限（{status}）{_snippet(body)}")
        else:
            print(f"  ? {label}: 探测未知结果（{status}）{_snippet(body)}")

    if not recovered_by:
        print(f"{PREFIX} 完成：{args.provider} 探测未恢复，保持停用")
        return 0

    if args.dry_run:
        print(f"{PREFIX} [dry-run] {args.provider}: {recovered_by} 探测恢复，未启用")
        return 0

    admin_cred = cfg.get("admin_password") or ""
    if not admin_cred:
        print(f"{PREFIX} config 缺 admin_password，无法启用渠道 "
              f"{args.provider}", file=sys.stderr)
        return 0

    t_status, t_body = enable_provider(args.admin_url, admin_cred, args.provider,
                                       args.insecure)
    if 200 <= t_status < 300:
        print(f"{PREFIX} 完成：{args.provider} 探测恢复（{recovered_by}）→ 已重新启用")
    else:
        print(f"{PREFIX} 探测恢复但启用失败 HTTP {t_status} {_snippet(t_body, 100)}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="渠道级自愈看守（额度恢复后自动启用）")
    ap.add_argument("--config", required=True, help="relay config.json 路径")
    ap.add_argument("--provider", required=True, help="只处理该渠道（显式点名，安全阀）")
    ap.add_argument("--probe-model", required=True, help="探测用模型（该渠道自己的模型）")
    ap.add_argument("--admin-url", required=True, help="relay 面板根地址")
    ap.add_argument("--dry-run", action="store_true", help="只探测不启用")
    ap.add_argument("--insecure", action="store_true", help="跳过 TLS 校验（证书异常时应急）")
    args = ap.parse_args(argv)

    try:
        return _run(args)
    except Exception as ex:             # 兜底：任何异常都不影响 cron
        print(f"{PREFIX} 未预期异常: {type(ex).__name__}: {ex}", file=sys.stderr)
        return 0


if __name__ == "__main__":
    sys.exit(main())
