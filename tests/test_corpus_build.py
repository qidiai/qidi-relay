# -*- coding: utf-8 -*-
"""训练语料构建器（scripts/corpus_build.py）测试：
去重 / 注水过滤 / 短样本 / 8:2 切分 / manifest
字段 / SFT 形（R6 项目9 持久化——把 T7 的
"18/18 临时自测"提交化）。

R6 项目9 修复点同步验收：
- 8:2 切分按 session_id 分组（同会话多轮样本
  高度重叠，全局洗牌会跨 train/val 泄漏——
  同会话样本整体入 train 或整体入 val）；
- manifest 增 dedup_note（如实披露：去重键
  仅 messages，同 prompt 异回复并留首见）。

R9 云端样本 schema 兼容（实证 bug 修复）：
- 云端拉取样本答案在顶层 response（无顶层
  assistant 键，messages 末条常是 tool）→
  _assistant_chars 三级 fallback（① assistant
  .content → ② response[str|dict，dict 取
  content 字段] → ③ messages 末条 assistant）
  + _sft_messages 云端收尾 assistant 由
  response 文本补齐（末条已是 assistant 且
  内容相同则不重复 append）。

纯 TMP 目录零仓库数据文件；脚本经 importlib
按路径加载（scripts/ 非包）。

跑法：py -3.12 -m pytest tests/test_corpus_build.py -v
"""

import importlib.util
import json
import pathlib
import sys

import pytest

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

# 加载 scripts/corpus_build.py（非包脚本，
# 按路径 importlib 加载——脚本自身把仓库根
# 进 sys.path 以 import proxy_core）
_SPEC = importlib.util.spec_from_file_location(
    "corpus_build",
    str(BASE / "scripts" / "corpus_build.py"))
CB = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(CB)


def _sample(session_id, content, *, assistant=None,
            water=False, model="GLM-5.3",
            role="coder"):
    """构造语料样本（corpus.py 落盘形态）。"""
    return {
        "id": "local-20261003-000001",
        "session_id": session_id,
        "ts": "2026-10-03T00:00:00",
        "client": "***",
        "role": role,
        "model": model,
        "model_served": model,
        "provider": "pool",
        "protocol": "openai",
        "messages": [{"role": "user",
                       "content": content}],
        "assistant": assistant or {
            "content": "这是足够长的回复内容用于通过短样本阈值",
            "thinking": "",
            "tool_calls": [],
            "finish": "stop"},
        "tools": [],
        "usage": {"prompt": 3, "completion": 5,
                  "cache_read": 0, "total": 8},
        "ttft_content_ms": 100,
        "total_ms": 200,
        "stream": False,
        "partial": False,
        "body_truncated": False,
        "water_suspect": water,
        "redact_hits": 0,
    }


def _write(corpus: pathlib.Path, samples: list) -> None:
    """样本写 local_202610.jsonl（UTF-8）。"""
    corpus.mkdir(parents=True, exist_ok=True)
    (corpus / "local_202610.jsonl").write_text(
        "\n".join(json.dumps(s, ensure_ascii=False)
                  for s in samples) + "\n",
        encoding="utf-8")


def _all_recs(out_dir: pathlib.Path) -> list:
    """train.jsonl + val.jsonl 全部记录。"""
    recs = []
    for name in ("train.jsonl", "val.jsonl"):
        p = out_dir / name
        if p.exists():
            recs.extend(
                json.loads(x) for x in
                p.read_text(encoding="utf-8").splitlines()
                if x.strip())
    return recs


def _count_lines(path: pathlib.Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for x in
               path.read_text(encoding="utf-8").splitlines()
               if x.strip())


# ─── 1. 精确去重 ─────────────────────────────

