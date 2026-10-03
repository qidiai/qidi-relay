# -*- coding: utf-8 -*-
"""训练语料构建器（阶段 E / 方案 §7.6）：
local_YYYYMM.jsonl（本机采集）+ corpus_YYYYMM.jsonl
（corpus_pull.py 拉取的云端语料）→ 去重 → 分桶
统计 → train/val 8:2 → dataset/{train,val}.jsonl
+ manifest.json。

流水线（§7.6 决策顺序）：
  1. 精确去重：messages 规范化序列化（sort_keys +
     紧凑分隔符）sha256——同 messages 不同 meta
     （ts/latency 等）视为同一条，留首见；
  2. 注水过滤：water_suspect 样本默认过滤
     （§7.5 决策表"训练构建默认过滤，可复核捞回"），
     --include-water-suspect 捞回；
  3. 短样本过滤：assistant 内容 < --min-assistant-
     chars 默认过滤但计数入 manifest（§7.4：
     min_assistant_chars 缺省值先统计后定——本脚本
     默认阈值 16 为本仓自主选择的占位值，manifest
     的 short_count/short_drop_rate 供回头定阈值）；
  4. 按 model/role（兼 provider/protocol/source）
     分桶统计；
  5. 固定种子洗牌后 train/val 切分（比例可调，
     缺省 8:2——可复现）。

输出格式：OpenAI SFT 兼容 {"messages":[...]}
（assistant 内容含 thinking 与否由 --include-thinking
开关控制；tool_calls 原样带出）。本地样本 id 规则
local-YYYYMMDD-序号（§7.2，采集侧已保证，与云端
递增整数 id 按文件名隔离）。

TODO（minhash 近邻去重，§7.6 可选项）：精确
hash 只杀完全重复；跨样本近似重复（同 prompt 小
改）留 minhash（如 datasketch 算法自实现）后补。

纯标准库（json/hashlib/argparse/pathlib/random/
collections）——零新依赖；落盘走 flush+fsync
（corpus_pull.py:56-60 纪律）。
"""
import argparse
import hashlib
import json
import os
import random
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

# 脚本位于 scripts/——仓库根进 sys.path 才能 import
# proxy_core（settings 路径常量模式，§7 测试纪律：
# 新模块一律走 settings.py 路径常量，可注入）
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from proxy_core import settings as S  # noqa: E402

# 固定洗牌种子（可复现 train/val 切分）
DEFAULT_SEED = 20261003
# 短样本默认阈值（§7.4"先统计后定"的占位值——
# 本仓自主选择；manifest 计数供回头校准）
DEFAULT_MIN_ASSISTANT_CHARS = 16


def _log(msg: str) -> None:
    print("%s %s" % (
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg))


def _assistant_chars(sample: dict) -> int:
    """assistant 可见文本长度（thinking 不计入——
    短样本判据是"有效回复"长度，工具确认/ack 类
    短样本 thinking 无关）。"""
    asst = sample.get("assistant") or {}
    content = asst.get("content") or ""
    return len(content)


