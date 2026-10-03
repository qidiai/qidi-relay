# -*- coding: utf-8 -*-
"""
模型同步守护：探活外部上游 → diff → 自动更新中转 config.json（热生效）
              + 自动补 qidi config.toml 条目 + 死模型报告
部署: schtasks 每小时跑一次（QidiRelay-ModelSync）
规则:
  - smtell / atomcode: 列表即权威（运营方策展过的 chat 模型表）→ 自动同步
  - agnes: 官方目录含 embedding/rerank 等杂项 → 只报告不自动写
  - 上游消失的模型: 从中转列表移除 + 报告; qidi 条目保留(fallback 兜底)
  - config 写入均为 tmp+replace 原子操作, 不与中转读撞车
"""
import json
import os
import re
import shutil
import sys
import time
import tomllib
from pathlib import Path

import httpx

RELAY = Path(r"G:\程序开发\本地局域网中转-v3")
CFG_FILE = RELAY / "config.json"
QIDI_CFG = Path(r"G:\程序开发\qidicode\qidicode-portable-windows-x64\qidicode-portable\.qidi\config.toml")
LOG_FILE = RELAY / "model_sync.log"
ATOM_CLIENT_CFG = Path(r"C:\Users\?\.atomcode\config.toml")

CHAT_HINT = re.compile(
    r"glm|deepseek|kimi|minimax|mimo|qwen|claude|fable|opus|sonnet|haiku|grok|gpt|"
    r"doubao|ernie|yi-|intern|longcat|agnes|spark", re.I)


def log(msg):
    line = f"[{time.strftime('%m-%d %H:%M:%S')}] {msg}"
    print(line)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def atomic_write_json(path, obj):
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def atomic_write_text(path, txt):
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(txt)
    os.replace(tmp, path)


# ─────────────────── 各上游模型源 ───────────────────

def fetch_smtell(base_url):
    """smtell 公开中转: /v1/models 免 key（运营方策展 chat 列表）"""
    r = httpx.get(f"{base_url.rstrip('/')}/models", timeout=20)
    r.raise_for_status()
    items = r.json().get("data", [])
    out = {}
    for m in items:
        mid = m.get("id")
        if mid:
            out[mid] = int(m.get("context_window") or 0) or None
    return out


def fetch_agnes(base_url, keys):
    """agnes 官方: /v1/models 带 key（目录含杂项 → 只取 chat 味儿的报告用）"""
    if not keys:
        raise RuntimeError("无 key")
    r = httpx.get(f"{base_url.rstrip('/')}/models",
                  headers={"Authorization": f"Bearer {keys[0]}"}, timeout=20)
    r.raise_for_status()
    ids = [m.get("id") for m in r.json().get("data", []) if m.get("id")]
    return {i: None for i in ids if CHAT_HINT.search(i)}


def fetch_atomcode():
    """atomcode: 客户端套餐同步的 config.toml 为权威（上游 /v1/models 被 Lite 套餐墙）"""
    with open(ATOM_CLIENT_CFG, "rb") as f:
        data = tomllib.load(f)
    out = {}
    for _k, m in (data.get("models") or {}).items():
        if isinstance(m, dict) and m.get("account") == "AtomGit" and m.get("model"):
            out[m["model"]] = {
                "ctx": int(m.get("context_window") or 0) or None,
                "vision": bool(m.get("supports_vision")),
            }
    if not out:
        raise RuntimeError("客户端 config.toml 无 AtomGit 模型")
    return out


# ─────────────────── qidi config.toml 追加 ───────────────────

def qidi_add_model(mid, ctx, note):
    """追加 [model."X"] 块（已存在则跳过）。qidi 启动时读取。"""
    txt = QIDI_CFG.read_text(encoding="utf-8")
    if f'[model."{mid}"]' in txt or f"[model.{mid}]" in txt:
        return False
    block = (
        f'\n[model."{mid}"]\n'
        f'model = "{mid}"\n'
        f'base_url = "http://127.0.0.1:8765/v1"\n'
        f'api_key = "REDACTED-PULL-KEY"\n'
        f'name = "{note}"\n'
        f'api_backend = "chat_completions"\n'
        f'context_window = {ctx or 128000}\n'
        f'fallback_models = ["glm-5.3"]\n'
    )
    # 插到 [ui] 段之前
    if "\n[ui]" in txt:
        txt = txt.replace("\n[ui]", block + "\n[ui]", 1)
    else:
        txt += block
    atomic_write_text(QIDI_CFG, txt)
    return True


# ─────────────────── 主流程 ───────────────────

def main():
    report = {"added": [], "removed": [], "qidi_added": [], "errors": []}
    cfg = json.load(open(CFG_FILE, encoding="utf-8"))
    backup = str(CFG_FILE) + ".bak-sync-" + time.strftime("%m%d")
    if not os.path.exists(backup):
        shutil.copy2(CFG_FILE, backup)

    for pname, p in (cfg.get("providers") or {}).items():
        try:
            if p.get("type") == "atomcode":
                fresh = fetch_atomcode()
                old = p.get("models") or []
                new = sorted(fresh)
                if set(new) != set(old):
                    p["models"] = new
                    report["added"] += [m for m in new if m not in old]
                    report["removed"] += [m for m in old if m not in new]
                    # vision 集合按客户端 supports_vision 维护
                    vis = set(cfg.get("vision_capable_models") or [])
                    for m, info in fresh.items():
                        if info["vision"]:
                            vis.add(m)
                        elif m in vis:
                            vis.discard(m)
                    cfg["vision_capable_models"] = sorted(vis)
                # qidi 条目补全（上下文取客户端声明）
                for m, info in fresh.items():
                    if qidi_add_model(m, info["ctx"], f"AtomCode Lite 同步"):
                        report["qidi_added"].append(m)
                log(f"atomcode: {old} -> {new}")

            elif "smtell" in (p.get("base_url") or ""):
                fresh = fetch_smtell(p["base_url"])
                old = p.get("models") or []
                new = sorted(fresh)
                if set(new) != set(old):
                    p["models"] = new
                    report["added"] += [m for m in new if m not in old]
                    report["removed"] += [m for m in old if m not in new]
                    pr = cfg.setdefault("pricing", {})
                    for m in new:
                        pr.setdefault(m, {"input": 0, "output": 0})
                    for m, ctx in fresh.items():
                        if qidi_add_model(m, ctx, f"smtell 同步"):
                            report["qidi_added"].append(m)
                log(f"smtell: {old} -> {new}")

            elif "agnes" in pname.lower():
                fresh = fetch_agnes(p.get("base_url", ""), [k.get("key") for k in p.get("api_keys", []) if k.get("key")])
                old = p.get("models") or []
                gone = [m for m in old if m not in fresh]
                new_chat = [m for m in fresh if CHAT_HINT.search(m)]
                if gone:
                    report["removed"] += gone
                    log(f"agnes: 检测到疑似下架 {gone}（报告模式，不自动改）")
                candidates = [m for m in new_chat if m not in old]
                if candidates:
                    log(f"agnes: 发现新模型(候选, 需人工确认) {candidates}")
        except Exception as e:
            report["errors"].append(f"{pname}: {type(e).__name__}: {e}")
            log(f"[ERR] {pname}: {type(e).__name__}: {e}")

    if report["added"] or report["removed"] or report["qidi_added"] or report["errors"]:
        atomic_write_json(CFG_FILE, cfg)
        log(f"config.json 已更新(热生效) | +{report['added']} -{report['removed']} "
            f"| qidi新增 {report['qidi_added']}")
    else:
        log("无变化, 配置未动")

    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