def test_dedup_same_prompt_diff_reply_keeps_first(
        tmp_path):
    # 去重键仅 messages（canonical json）：
    # 同 prompt 异回复 → 并留首见（meta 不同
    # 不影响去重判定）
    corpus = tmp_path / "corpus_local"
    a = _sample("s1", "问题一")
    a["id"] = "local-20261003-000001"
    a["assistant"]["content"] = "首见回复" + "x" * 20
    b = _sample("s1", "问题一")
    b["id"] = "local-20261003-000002"
    b["ts"] = "2026-10-03T01:00:00"
    b["assistant"]["content"] = "后见回复" + "y" * 20
    _write(corpus, [a, b])
    out = tmp_path / "out"
    m = CB.build(corpus, out)
    assert m["input"] == 2
    assert m["dedup_dropped"] == 1
    assert m["unique"] == 1
    # dedup_note 如实披露去重键口径
    assert "messages" in m["dedup_note"]
    assert "首见" in m["dedup_note"]
    # 留首见：SFT 输出含首见回复、不含后见回复
    recs = _all_recs(out)
    assert len(recs) == 1
    assert recs[0]["messages"][-1]["content"] \
        .startswith("首见回复")
    assert "后见回复" not in json.dumps(
        recs, ensure_ascii=False)


# ─── 2. 注水过滤 ─────────────────────────────

def test_water_suspect_filter_and_recover(tmp_path):
    # 注水嫌疑样本默认过滤（§7.5 决策表），
    # include_water_suspect=True 可复核捞回
    corpus = tmp_path / "corpus_local"
    clean = _sample("s1", "干净样本")
    dirty = _sample("s2", "注水样本", water=True)
    _write(corpus, [clean, dirty])
    out = tmp_path / "out"
    m = CB.build(corpus, out)
    assert m["input"] == 2
    assert m["water_filtered"] == 1
    recs = _all_recs(out)
    assert len(recs) == 1
    assert recs[0]["messages"][0]["content"] \
        == "干净样本"
    # 捞回
    out2 = tmp_path / "out2"
    m2 = CB.build(corpus, out2,
                  include_water_suspect=True)
    assert m2["water_filtered"] == 0
    assert len(_all_recs(out2)) == 2


# ─── 3. 短样本过滤 ───────────────────────────

def test_short_sample_filtered_counted(tmp_path):
    # 短样本（assistant 可见文本 < 16 字符）
    # 默认过滤但计数入 manifest
    corpus = tmp_path / "corpus_local"
    short = _sample("s1", "短样本")
    short["assistant"]["content"] = "太短"
    long_ = _sample("s2", "长样本")
    _write(corpus, [short, long_])
    out = tmp_path / "out"
    m = CB.build(corpus, out)
    assert m["input"] == 2
    assert m["short_count"] == 1
    assert m["short_filtered"] == 1
    recs = _all_recs(out)
    assert len(recs) == 1
    assert recs[0]["messages"][0]["content"] \
        == "长样本"


# ─── 4. train/val 8:2 切分 ───────────────────

def test_train_val_ratio_8_2(tmp_path):
    # 8:2 切分（独立会话样本——组原子性下
    # 比例精确）；落盘条数与 manifest 一致
    corpus = tmp_path / "corpus_local"
    samples = [_sample(f"s{i}", f"问题{i}")
               for i in range(50)]
    _write(corpus, samples)
    out = tmp_path / "out"
    m = CB.build(corpus, out)
    assert m["train"] + m["val"] == 50
    assert m["train"] == 40
    assert m["val"] == 10
    assert _count_lines(out / "train.jsonl") == 40
    assert _count_lines(out / "val.jsonl") == 10


# ─── 5. session_id 分组切分（R6 项目9）────────

def test_session_grouped_split_no_leak(tmp_path):
    # 同会话多轮样本（messages 各异→去重键
    # 不同）整体入 train 或整体入 val——
    # 不跨切分（旧实现全局洗牌会泄漏）
    corpus = tmp_path / "corpus_local"
    samples = []
    # 10 个会话 × 每会话 3 轮
    for i in range(10):
        for j in range(3):
            samples.append(
                _sample(f"sess-{i}", f"问题{i}-{j}"))
    _write(corpus, samples)
    out = tmp_path / "out"
    m = CB.build(corpus, out)
    assert m["train"] + m["val"] == 30
    train_text = (out / "train.jsonl").read_text(
        encoding="utf-8")
    val_text = (out / "val.jsonl").read_text(
        encoding="utf-8")
    for i in range(10):
        turns = [f"问题{i}-{j}" for j in range(3)]
        in_train = [t in train_text for t in turns]
        in_val = [t in val_text for t in turns]
        # 同会话 3 轮同侧（全 train 或全 val）
        assert all(in_train) or all(in_val), i
        assert not (any(in_train) and any(in_val)), i


