# -*- coding: utf-8 -*-
"""_refresh_catalog.py —— 从本机官方客户端目录缓存刷新区域模型快照

用途（客户端更新后跑一次即可跟上官方）：
  1. 解密本机官方客户端的模型目录缓存
       国际版  ~/.qoder/.models/<uid>/catalog-v6
       国内版  ~/.qoder-cn/.models/<uid>/catalog-v6
     （QMC v1：HKDF-SHA256(uid) + AES-256-GCM，见 qoder_sign.qmc_decrypt）
  2. 取 chat 场景条目**逐字段原样**写入
       qoder_catalog_intl.json / qoder_catalog_cn.json
     （网关运行时优先读这两个文件；`qoder_catalog.py` 里的内嵌副本只是
       文件缺失时的冻结回退，默认一并更新，见 --inline/--no-inline）
  3. 打印与现有快照的差异摘要（价格倍率 / 上下文窗口 / 思考档位 / 上下架）

用法：
    python _refresh_catalog.py              # 更新 JSON + 内嵌回退，并打印差异
    python _refresh_catalog.py --dry-run    # 只打印差异，不写文件
    python _refresh_catalog.py --no-inline  # 只更新 JSON 快照文件

退出码：0=成功（含"无变化"）；1=任一侧读取/解密失败。
"""
import argparse
import io
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from qoder_sign import qmc_decrypt  # noqa: E402

REALMS = (
    ("intl", ".qoder", "qoder_catalog_intl.json", "_INTL_JSON"),
    ("cn", ".qoder-cn", "qoder_catalog_cn.json", "_CN_JSON"),
)

COMPARE_FIELDS = ("enable", "price_factor", "max_input_tokens", "is_free",
                  "is_new", "is_reasoning", "is_vl", "display_name",
                  "context_config", "thinking_config", "promotion",
                  "strategies", "minimal_version")


def client_catalog_path(home_dir):
    """返回本机客户端目录缓存文件路径（catalog-v6）。"""
    home = os.path.join(os.path.expanduser("~"), home_dir, ".models")
    if not os.path.isdir(home):
        return ""
    uid = ""
    default = os.path.join(home, "default")
    if os.path.isfile(default):
        try:
            with io.open(default, encoding="utf-8") as fh:
                uid = str(json.load(fh).get("uid") or "")
        except Exception:
            uid = ""
    subs = [uid] if uid and os.path.isdir(os.path.join(home, uid)) else \
        sorted(d for d in os.listdir(home) if os.path.isdir(os.path.join(home, d)))
    for sub in subs:
        cat = os.path.join(home, sub, "catalog-v6")
        if os.path.isfile(cat):
            return cat
    return ""


def read_chat_models(cat_path):
    """解密 catalog-v6 并取 chat 场景条目（逐字段原样）。"""
    sub = os.path.basename(os.path.dirname(cat_path))
    with io.open(cat_path, "rb") as fh:
        blob = fh.read().decode("ascii", "replace")
    plain = json.loads(qmc_decrypt(blob, sub).decode("utf-8"))
    return [m for m in (plain.get("chat") or [])
            if isinstance(m, dict) and m.get("key")]


def summarize(old, new):
    """打印两份快照的差异摘要；返回变化条数。"""
    old_map = {m["key"]: m for m in old or []}
    new_map = {m["key"]: m for m in new}
    changed = 0
    for k in sorted(set(old_map) | set(new_map)):
        a, b = old_map.get(k), new_map.get(k)
        if a is None:
            print("  + 新增模型 %s (%s)" % (k, b.get("display_name") or ""))
            changed += 1
            continue
        if b is None:
            print("  - 下架模型 %s" % k)
            changed += 1
            continue
        diffs = []
        for f in COMPARE_FIELDS:
            if a.get(f) != b.get(f):
                if f in ("context_config", "thinking_config", "promotion",
                         "strategies", "minimal_version"):
                    diffs.append(f)
                else:
                    diffs.append("%s: %r -> %r" % (f, a.get(f), b.get(f)))
        if diffs:
            changed += 1
            print("  * %s" % k)
            for d in diffs:
                print("      " + d)
    if not changed:
        print("  （与当前快照一致，无需更新）")
    return changed


def splice_inline(src, var, models):
    """把内嵌 JSON 块替换为新内容（保持 r'''...''' 包裹与缩进风格）。"""
    pat = re.compile(r"(" + var + r" = r'''\n)(.*?)(\n''')", re.DOTALL)
    m = pat.search(src)
    if not m:
        raise SystemExit("inline block %s not found in qoder_catalog.py" % var)
    body = json.dumps(models, indent=2, ensure_ascii=False)
    if m.group(2).strip() == body.strip():
        return src, False
    return src[:m.start(2)] + body + src[m.end(2):], True


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true",
                    help="只打印差异，不写任何文件")
    ap.add_argument("--no-inline", action="store_true",
                    help="不更新 qoder_catalog.py 里的内嵌冻结副本")
    args = ap.parse_args()

    catalog_py = os.path.join(HERE, "qoder_catalog.py")
    src = ""
    if not args.no_inline and not args.dry_run:
        with io.open(catalog_py, encoding="utf-8") as fh:
            src = fh.read()

    failed = 0
    for realm, home, out_name, var in REALMS:
        print("=" * 66)
        print("[%s] client cache: %s" % (realm, home))
        cat = client_catalog_path(home)
        if not cat:
            print("  ! 未找到目录缓存（该区域客户端未安装/未登录），跳过")
            failed += 1
            continue
        try:
            models = read_chat_models(cat)
        except Exception as exc:
            print("  ! 解密失败: %s" % exc)
            failed += 1
            continue
        out_path = os.path.join(HERE, out_name)
        old = []
        if os.path.isfile(out_path):
            try:
                with io.open(out_path, encoding="utf-8") as fh:
                    old = json.load(fh)
            except Exception:
                old = []
        print("  %d models (was %d)" % (len(models), len(old)))
        summarize(old, models)
        if args.dry_run:
            continue
        with io.open(out_path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(models, indent=2, ensure_ascii=False) + "\n")
        print("  -> 写入 %s" % out_name)
        if src:
            src, changed = splice_inline(src, var, models)
            print("  -> 内嵌副本%s" % ("已更新" if changed else "无变化"))

    if src and not args.dry_run:
        with io.open(catalog_py, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(src)
        print("=" * 66)
        print("写入 qoder_catalog.py（内嵌冻结副本）")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
