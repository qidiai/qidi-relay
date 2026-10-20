#!/usr/bin/env python3
"""_verify_models.py —— 模型库与能力清单端到端逐字段验证（对比官方真实数据）。

做法（全离线官方基准 + 本机 HTTP 网关输出）：
  官方基准：
    1) 本机官方客户端模型目录 ~/.qoder[.cn]/.models/<uid>/catalog-v6
       （QMC 解密，桌面版此刻渲染选择器的同源数据）
    2) 官方界面文案 dynamic-text（zh.model.*.detail/label）
  网关侧：
    GET {base}/v1/models?realm={cn,intl}（/v1/models 的 model_entry 输出）

逐模型断言：
    A. id == 官方 display_name（客户端唯一要填的值）
    B. enabled == 官方 enable
    C. 峰谷价 == 官方 promotion（peak=before_promotion_price_factor，
       valley=peak×discount_factor，price_factor=快照时刻的官方当前价，
       window_start/end、badge 文案、active 透传）
    D. 上下文窗口 == 官方 context_config（标签集合 + 默认窗口）
    E. 思考档位 == 官方 thinking_config（efforts 集合 + 默认档）
    F. description == 官方动态文案 zh.detail
    G. 禁用项 disabled_reason == 官方原话 + disabled_message_key 透传
    H. 不编造字段：无 max_output_tokens（官方 catalog/动态接口均无）
    I. off_peak_active_now 与窗口即时判定一致（当前 02:xx 应在 22:00-08:00 低谷内）

用法：
    python qoder_proxy.py --port 8790     # 终端 A
    python _verify_models.py              # 终端 B（默认 --base http://127.0.0.1:8790）
退出码：0 全部通过；1 有失败；2 网关不可达。
"""
import argparse
import ipaddress
import json
import os
import socket
import sys
import urllib.request
import urllib.error
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import qoder_catalog as C

PASS = FAIL = 0
FAILURES = []


def check(realm, key, label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        FAILURES.append(f"[{realm}] {key}: {label} {extra}")


def official_catalog(realm):
    """解密本机官方客户端 catalog（chat 场景）——作为动态不可用时的按字段兜底。"""
    from qoder_sign import qmc_decrypt
    home = os.path.join(os.path.expanduser("~"),
                        ".qoder-cn" if realm == "cn" else ".qoder", ".models")
    with open(os.path.join(home, "default"), encoding="utf-8") as fh:
        uid = json.load(fh).get("uid")
    cat = os.path.join(home, str(uid), "catalog-v6")
    with open(cat, "rb") as fh:
        blob = fh.read().decode("ascii", "replace")
    return json.loads(qmc_decrypt(blob, str(uid)).decode("utf-8"))["chat"]


def official_live(realm):
    """官方"此刻"基准：动态接口优先（桌面版选择器同源），本地目录按字段兜底。

    返回 (entries_dict, source_label)：entries 以动态条目为主体、动态为 None
    的字段用本地目录同 key 条目补齐（与网关 merge 的 None-保护一致）。
    """
    try:
        import qoder_proxy as P
        import qoder_accounts as A
        if P.POOL is None:
            pool_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "accounts")
            P.POOL = A.AccountPool(pool_dir)
            P.POOL.load()
        dynamic = P.read_dynamic_models(realm=realm)
    except Exception:
        dynamic = []
    local = {m["key"]: m for m in official_catalog(realm)}
    if not dynamic:
        return local, "local-catalog(静态文件)"
    out = {}
    for key, meta in dynamic:
        base = dict(local.get(key) or {})
        base.update({k: v for k, v in (meta or {}).items() if v is not None})
        base.setdefault("key", key)
        out[key] = base
    return out, "live-dynamic(官方此刻, 本地字段兜底)"


def _validate_fetch_url(url, allow_loopback):
    """请求前校验：协议白名单 + host 解析 + IP 边界。

    本工具面向开发者验证网关，默认目标为本机 127.0.0.1，因此环回地址按
    allow_loopback 显式放行；私网、保留、链路本地（含云元数据 169.254.x）
    与多播地址一律拒绝。
    """
    parsed = urllib.parse.urlsplit(str(url or ""))
    if parsed.scheme not in ("http", "https"):
        raise ValueError("only http/https URLs are allowed")
    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise ValueError("URL host is required")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    for info in infos:
        ip = ipaddress.ip_address(str(info[4][0]).strip("[]"))
        if ip.is_loopback:
            if not allow_loopback:
                raise ValueError("loopback target requires --allow-loopback")
            continue
        if (ip.is_private or ip.is_reserved or ip.is_link_local
                or ip.is_multicast or ip.is_unspecified):
            raise ValueError("target address %s is not allowed" % ip)
    return url