# ─── 6. manifest 字段 ────────────────────────

def test_manifest_fields(tmp_path):
    # manifest 字段齐全（参数/分布/比率/
    # dedup_note/token 量），落盘 manifest.json
    # 与返回值一致
    corpus = tmp_path / "corpus_local"
    _write(corpus, [_sample("s1", "问题一",
                            model="GLM-5.3",
                            role="coder")])
    out = tmp_path / "out"
    m = CB.build(corpus, out)
    for k in ("generated_at", "corpus_dir", "params",
              "input", "dedup_dropped", "dedup_note",
              "unique", "water_filtered", "short_count",
              "short_filtered", "bad_lines", "train",
              "val", "tokens", "water_filter_rate",
              "redact_hit_rate", "short_drop_rate",
              "by_model", "by_role", "by_provider",
              "by_protocol", "by_source"):
        assert k in m, k
    assert m["params"]["val_ratio"] == 0.2
    assert m["params"]["min_assistant_chars"] == \
        CB.DEFAULT_MIN_ASSISTANT_CHARS
    assert m["params"]["seed"] == CB.DEFAULT_SEED
    assert m["tokens"]["train"] + m["tokens"]["val"] \
        == m["tokens"]["input"]
    assert m["by_model"]["GLM-5.3"]["count"] == 1
    assert m["by_model"]["GLM-5.3"]["tokens"] == 8
    assert m["by_role"]["coder"]["count"] == 1
    assert m["by_provider"]["pool"]["count"] == 1
    assert m["by_protocol"]["openai"]["count"] == 1
    assert m["by_source"]["local"]["count"] == 1
    on_disk = json.loads(
        (out / "manifest.json").read_text(
            encoding="utf-8"))
    assert on_disk["input"] == m["input"]
    assert on_disk["dedup_note"] == m["dedup_note"]


# ─── 7. SFT 输出形 ───────────────────────────

def test_sft_shape(tmp_path):
    # SFT 形：{"messages":[...]}（仅此一键），
    # user/assistant 角色；thinking 仅
    # include_thinking=True 并入 assistant 内容
    # 首部；tool_calls 原样带出
    corpus = tmp_path / "corpus_local"
    s = _sample("s1", "问题一")
    s["assistant"]["thinking"] = "思考过程"
    s["assistant"]["tool_calls"] = [{
        "id": "call_1", "type": "function",
        "function": {"name": "lookup",
                     "arguments": "{}"}}]
    _write(corpus, [s])
    # 默认不含 thinking
    out = tmp_path / "out"
    CB.build(corpus, out)
    rec = _all_recs(out)[0]
    assert set(rec.keys()) == {"messages"}
    roles = [mm["role"] for mm in rec["messages"]]
    assert roles == ["user", "assistant"]
    assert rec["messages"][0]["content"] == "问题一"
    assert rec["messages"][1]["content"] == \
        "这是足够长的回复内容用于通过短样本阈值"
    assert "思考过程" not in json.dumps(
        rec, ensure_ascii=False)
    assert rec["messages"][1]["tool_calls"][0][
        "function"]["name"] == "lookup"
    # include_thinking=True → thinking 并入
    # assistant 内容首部
    out2 = tmp_path / "out2"
    CB.build(corpus, out2, include_thinking=True)
    rec2 = _all_recs(out2)[0]
    assert rec2["messages"][1]["content"].startswith(
        "思考过程\n\n")
# ─── 8. 云端样本 schema 兼容（R9）────────

CLOUD_RESP = "这是云端回复的足够长文本内容用于通过阈值"


