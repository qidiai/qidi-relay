# -*- coding: utf-8 -*-
"""依赖扫描：判断 _test_qoder.py 的哪些段落是「叶子段」（搬走后不会让后续段崩）。

动机（第三批回滚的教训）：段里可能定义供**后续段继承**的全局（如 `time = _time_for_11`、
`_A`、`_orig_sleep2`）。直接搬走就会让后面的段 NameError —— 而 legacy 只在**整体跑**时才暴露，
新套件自己反而是绿的。所以每批迁移前先跑这个扫描。

用法: python tests/_tools/dep_scan.py [段号1 段号2 ...]
"""
import io, os, re, sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LEG = os.path.join(ROOT, "_test_qoder.py")

MARK = re.compile(r'^print\("\[([0-9.]+)\]')
DEF = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=")          # 模块级赋值
DEF_f = re.compile(r"^(?:async\s+)?def\s+([A-Za-z_][A-Za-z0-9_]*)")
DEF_i = re.compile(r"^(?:import\s+([A-Za-z_][A-Za-z0-9_]*)|from\s+[\w.]+\s+import\s+([A-Za-z_][A-Za-z0-9_]*))")


def sections(lines):
    idx = [(m.group(1), i) for i, l in enumerate(lines) if (m := MARK.match(l))]
    out = []
    for k, (tag, i) in enumerate(idx):
        j = idx[k + 1][1] if k + 1 < len(idx) else len(lines)
        out.append((tag, i, j))
    return out


def defined_globals(lines, i, j):
    names = set()
    for l in lines[i:j]:
        if not l or l[0] in " \t#":
            continue
        for rx, grp in ((DEF, 1), (DEF_f, 1)):
            m = rx.match(l)
            if m:
                names.add(m.group(grp))
                break
        else:
            m = DEF_i.match(l)
            if m:
                names.add(m.group(1) or m.group(2))
    # 过滤噪声：循环变量（for k in ...）、通用短名（k/url/enc/sample…）不是「段定义给后续用的全局」。
    # 只保留下划线前缀或长度 >= 4 的名字 —— 实测未过滤时每个段都被误判成「被依赖」。
    loopy = set()
    for l in lines[i:j]:
        m = re.match(r"\s*for\s+([A-Za-z_][A-Za-z0-9_]*)\s+in\b", l)
        if m:
            loopy.add(m.group(1))
    return {n for n in names
            if not n.startswith("__") and n not in loopy
            and (n.startswith("_") or len(n) >= 4)}


def main(argv):
    lines = io.open(LEG, encoding="utf-8").read().split("\n")
    secs = sections(lines)
    want = set(argv[1:])
    only_leaf = "--leaf-only" in argv
    print("segments found: %d" % len(secs))
    for tag, i, j in secs:
        if want and tag not in want:
            continue
        gl = defined_globals(lines, i, j)
        if not gl:
            continue
        used_later = {}
        for tag2, i2, j2 in secs:
            if i2 <= i:
                continue
            body = "\n".join(lines[i2:j2])
            hit = sorted(n for n in gl
                         if re.search(r"\b%s\b" % re.escape(n), body))
            if hit:
                used_later[tag2] = hit
        if used_later:
            if only_leaf:
                continue
            print("[%s] 被后续段依赖，**不可单独迁移**：" % tag)
            for t2, hit in used_later.items():
                print("      -> [%s] 用了 %s" % (t2, ", ".join(hit)))
        else:
            print("[%s] 叶子段（无后续引用）可独立成套件；定义了 %d 个全局" % (tag, len(gl)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
