# -*- coding: utf-8 -*-
"""迁移自 _test_qoder.py 的 [39] 段（P0-2 测试工程化）。

段内注释与「为什么这么测」的理由原样保留 —— 那是前面几十轮踩坑换来的。
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _suite_head import *  # noqa: F401,F403
declare_suite(__file__, ["qoder_proxy.py"])

P, A = bootstrap()

print("[39] upstream credit 字段映射")
print()
print("[39] issue #22：上游 credit（复数）字段映射 + 聚合字段表 + jsonl 落盘")
import shutil as _sh39
import tempfile as _tf39

_U39 = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15,
        "prompt_cache_hit_tokens": 2,
        "completion_tokens_details": {"reasoning_tokens": 3, "cached_tokens": 1},
        "credits": 97.96, "original_credits": 100.5, "billable": True}
_E39 = P._extract_usage(_U39)
check("#60-1 【命门】上游复数 credits=97.96 → 提取出的 credit 等于该值（修复前恒为 0）",
      _E39.get("credit") == 97.96, _E39)
check("#60-2 兜底：上游只给单数 credit 时仍能读到",
      P._extract_usage({"credit": 12.5}).get("credit") == 12.5,
      P._extract_usage({"credit": 12.5}))
check("#60-3 优先级：单数 1.0 与复数 2.0 同时存在 → 取**复数** credits",
      P._extract_usage({"credit": 1.0, "credits": 2.0}).get("credit") == 2.0,
      P._extract_usage({"credit": 1.0, "credits": 2.0}))
check("#60-4 双缺 → 0（行为不变）",
      P._extract_usage({"prompt_tokens": 1}).get("credit") == 0,
      P._extract_usage({"prompt_tokens": 1}))
check("#60-5 original_credits 被提取",
      _E39.get("original_credits") == 100.5, _E39)
check("#60-6 original_credits **参与聚合**（在 USAGE_FIELDS 里）",
      "original_credits" in P.USAGE_FIELDS, P.USAGE_FIELDS)
check("#60-7 billable 被记录进行（提取产物里可见）",
      _E39.get("billable") is True, _E39)
check("#60-8 billable **不进**聚合字段表（只记行、不累加）",
      "billable" not in P.USAGE_FIELDS, P.USAGE_FIELDS)
check("#60-9 不变性：prompt/completion/reasoning/cached/total 五个 token 字段"
      "提取值不变（10/5/3/2/15）",
      (_E39.get("prompt_tokens"), _E39.get("completion_tokens"),
       _E39.get("reasoning_tokens"), _E39.get("cached_tokens"),
       _E39.get("total_tokens")) == (10, 5, 3, 2, 15), _E39)
_orig_ud39 = P.USAGE_DIR
_ud39 = _tf39.mkdtemp(prefix="qd-usage60-")
P.USAGE_DIR = _ud39
try:
    try:
        P.record_usage("Qwen3.8-Flash", dict(_U39))
    except Exception as _e39:
        pass
    _log39 = os.path.join(_ud39, os.path.basename(P.USAGE_LOG))
    _row39 = {}
    if os.path.isfile(_log39):
        with open(_log39, encoding="utf-8") as _fh39:
            _lines39 = [l for l in _fh39.read().splitlines() if l.strip()]
        if _lines39:
            try:
                _row39 = json.loads(_lines39[-1])
            except Exception:
                _row39 = {}
    check("#60-10 端到端：写进 %s 的那一行 credit == 97.96（不再是 0）"
          % os.path.basename(P.USAGE_LOG),
          _row39.get("credit") == 97.96,
          {_k: _row39.get(_k) for _k in ("credit", "original_credits",
                                         "prompt_tokens", "model")})
finally:
    P.USAGE_DIR = _orig_ud39
    _sh39.rmtree(_ud39, ignore_errors=True)

finish()