def _cloud_sample(session_id, user_q, response, *,
                  model="GLM-5.3"):
    """云端拉取样本真实 schema（实证）：顶层
    id（整数）/messages/response/model/ts/
    tool_calls——答案在顶层 response，无顶层
    assistant 键，messages 为 assistant/tool
    交错历史、末条常是 tool。"""
    return {
        "id": 7161001,
        "messages": [
            {"role": "user", "content": user_q},
            {"role": "assistant",
             "content": "我需要先查询再回答",
             "tool_calls": [{
                 "id": "call_1",
                 "type": "function",
                 "function": {
                     "name": "lookup",
                     "arguments": "{}"}}]},
            {"role": "tool",
             "content": "查询结果：……"},
        ],
        "response": response,
        "model": model,
        "ts": "2026-09-15T00:00:00",
        "tool_calls": [],
        "session_id": session_id,
        "usage": {"prompt": 3, "completion": 5,
                  "total": 8},
    }


def _write_cloud(corpus: pathlib.Path,
                 samples: list) -> None:
    """云端样本写 corpus_202610.jsonl。"""
    corpus.mkdir(parents=True, exist_ok=True)
    (corpus / "corpus_202610.jsonl").write_text(
        "\n".join(json.dumps(s, ensure_ascii=False)
                  for s in samples) + "\n",
        encoding="utf-8")


def test_cloud_sample_passes_filter_into_train(
        tmp_path):
    # 云端样本（答案在顶层 response）：修复前
    # _assistant_chars 只读 assistant.content
    # → 恒 0 → 全部被短样本过滤（实证
    # 7040→0）；修复后按 response 长度过
    # 16 字符阈值进 train（5 组会话→
    # train 4 / val 1）
    corpus = tmp_path / "corpus_local"
    _write_cloud(corpus, [
        _cloud_sample("cs-%d" % i, "云端问题%d" % i,
                      CLOUD_RESP + suffix)
        for i, suffix in enumerate(["", "二",
                                    "三", "四",
                                    "五"])])
    out = tmp_path / "out"
    m = CB.build(corpus, out)
    assert m["input"] == 5
    assert m["short_count"] == 0
    assert m["short_filtered"] == 0
    assert m["train"] == 4
    assert m["val"] == 1
    assert _count_lines(out / "train.jsonl") == 4


def test_cloud_train_closing_assistant_is_response(
        tmp_path):
    # 云端 train 记录：收尾 assistant =
    # response 文本；messages 末条常是
    # tool，append assistant 收尾正好补齐
    # SFT 形状（roles 以 tool→assistant
    # 收尾）
    corpus = tmp_path / "corpus_local"
    resp_map = {
        "云端问题甲": CLOUD_RESP,
        "云端问题乙": CLOUD_RESP + "乙",
    }
    _write_cloud(corpus, [
        _cloud_sample("cs-甲", "云端问题甲",
                      resp_map["云端问题甲"]),
        _cloud_sample("cs-乙", "云端问题乙",
                      resp_map["云端问题乙"])])
    out = tmp_path / "out"
    m = CB.build(corpus, out)
    assert m["train"] == 1
    rec = json.loads(
        (out / "train.jsonl").read_text(
            encoding="utf-8").splitlines()[0])
    roles = [mm["role"] for mm in rec["messages"]]
    assert roles == ["user", "assistant",
                     "tool", "assistant"]
    q = rec["messages"][0]["content"]
    assert rec["messages"][-1]["content"] ==         resp_map[q]


def test_cloud_response_dict_form(tmp_path):
    # response 可能是 dict——取 content 字段
    # （经 _to_text 归一）后同样过滤+收尾
    corpus = tmp_path / "corpus_local"
    _write_cloud(corpus, [
        _cloud_sample("cs-甲", "云端问题甲",
                      {"content": CLOUD_RESP}),
        _cloud_sample("cs-乙", "云端问题乙",
                      {"content": CLOUD_RESP + "乙"})])
    out = tmp_path / "out"
    m = CB.build(corpus, out)
    assert m["short_count"] == 0
    assert m["train"] == 1
    rec = json.loads(
        (out / "train.jsonl").read_text(
            encoding="utf-8").splitlines()[0])
    assert rec["messages"][-1]["content"] in (
        CLOUD_RESP, CLOUD_RESP + "乙")