def fetch_models(base, realm):
    url = _validate_fetch_url(f"{base}/v1/models?realm={realm}",
                              allow_loopback=True)
    with urllib.request.urlopen(url, timeout=30) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    return payload.get("data") or []


def window_now(promo):
    """复算 off_peak_active_now（与 qoder_proxy 同逻辑，独立实现防同错）。"""
    import time as _t
    import datetime as _dt
    try:
        s = int(str(promo["window_start"]).split(":")[0]) * 60 + int(str(promo["window_start"]).split(":")[1])
        e = int(str(promo["window_end"]).split(":")[0]) * 60 + int(str(promo["window_end"]).split(":")[1])
    except Exception:
        return None
    local = _dt.datetime.fromtimestamp(_t.time(), _dt.timezone.utc) + _dt.timedelta(hours=8)
    cur = local.hour * 60 + local.minute
    if s == e:
        return True
    return (s <= cur < e) if s < e else (cur >= s or cur < e)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8790")
    args = ap.parse_args()

    texts = C.load_official_text()
    report = {}

    for realm in ("cn", "intl"):
        # --- 网关输出 ---
        try:
            entries = fetch_models(args.base, realm)
        except urllib.error.HTTPError as e:
            print(f"FATAL: gateway returned HTTP {e.code} for realm={realm} "
                  f"(401 means panel/API key required — pass a key via ?key=)")
            return 2
        except Exception as e:
            print(f"FATAL: gateway unreachable at {args.base}: {e}")
            print("       start it first:  python qoder_proxy.py --port 8790")
            return 2
        # --- 官方基准（动态优先，本地目录兜底） ---
        official, baseline_src = official_live(realm)
        gw = {e.get("upstream_key") or "": e for e in entries}
        rep = {"official": len(official), "gateway": len(gw),
               "baseline": baseline_src,
               "official_only": sorted(set(official) - set(gw)),
               "gateway_only": sorted(set(gw) - set(official))}
        report[realm] = rep

        print(f"\n===== [{realm}] baseline={baseline_src} "
              f"official={len(official)} gateway={len(gw)} =====")

        for key, om in official.items():
            e = gw.get(key)
            if not e:
                check(realm, key, "missing in gateway", False)
                continue
            # A. id == display_name
            check(realm, key, "id == display_name",
                  e.get("id") == om.get("display_name"),
                  f"id={e.get('id')!r} official={om.get('display_name')!r}")
            # B. enable
            check(realm, key, "enabled == official enable",
                  bool(e.get("enabled")) == bool(om.get("enable")),
                  f"gw={e.get('enabled')} off={om.get('enable')}")
            # C. 峰谷价
            promo = om.get("promotion") or {}
            if promo:
                peak_official = promo.get("before_promotion_price_factor")
                check(realm, key, "price_factor_peak == official before_promotion",
                      e.get("price_factor_peak") == peak_official,
                      f"gw={e.get('price_factor_peak')} off={peak_official}")
                op = e.get("off_peak") or {}
                check(realm, key, "off_peak window matches",
                      op.get("window_start") == promo.get("window_start")
                      and op.get("window_end") == promo.get("window_end"),
                      f"gw={op.get('window_start')}-{op.get('window_end')} "
                      f"off={promo.get('window_start')}-{promo.get('window_end')}")
                check(realm, key, "off_peak badge (zh) matches official",
                      op.get("badge") == (promo.get("badge") or {}).get("zh"),
                      f"gw={op.get('badge')!r} off={(promo.get('badge') or {}).get('zh')!r}")
                check(realm, key, "off_peak.active == official promotion.active",
                      bool(op.get("active")) == bool(promo.get("active")),
                      f"gw={op.get('active')} off={promo.get('active')}")
                # I. 低谷即时判定
                expect_now = window_now(promo)
                check(realm, key, "off_peak_active_now == independent recomputation",
                      e.get("off_peak_active_now") == bool(expect_now),
                      f"gw={e.get('off_peak_active_now')} recompute={expect_now}")
            # 官方 price_factor 是"快照抓取时刻"的价格；低谷价 = 峰价 × 折扣
            check(realm, key, "price_factor == official price_factor",
                  e.get("price_factor") == om.get("price_factor"),
                  f"gw={e.get('price_factor')} off={om.get('price_factor')}")
            if promo and promo.get("before_promotion_price_factor") is not None \
                    and promo.get("discount_factor"):
                want_valley = round(float(promo["before_promotion_price_factor"])
                                    * float(promo["discount_factor"]), 4)
                check(realm, key, "valley == peak x discount (official rule)",
                      e.get("price_factor_valley") == want_valley,
                      f"gw={e.get('price_factor_valley')} off={want_valley}")
            # D. 上下文窗口
            ccfg = om.get("context_config") or {}
            if ccfg:
                official_labels = set(ccfg.keys())
                gw_labels = set(e.get("context_window_labels") or [])
                check(realm, key, "context labels == official context_config",
                      gw_labels == official_labels,
                      f"gw={sorted(gw_labels)} off={sorted(official_labels)}")
                official_default = next(
                    (k for k, v in ccfg.items()
                     if isinstance(v, dict) and v.get("is_default")), None)
                check(realm, key, "context default matches",
                      e.get("context_window_default") == official_default,
                      f"gw={e.get('context_window_default')} off={official_default}")
            # E. 思考档位
            tcfg = om.get("thinking_config") or {}
            enabled = tcfg.get("enabled") if isinstance(tcfg.get("enabled"), dict) else {}
            off_efforts = set((enabled.get("efforts") or {}).keys())
            gw_efforts = set(e.get("reasoning_efforts") or [])
            check(realm, key, "reasoning efforts == official thinking_config",
                  gw_efforts == off_efforts,
                  f"gw={sorted(gw_efforts)} off={sorted(off_efforts)}")
            # F. 官方介绍文案
            off_desc = texts["descriptions"].get(key) or ""
            check(realm, key, "description == official dynamic-text",
                  (e.get("description") or "") == off_desc,
                  f"gw={(e.get('description') or '')[:40]!r} off={off_desc[:40]!r}")
            # F2. 官方本地化名（zh.label 与 display_name 不同时须如实透出）
            check(realm, key, "name_local == official local label",
                  (e.get("name_local") or "") == (C.official_local_name(key) or ""),
                  f"gw={e.get('name_local')!r} off={C.official_local_name(key)!r}")
            # F3. 免费语义：官方 0 价模型 valley 必须为 0；is_free 不冒充 0 价
            if om.get("price_factor") == 0:
                check(realm, key, "free model valley == 0",
                      e.get("price_factor_valley") == 0,
                      e.get("price_factor_valley"))
            if e.get("price_factor_valley") == 0:
                check(realm, key, "valley==0 iff official price==0",
                      om.get("price_factor") == 0,
                      om.get("price_factor"))
            # G. 禁用项官方原因
            if not om.get("enable"):
                check(realm, key, "disabled_reason == OFFICIAL copy",
                      e.get("disabled_reason") == C.OFFICIAL_DISABLED_REASON,
                      f"gw={e.get('disabled_reason')!r}")
                strategies = om.get("strategies") or []
                want_key = next((s.get("disabled_message_key") for s in strategies
                                 if isinstance(s, dict) and s.get("disabled_message_key")),
                                None)
                if want_key:
                    check(realm, key, "disabled_message_key passthrough",
                          e.get("disabled_message_key") == want_key,
                          f"gw={e.get('disabled_message_key')!r} off={want_key!r}")
            # H. 不编造最大输出
            check(realm, key, "no fabricated max_output_tokens",
                  "max_output_tokens" not in e,
                  sorted(k for k in e if "output" in k))

        # 双向集合一致性
        check(realm, "*", "no official model missing in gateway",
              not rep["official_only"], rep["official_only"])
        check(realm, "*", "no unknown model injected by gateway",
              not rep["gateway_only"], rep["gateway_only"])
        print(f"  set diff: official_only={rep['official_only']} "
              f"gateway_only={rep['gateway_only']}")
        # 人可读完整清单（便于肉眼对照官方客户端）
        print("  gateway list: " + " | ".join(
            f"{e.get('upstream_key')}={e.get('id')}"
            f"{'*' if not e.get('enabled') else ''}"
            for e in entries))
        # 低谷促销模型必须齐全（3 个，缺一不可）
        promo_gw = sorted(e.get("upstream_key") for e in entries if e.get("off_peak"))
        check(realm, "*", "ALL 3 off-peak promotion models present",
              promo_gw == ["qmodel", "qmodel_38max", "qmodel_latest"], promo_gw)
        print(f"  off-peak models: {promo_gw}")

    print("\n" + "=" * 60)
    print(f"VERIFY SUMMARY: PASS={PASS} FAIL={FAIL}")
    if FAILURES:
        print("FAILURES:")
        for f in FAILURES:
            print("  -", f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