def _dedup_key(sample: dict) -> str:
    """messages 规范化序列化 hash（精确去重键）。"""
    msgs = sample.get("messages") or []
    canonical = json.dumps(
        msgs, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _to_text(content) -> str:
    """content 可能是字符串或多模态块列表
    （[{"type":"text","text":...}, ...]）→ 纯文本
    （SFT 输出只带文本——图片占位符不入训练）。"""
    if isinstance(content, list):
        parts = []
        for blk in content:
            if isinstance(blk, dict) and blk.get("type") == "text":
                parts.append(blk.get("text") or "")
        return "\n".join(parts)
    return content or ""


def _sft_messages(sample: dict, include_thinking: bool) -> list:
    """OpenAI SFT 兼容 messages（原始 messages +
    assistant 收尾；thinking 并入与否由开关控制）。"""
    out = []
    for m in sample.get("messages") or []:
        if not isinstance(m, dict):
            continue
        out.append({"role": m.get("role"),
                    "content": _to_text(m.get("content"))})
    asst = sample.get("assistant") or {}
    content = _to_text(asst.get("content"))
    thinking = asst.get("thinking") or ""
    if include_thinking and thinking:
        content = thinking + "\n\n" + content
    msg = {"role": "assistant", "content": content}
    tool_calls = asst.get("tool_calls") or []
    if tool_calls:
        msg["tool_calls"] = tool_calls
    out.append(msg)
    return out


def _sample_tokens(sample: dict) -> int:
    usage = sample.get("usage") or {}
    try:
        return int(usage.get("total") or 0)
    except (TypeError, ValueError):
        return 0


def _bucket(counter: Counter, key, tokens: int) -> None:
    entry = counter.get(key)
    if entry is None:
        entry = counter[key] = {"count": 0, "tokens": 0}
    entry["count"] += 1
    entry["tokens"] += tokens


def build(corpus_dir: Path, out_dir: Path, *,
          val_ratio: float = 0.2,
          min_assistant_chars: int = DEFAULT_MIN_ASSISTANT_CHARS,
          include_thinking: bool = False,
          include_water_suspect: bool = False,
          seed: int = DEFAULT_SEED) -> dict:
    """读语料 → 去重 → 过滤 → 分桶 → 切分 → 落盘。
    返回 manifest dict（已写盘）。"""
    corpus_dir = Path(corpus_dir)
    out_dir = Path(out_dir)
    val_ratio = min(0.9, max(0.0, float(val_ratio)))
    min_assistant_chars = max(0, int(min_assistant_chars))

    # ── 读（local_* = 本机采集；corpus_* = 云端拉取）
    files = sorted(
        [p for p in corpus_dir.glob("local_*.jsonl")]
        + [p for p in corpus_dir.glob("corpus_*.jsonl")])
    samples = []
    bad_lines = 0
    for path in files:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    samples.append(json.loads(line))
                except ValueError:
                    bad_lines += 1
    input_count = len(samples)
    _log("读 %d 个语料文件、%d 条样本（坏行 %d）"
         % (len(files), input_count, bad_lines))

    # ── 1. 精确去重（留首见）
    seen = set()
    unique = []
    for s in samples:
        key = _dedup_key(s)
        if key in seen:
            continue
        seen.add(key)
        unique.append(s)
    dedup_dropped = input_count - len(unique)

    # ── 2. 注水过滤（默认过滤，可复核捞回）
    water_filtered = 0
    if not include_water_suspect:
        kept = []
        for s in unique:
            if s.get("water_suspect"):
                water_filtered += 1
            else:
                kept.append(s)
        unique = kept

    # ── 3. 短样本打标 + 过滤（默认过滤但计数）
    short_count = sum(
        1 for s in unique
        if _assistant_chars(s) < min_assistant_chars)
    if min_assistant_chars > 0:
        unique = [s for s in unique
                  if _assistant_chars(s) >= min_assistant_chars]
    short_filtered = short_count if min_assistant_chars > 0 else 0
    eligible = unique

    # ── 4. 分桶统计（按 model/role，兼 provider/
    #     protocol/source——来源分布）
    by_model, by_role = Counter(), Counter()
    by_provider, by_protocol, by_source = (Counter(),
                                           Counter(),
                                           Counter())
    for s in eligible:
        tokens = _sample_tokens(s)
        _bucket(by_model, s.get("model") or "unknown", tokens)
        _bucket(by_role, s.get("role") or "unknown", tokens)
        _bucket(by_provider,
                s.get("provider") or "unknown", tokens)
        _bucket(by_protocol,
                s.get("protocol") or "unknown", tokens)
        _bucket(by_source,
                "cloud" if str(s.get("id") or "").isdigit()
                else "local", tokens)

    # ── 5. 固定种子洗牌 → train/val 切分（缺省 8:2）
    # 按 session_id 分组切分（R6 项目9：同会话多轮
    # 样本高度重叠，全局洗牌会把同会话样本跨
    # train/val 泄漏——同一会话的样本整体入
    # train 或整体入 val，不跨切分；无 session_id
    # 的匿名样本各自成组（互不重叠））。组间
    # 洗牌，组按序填入 train 至目标条数（末组
    # 可能越界——组原子性优先于精确比例）。
    rnd = random.Random(seed)
    groups: dict = {}
    anon_i = 0
    for s in eligible:
        sid = s.get("session_id")
        if isinstance(sid, str) and sid:
            groups.setdefault(sid, []).append(s)
        else:
            groups.setdefault(
                ("__anon__", anon_i), []).append(s)
            anon_i += 1
    group_keys = list(groups)
    rnd.shuffle(group_keys)
    n_train = int(len(eligible) * (1.0 - val_ratio))
    train, val = [], []
    acc = 0
    for gk in group_keys:
        g = groups[gk]
        if acc < n_train:
            train.extend(g)
            acc += len(g)
        else:
            val.extend(g)

    # ── 落盘（SFT 兼容格式；flush+fsync 纪律）
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, part in (("train", train), ("val", val)):
        path = out_dir / ("%s.jsonl" % name)
        with open(path, "w", encoding="utf-8") as f:
            for s in part:
                rec = {"messages": _sft_messages(
                    s, include_thinking)}
                f.write(json.dumps(
                    rec, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        _log("%s: %d 条 -> %s" % (name, len(part), path))

    redact_hits = sum(
        1 for s in samples
        if int((s.get("redact_hits") or 0) or 0) > 0)
    manifest = {
        "generated_at": datetime.now().isoformat(
            timespec="seconds"),
        "corpus_dir": str(corpus_dir),
        "params": {
            "val_ratio": val_ratio,
            "min_assistant_chars": min_assistant_chars,
            "include_thinking": include_thinking,
            "include_water_suspect": include_water_suspect,
            "seed": seed,
        },
        "input": input_count,
        "dedup_dropped": dedup_dropped,
        "dedup_note": "去重键仅 messages（canonical"
                      " json，sort_keys）；同 prompt 异"
                      "回复的样本并留首见",
        "unique": input_count - dedup_dropped,
        "water_filtered": water_filtered,
        "short_count": short_count,
        "short_filtered": short_filtered,
        "bad_lines": bad_lines,
        "train": len(train),
        "val": len(val),
        # token 量
        "tokens": {
            "train": sum(_sample_tokens(s) for s in train),
            "val": sum(_sample_tokens(s) for s in val),
            "input": sum(_sample_tokens(s) for s in samples),
        },
        # 比率（分母 0 → 0.0）
        "water_filter_rate": (
            water_filtered / (input_count - dedup_dropped)
            if (input_count - dedup_dropped) else 0.0),
        "redact_hit_rate": (
            redact_hits / input_count if input_count else 0.0),
        # §7.4 丢弃率公式：len(samples[assistant_chars<T])/N
        "short_drop_rate": (
            short_count / input_count if input_count else 0.0),
        # 来源分布
        "by_model": dict(by_model),
        "by_role": dict(by_role),
        "by_provider": dict(by_provider),
        "by_protocol": dict(by_protocol),
        "by_source": dict(by_source),
    }
    manifest_path = out_dir / "manifest.json"
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False,
                  indent=2)
        f.flush()
        os.fsync(f.fileno())
    _log("manifest -> %s" % manifest_path)
    return manifest


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="训练语料构建（方案 §7.6）")
    ap.add_argument("--corpus-dir", type=Path,
                    default=Path(S.CORPUS_DIR),
                    help="语料目录（默认 settings.CORPUS_DIR"
                         "——路径常量可注入）")
    ap.add_argument("--out-dir", type=Path,
                    default=None,
                    help="输出目录（默认 <corpus-dir>/dataset）")
    ap.add_argument("--val-ratio", type=float,
                    default=0.2,
                    help="val 切分比例（缺省 0.2 = 8:2）")
    ap.add_argument("--min-assistant-chars", type=int,
                    default=DEFAULT_MIN_ASSISTANT_CHARS,
                    help="短样本阈值（assistant 可见字符"
                         "数，低于则过滤但计数；缺省 %d，"
                         "§7.4 先统计后定）"
                    % DEFAULT_MIN_ASSISTANT_CHARS)
    ap.add_argument("--include-thinking",
                    action="store_true",
                    help="thinking 并入 assistant 内容"
                         "（SFT 输出默认不含）")
    ap.add_argument("--include-water-suspect",
                    action="store_true",
                    help="注水嫌疑样本捞回（默认过滤，"
                         "§7.5 可复核捞回）")
    ap.add_argument("--seed", type=int,
                    default=DEFAULT_SEED,
                    help="train/val 洗牌种子（缺省 %d"
                         "——可复现切分）" % DEFAULT_SEED)
    args = ap.parse_args(argv)
    out_dir = args.out_dir or (
        Path(args.corpus_dir) / "dataset")
    m = build(args.corpus_dir, out_dir,
              val_ratio=args.val_ratio,
              min_assistant_chars=args.min_assistant_chars,
              include_thinking=args.include_thinking,
              include_water_suspect=args.include_water_suspect,
              seed=args.seed)
    _log("汇总：输入 %d → 去重 -%d → 注水 -%d → 短样本 "
         "-%d → train %d / val %d"
         % (m["input"], m["dedup_dropped"],
            m["water_filtered"], m["short_filtered"],
            m["train"], m["val"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