def test_cloud_last_assistant_same_as_response_no_dup(
        tmp_path):
    # messages 末条已是 assistant 且内容与
    # response 相同 → 不重复 append 收尾
    corpus = tmp_path / "corpus_local"
    s = _cloud_sample("cs1", "云端问题一",
                      CLOUD_RESP)
    s["messages"] = [
        {"role": "user", "content": "云端问题一"},
        {"role": "assistant", "content": CLOUD_RESP}]
    _write_cloud(corpus, [s])
    out = tmp_path / "out"
    CB.build(corpus, out)
    rec = _all_recs(out)[0]
    roles = [mm["role"] for mm in rec["messages"]]
    assert roles == ["user", "assistant"]
    assert rec["messages"][-1]["content"] ==         CLOUD_RESP


def test_local_format_no_regression(tmp_path):
    # 本地采集样本（顶层 assistant 段）照旧：
    # 收尾内容取 assistant.content，thinking
    # 开关逻辑不变（R9 不动本地路径）
    corpus = tmp_path / "corpus_local"
    s = _sample("s1", "问题一")
    s["assistant"]["thinking"] = "思考过程"
    _write(corpus, [s])
    out = tmp_path / "out"
    CB.build(corpus, out)
    rec = _all_recs(out)[0]
    roles = [mm["role"] for mm in rec["messages"]]
    assert roles == ["user", "assistant"]
    assert rec["messages"][-1]["content"] ==         "这是足够长的回复内容用于通过短样本阈值"
    assert "思考过程" not in json.dumps(
        rec, ensure_ascii=False)
    out2 = tmp_path / "out2"
    CB.build(corpus, out2, include_thinking=True)
    rec2 = _all_recs(out2)[0]
    assert rec2["messages"][-1][
        "content"].startswith("思考过程\n\n")


def test_mixed_sources_by_source_counts(tmp_path):
    # 混合双源构建：by_source 按 id 形态计数
    # （整数 id=cloud / 其他=local）；云端
    # model 取顶层 model 字段入 by_model
    corpus = tmp_path / "corpus_local"
    _write(corpus, [
        _sample("s1", "本地问题一"),
        _sample("s2", "本地问题二")])
    _write_cloud(corpus, [
        _cloud_sample("cs1", "云端问题一",
                      CLOUD_RESP),
        _cloud_sample("cs2", "云端问题二",
                      CLOUD_RESP + "二"),
        _cloud_sample("cs3", "云端问题三",
                      CLOUD_RESP + "三")])
    out = tmp_path / "out"
    m = CB.build(corpus, out)
    assert m["input"] == 5
    assert m["by_source"]["local"]["count"] == 2
    assert m["by_source"]["cloud"]["count"] == 3
    assert m["by_source"]["local"]["tokens"] == 16
    assert m["by_source"]["cloud"]["tokens"] == 24
    assert m["by_model"]["GLM-5.3"]["count"] == 5
    assert m["train"] + m["val"] == 5


def test_assistant_chars_third_fallback(tmp_path):
    # 三级 fallback 末级：无 assistant 段、
    # 无顶层 response → 取 messages 内最后
    # 一条 role=assistant 的 content 长度；
    # 收尾时末条已是 assistant 且内容相同
    # → 不重复 append
    corpus = tmp_path / "corpus_local"
    s = _cloud_sample("cs1", "云端问题一", "")
    s["messages"].append({
        "role": "assistant",
        "content": "末条助理回复内容足够长可以通过阈值"})
    _write_cloud(corpus, [s])
    assert CB._assistant_chars(s) == len(
        "末条助理回复内容足够长可以通过阈值")
    out = tmp_path / "out"
    CB.build(corpus, out)
    rec = _all_recs(out)[0]
    roles = [mm["role"] for mm in rec["messages"]]
    assert roles == ["user", "assistant",
                     "tool", "assistant"]
    assert rec["messages"][-1]["content"] ==         "末条助理回复内容足够长可以通过阈值"
