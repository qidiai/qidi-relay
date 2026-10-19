# -*- coding: utf-8 -*-
"""语料导出/确认端点（GET /api/training/export、GET /api/training/ack）
回归测试。

拉取端是台式机 corpus_pull.py：带 Authorization: Bearer <pull_key>
GET {pull_url}/training/export?since_id=N&limit=5000，期望
{"lines": ["<json行>", ...], "max_id": N}，每行 JSON 必含整数 id
（增量与 ack 全靠它）；本地落盘成功后再 GET /training/ack?max_id=N，
**只有 ack 200 才推进本地水位**（所以 ack 必须先落库再返 200）。
本文件覆盖：

1. 正常导出 + since_id 增量（两页不重不漏、max_id 正确）；
2. 鉴权：缺 Bearer / 错 key / 裸 key（无 Bearer 前缀）→ 401；
3. corpus.enabled=false → 503 + 明确 error；
4. corpus.pull_key 缺失 / 为空 → 503；
5. limit 钳制：99999 → 5000、0 / 负数 → 1、缺省 5000；
6. 空语料 → {"lines": [], "max_id": 0}（200）；
7. 语料库损坏 → 503（不 500 裸崩）；
8. 原生语料表形态（corpus 表带 line 列）：line 内容原样、id 取库行 id；
9. 尾部半行不入索引（worker 逐行 fsync，写一半不能被导出）；
10. 非整数 since_id → 400；
11. corpus_YYYYMM.jsonl（拉取器落地的远端副本）不导出（防自拉自环）；
12. ack 鉴权/未启用/缺 key（与 export 同款）；
13. ack 参数校验：缺 max_id / 非整数 / 负数 / 超可导出上限 → 400；
14. ack 水位语义（index 形态：推进 exported_upto，不删索引行）；
15. ack 删除语义（table 形态：DELETE id <= max_id，重复 ack 幂等）；
16. export→ack→export 全链不重复（拉取器两轮模拟，核心回归）。

零真实网络、零仓库数据文件：settings.BASE_DIR / CORPUS_DIR 注入
tmp_path（方案 §7.7 路径常量纪律），config 用 monkeypatch 替换
corpus.load_config。
"""

import json
import pathlib
import sqlite3
import sys

import pytest
from flask import Flask

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import proxy_core.corpus as CO  # noqa: E402
import proxy_core.settings as S  # noqa: E402
from routes.admin import bp as admin_bp  # noqa: E402

_app = Flask("training-export-test")
_app.register_blueprint(admin_bp)
client = _app.test_client()

URL = "/api/training/export"
ACK = "/api/training/ack"
PULL_KEY = "pull-" + "k" * 40


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """路径/配置注入：语料库与语料目录全部落在 tmp_path。"""
    corpus_dir = tmp_path / "corpus_local"
    corpus_dir.mkdir()
    cfg = {"corpus": {"enabled": True, "pull_key": PULL_KEY}}
    monkeypatch.setattr(S, "BASE_DIR", tmp_path)
    monkeypatch.setattr(S, "CORPUS_DIR", corpus_dir)
    monkeypatch.setattr(CO, "CORPUS_DIR", corpus_dir)
    monkeypatch.setattr(CO, "load_config", lambda: cfg)
    return {"base": tmp_path, "dir": corpus_dir, "cfg": cfg}


def _auth(key=PULL_KEY):
    return {"Authorization": "Bearer " + key}


def _sample(i):
    return {
        "id": "local-20261020-%06d" % i,
        "messages": [{"role": "user", "content": "m%d" % i}],
        "model": "GLM-5.3",
    }


def _write_jsonl(path, objs):
    path.write_text(
        "".join(json.dumps(o, ensure_ascii=False) + "\n" for o in objs),
        encoding="utf-8")


# 富列老表（云端旧构建 relay_corpus.db 的真实 schema）
_RICH_TABLE_DDL = (
    "CREATE TABLE corpus ("
    " id INTEGER PRIMARY KEY AUTOINCREMENT,"
    " ts TEXT, user_id TEXT, api_key_label TEXT,"
    " model TEXT, model_served TEXT, is_stream INTEGER,"
    " prompt_tokens INTEGER, completion_tokens INTEGER,"
    " body_chars INTEGER, sha256 TEXT, messages_json TEXT,"
    " response_text TEXT, tool_calls_json TEXT)")

_RICH_INSERT = (
    "INSERT INTO corpus (ts, user_id, api_key_label, model, model_served,"
    " is_stream, prompt_tokens, completion_tokens, body_chars, sha256,"
    " messages_json, response_text, tool_calls_json)"
    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)")

_RICH_COLS = ("ts", "user_id", "api_key_label", "model", "model_served",
              "is_stream", "prompt_tokens", "completion_tokens",
              "body_chars", "sha256", "messages_json", "response_text",
              "tool_calls_json")


def _make_rich_db(path, rows):
    conn = sqlite3.connect(path)
    with conn:
        conn.execute(_RICH_TABLE_DDL)
        conn.executemany(_RICH_INSERT, rows)
    conn.close()


def _rich_row(i, **over):
    row = {
        "ts": "2026-10-14 20:31:00",
        "user_id": "u%d" % i,
        "api_key_label": "key-%d" % i,
        "model": "GLM-5.3",
        "model_served": "codely-core",
        "is_stream": 1,
        "prompt_tokens": 100 * i,
        "completion_tokens": 10 * i,
        "body_chars": 500,
        "sha256": "h%d" % i,
        "messages_json": json.dumps(
            [{"role": "user", "content": "r%d" % i}]),
        "response_text": "resp%d" % i,
        "tool_calls_json": json.dumps([{"id": "t%d" % i}]),
    }
    row.update(over)
    return tuple(row[k] for k in _RICH_COLS)


# ─── 1. 正常导出 + 增量 ─────────────────────────────

def test_export_basic_and_incremental(env):
    _write_jsonl(env["dir"] / "local_202609.jsonl",
                 [{"id": "local-20260930-000001",
                   "messages": [{"role": "user", "content": "a"}]},
                  {"id": "local-20260930-000002",
                   "messages": [{"role": "user", "content": "b"}]}])
    _write_jsonl(env["dir"] / "local_202610.jsonl",
                 [{"id": "local-20261001-000001",
                   "messages": [{"role": "user", "content": "c"}]}])

    r = client.get(URL, query_string={"since_id": 0, "limit": 2},
                   headers=_auth())
    assert r.status_code == 200
    d = r.get_json()
    assert len(d["lines"]) == 2
    assert d["max_id"] == 2
    objs = [json.loads(x) for x in d["lines"]]
    # 导出的 id 一律是整数（拉取器 int() 解析增量）
    assert [o["id"] for o in objs] == [1, 2]
    assert objs[0]["messages"][0]["content"] == "a"
    assert objs[1]["messages"][0]["content"] == "b"

    # 第二页：只回更大的 id（since_id 增量生效）
    d2 = client.get(URL, query_string={"since_id": d["max_id"]},
                    headers=_auth()).get_json()
    assert [json.loads(x)["id"] for x in d2["lines"]] == [3]
    assert d2["max_id"] == 3

    # 追平后无新数据
    d3 = client.get(URL, query_string={"since_id": 3},
                    headers=_auth()).get_json()
    assert d3 == {"lines": [], "max_id": 0}


# ─── 2. 鉴权 ────────────────────────────────────────

def test_export_auth(env):
    _write_jsonl(env["dir"] / "local_202610.jsonl", [_sample(1)])
    assert client.get(URL).status_code == 401
    assert client.get(
        URL, headers={"Authorization": "Bearer wrong-key"}).status_code == 401
    # 裸 key（无 Bearer 前缀）不算合法凭据
    assert client.get(
        URL, headers={"Authorization": PULL_KEY}).status_code == 401
    assert client.get(URL, headers=_auth()).status_code == 200


# ─── 3/4. 未启用 / 缺 pull_key → 503 ────────────────

def test_export_disabled_503(env):
    env["cfg"]["corpus"] = {"enabled": False, "pull_key": PULL_KEY}
    r = client.get(URL, headers=_auth())
    assert r.status_code == 503
    assert "error" in r.get_json()


def test_export_missing_pull_key_503(env):
    env["cfg"]["corpus"] = {"enabled": True}
    r = client.get(URL, headers=_auth())
    assert r.status_code == 503
    assert "error" in r.get_json()

    env["cfg"]["corpus"] = {"enabled": True, "pull_key": ""}
    assert client.get(URL, headers=_auth()).status_code == 503

    # corpus 块整体缺失（缺省关闭）
    env["cfg"].pop("corpus")
    assert client.get(URL, headers=_auth()).status_code == 503


# ─── 5. limit 钳制 ─────────────────────────────────

def test_export_limit_clamped(env):
    _write_jsonl(env["dir"] / "local_202610.jsonl",
                 [_sample(i) for i in range(1, 5002)])

    d = client.get(URL, query_string={"limit": 99999},
                   headers=_auth()).get_json()
    assert len(d["lines"]) == 5000          # 钳到上限 5000
    assert d["max_id"] == 5000

    d2 = client.get(URL, query_string={"since_id": 5000, "limit": 99999},
                    headers=_auth()).get_json()
    assert [json.loads(x)["id"] for x in d2["lines"]] == [5001]

    # limit=0 / 负数 → 钳到 1 行
    assert len(client.get(URL, query_string={"limit": 0},
                          headers=_auth()).get_json()["lines"]) == 1
    assert len(client.get(URL, query_string={"limit": -5},
                          headers=_auth()).get_json()["lines"]) == 1

    # 缺省 limit = 5000
    assert len(client.get(URL, headers=_auth()).get_json()["lines"]) == 5000


# ─── 6. 空语料 ─────────────────────────────────────

def test_export_empty_corpus(env):
    r = client.get(URL, headers=_auth())
    assert r.status_code == 200
    assert r.get_json() == {"lines": [], "max_id": 0}

    # 空文件同样算空库（不报错）
    (env["dir"] / "local_202610.jsonl").write_text("", encoding="utf-8")
    assert client.get(URL, headers=_auth()).get_json() == {
        "lines": [], "max_id": 0}


# ─── 7. 语料库损坏 → 503 ────────────────────────────

def test_export_corrupt_db_503(env):
    (env["base"] / "relay_corpus.db").write_bytes(
        b"\x00not-a-sqlite-database" * 64)
    r = client.get(URL, headers=_auth())
    assert r.status_code == 503
    assert "error" in r.get_json()
    # ack 同一条错误路径（不 500 裸崩）
    ra = client.get(ACK, query_string={"max_id": 0}, headers=_auth())
    assert ra.status_code == 503
    assert "error" in ra.get_json()


# ─── 8. 原生语料表形态（corpus 表带 line 列）────────

def test_export_native_corpus_table(env):
    db = env["base"] / "relay_corpus.db"
    conn = sqlite3.connect(db)
    with conn:
        conn.execute("CREATE TABLE corpus (id INTEGER PRIMARY KEY"
                     " AUTOINCREMENT, line TEXT NOT NULL,"
                     " created_at TEXT)")
        conn.execute("INSERT INTO corpus (line) VALUES (?)", (json.dumps(
            {"id": 77,
             "messages": [{"role": "user", "content": "x"}]}),))
        conn.execute("INSERT INTO corpus (line) VALUES (?)",
                     (json.dumps({"messages": []}),))
    conn.close()

    d = client.get(URL, headers=_auth()).get_json()
    assert d["max_id"] == 2
    objs = [json.loads(x) for x in d["lines"]]
    # id 统一取库行 id（行内旧 id 被覆盖，保证增量口径一致）
    assert [o["id"] for o in objs] == [1, 2]
    assert objs[0]["messages"][0]["content"] == "x"   # 其余内容原样
    assert objs[1] == {"id": 2, "messages": []}

    # 原生表形态下不索引 JSONL 目录（互不串台）
    _write_jsonl(env["dir"] / "local_202610.jsonl", [_sample(1)])
    assert client.get(URL, query_string={"since_id": 2},
                      headers=_auth()).get_json() == {"lines": [],
                                                      "max_id": 0}


# ─── 9. 尾部半行不导出 ──────────────────────────────

def test_export_skips_partial_trailing_line(env):
    path = env["dir"] / "local_202610.jsonl"
    good = json.dumps(_sample(1), ensure_ascii=False)
    full2 = json.dumps(_sample(2), ensure_ascii=False)
    # worker 逐行 fsync：可能读到只写了一半的最后一行
    path.write_text(good + "\n" + full2[:20], encoding="utf-8")

    d = client.get(URL, headers=_auth()).get_json()
    assert len(d["lines"]) == 1
    assert d["max_id"] == 1

    # 写完整后下轮补收（半行不丢）
    path.write_text(good + "\n" + full2 + "\n", encoding="utf-8")
    d2 = client.get(URL, query_string={"since_id": d["max_id"]},
                    headers=_auth()).get_json()
    assert [json.loads(x)["id"] for x in d2["lines"]] == [2]
    assert d2["max_id"] == 2


# ─── 10. 非法 since_id / limit ─────────────────────

def test_export_invalid_since_id_400(env):
    r = client.get(URL, query_string={"since_id": "abc"}, headers=_auth())
    assert r.status_code == 400
    assert "error" in r.get_json()

    r2 = client.get(URL, query_string={"limit": "abc"}, headers=_auth())
    assert r2.status_code == 400
    assert "error" in r2.get_json()


def test_export_non_ascii_pull_key(env):
    """非 ASCII key 走字节比对（compare_digest 的 str 分支只支持
    ASCII，直接用 str 会 TypeError → 500）。"""
    key = "pull-k\u00eby-123"
    env["cfg"]["corpus"]["pull_key"] = key
    _write_jsonl(env["dir"] / "local_202610.jsonl", [_sample(1)])
    assert client.get(URL, headers=_auth()).status_code == 401
    r = client.get(URL, headers={"Authorization": "Bearer " + key})
    assert r.status_code == 200
    assert len(r.get_json()["lines"]) == 1


# ─── 11. 远端副本文件不导出（防自拉自环）───────────

def test_export_ignores_pulled_corpus_files(env):
    _write_jsonl(env["dir"] / "corpus_202610.jsonl", [_sample(1)])
    _write_jsonl(env["dir"] / "local_202610.jsonl", [_sample(2)])
    d = client.get(URL, headers=_auth()).get_json()
    assert len(d["lines"]) == 1
    assert json.loads(d["lines"][0])["messages"][0]["content"] == "m2"


# ─── 12. ack 鉴权 / 未启用 / 缺 key（与 export 同款）────

def test_ack_auth(env):
    _write_jsonl(env["dir"] / "local_202610.jsonl", [_sample(1)])
    assert client.get(ACK, query_string={"max_id": 1}).status_code == 401
    assert client.get(
        ACK, query_string={"max_id": 1},
        headers={"Authorization": "Bearer wrong"}).status_code == 401
    # 裸 key（无 Bearer 前缀）不算合法凭据
    assert client.get(
        ACK, query_string={"max_id": 1},
        headers={"Authorization": PULL_KEY}).status_code == 401
    r = client.get(ACK, query_string={"max_id": 1}, headers=_auth())
    assert r.status_code == 200
    assert r.get_json() == {"ok": True, "acked_upto": 1, "deleted": 0}


def test_ack_disabled_or_missing_key_503(env):
    env["cfg"]["corpus"] = {"enabled": False, "pull_key": PULL_KEY}
    assert client.get(ACK, query_string={"max_id": 0},
                      headers=_auth()).status_code == 503
    env["cfg"]["corpus"] = {"enabled": True}
    assert client.get(ACK, query_string={"max_id": 0},
                      headers=_auth()).status_code == 503
    env["cfg"]["corpus"] = {"enabled": True, "pull_key": ""}
    assert client.get(ACK, query_string={"max_id": 0},
                      headers=_auth()).status_code == 503
    env["cfg"].pop("corpus")
    assert client.get(ACK, query_string={"max_id": 0},
                      headers=_auth()).status_code == 503


# ─── 13. ack 参数校验 ───────────────────────────────

def test_ack_invalid_max_id_400(env):
    _write_jsonl(env["dir"] / "local_202610.jsonl", [_sample(1)])
    # 缺失
    assert client.get(ACK, headers=_auth()).status_code == 400
    # 非整数 / 负数
    assert client.get(ACK, query_string={"max_id": "abc"},
                      headers=_auth()).status_code == 400
    assert client.get(ACK, query_string={"max_id": -1},
                      headers=_auth()).status_code == 400


def test_ack_rejects_above_exportable(env):
    # 空库：max_id=0 幂等 200；max_id=1（从未导出过）→ 400
    r0 = client.get(ACK, query_string={"max_id": 0}, headers=_auth())
    assert r0.status_code == 200
    assert r0.get_json() == {"ok": True, "acked_upto": 0, "deleted": 0}
    assert client.get(ACK, query_string={"max_id": 1},
                      headers=_auth()).status_code == 400

    # 有数据但 ack 越过当前最大 id → 400，且水位不动（数据仍可导出）
    _write_jsonl(env["dir"] / "local_202610.jsonl",
                 [_sample(1), _sample(2)])
    r = client.get(ACK, query_string={"max_id": 5}, headers=_auth())
    assert r.status_code == 400
    assert r.get_json()["exportable_max"] == 2
    d = client.get(URL, headers=_auth()).get_json()
    assert [json.loads(x)["id"] for x in d["lines"]] == [1, 2]
    # 上限内正常 ack
    assert client.get(ACK, query_string={"max_id": 2},
                      headers=_auth()).status_code == 200


# ─── 14. index 形态：水位推进、不删索引行 ───────────

def test_ack_watermark_index_mode(env):
    _write_jsonl(env["dir"] / "local_202610.jsonl",
                 [_sample(i) for i in (1, 2, 3)])
    d = client.get(URL, headers=_auth()).get_json()
    assert [json.loads(x)["id"] for x in d["lines"]] == [1, 2, 3]

    r = client.get(ACK, query_string={"max_id": 2}, headers=_auth())
    assert r.status_code == 200
    assert r.get_json() == {"ok": True, "acked_upto": 2, "deleted": 0}

    # 水位过滤：即使 since_id=0 也只回未 ack 的 id 3（防重第二层）
    d2 = client.get(URL, headers=_auth()).get_json()
    assert [json.loads(x)["id"] for x in d2["lines"]] == [3]
    assert d2["max_id"] == 3
    assert client.get(URL, query_string={"since_id": 3},
                      headers=_auth()).get_json() == {"lines": [],
                                                      "max_id": 0}

    # 重复 ack 同一水位幂等
    r2 = client.get(ACK, query_string={"max_id": 2}, headers=_auth())
    assert r2.status_code == 200
    assert r2.get_json() == {"ok": True, "acked_upto": 2, "deleted": 0}

    # 追平后 ack 3 → 导出空
    assert client.get(ACK, query_string={"max_id": 3},
                      headers=_auth()).get_json() == {"ok": True,
                                                      "acked_upto": 3,
                                                      "deleted": 0}
    assert client.get(URL, headers=_auth()).get_json() == {"lines": [],
                                                          "max_id": 0}


# ─── 15. table 形态：ack 删行 ───────────────────────

def test_ack_table_mode_deletes_rows(env):
    db = env["base"] / "relay_corpus.db"
    conn = sqlite3.connect(db)
    with conn:
        conn.execute("CREATE TABLE corpus (id INTEGER PRIMARY KEY"
                     " AUTOINCREMENT, line TEXT NOT NULL,"
                     " created_at TEXT)")
        for i in (1, 2, 3):
            conn.execute("INSERT INTO corpus (line) VALUES (?)", (json.dumps(
                {"messages": [{"role": "user", "content": "t%d" % i}]}),))
    conn.close()

    d = client.get(URL, headers=_auth()).get_json()
    assert [json.loads(x)["id"] for x in d["lines"]] == [1, 2, 3]

    r = client.get(ACK, query_string={"max_id": 2}, headers=_auth())
    assert r.status_code == 200
    assert r.get_json() == {"ok": True, "acked_upto": 2, "deleted": 2}

    # 删掉的行不再导出
    d2 = client.get(URL, headers=_auth()).get_json()
    assert [json.loads(x)["id"] for x in d2["lines"]] == [3]
    assert d2["max_id"] == 3

    # 重复 ack 幂等（删 0 行，水位兜住仍 200）
    r2 = client.get(ACK, query_string={"max_id": 2}, headers=_auth())
    assert r2.status_code == 200
    assert r2.get_json() == {"ok": True, "acked_upto": 2, "deleted": 0}

    # 超过可导出上限 → 400
    assert client.get(ACK, query_string={"max_id": 9},
                      headers=_auth()).status_code == 400


# ─── 16. export→ack→export 全链不重复（核心回归）────

def test_export_ack_export_no_duplicates(env):
    path = env["dir"] / "local_202610.jsonl"
    _write_jsonl(path, [_sample(1), _sample(2)])

    # 第一轮：拉取 → ack（拉取器把 got_max 写进本地 state）
    d1 = client.get(URL, query_string={"since_id": 0, "limit": 5000},
                    headers=_auth()).get_json()
    ids1 = [json.loads(x)["id"] for x in d1["lines"]]
    assert ids1 == [1, 2]
    assert client.get(ACK, query_string={"max_id": ids1[-1]},
                      headers=_auth()).status_code == 200

    # 第二轮：新增两行，按本地水位 since_id=2 续拉
    _write_jsonl(path, [_sample(1), _sample(2), _sample(3), _sample(4)])
    d2 = client.get(URL, query_string={"since_id": 2, "limit": 5000},
                    headers=_auth()).get_json()
    ids2 = [json.loads(x)["id"] for x in d2["lines"]]
    assert ids2 == [3, 4]
    assert client.get(ACK, query_string={"max_id": ids2[-1]},
                      headers=_auth()).status_code == 200

    # 两轮 id 无交集、拼起来正好是全集
    assert set(ids1).isdisjoint(ids2)
    assert ids1 + ids2 == [1, 2, 3, 4]

    # 防御：客户端误以 since_id=0 重跑，水位过滤仍不重复下发
    assert client.get(URL, query_string={"since_id": 0, "limit": 5000},
                      headers=_auth()).get_json() == {"lines": [],
                                                      "max_id": 0}


# ─── 17. table_rich 形态（云端旧构建富列老表）────────

def test_export_table_rich_mode(env):
    _make_rich_db(env["base"] / "relay_corpus.db", [
        _rich_row(1),
        # 第二行：messages_json 故意坏 JSON、tool_calls 非 JSON、
        # response_text NULL → 验证"解析失败原样 / NULL 给默认"
        _rich_row(2, messages_json='{"broken": ', response_text=None,
                  tool_calls_json="not-json"),
    ])

    d = client.get(URL, headers=_auth()).get_json()
    assert d["max_id"] == 2
    objs = [json.loads(x) for x in d["lines"]]
    assert [o["id"] for o in objs] == [1, 2]

    first = objs[0]
    assert first["messages"] == [{"role": "user", "content": "r1"}]
    assert first["tool_calls"] == [{"id": "t1"}]
    assert first["response"] == "resp1"
    assert first["ts"] == "2026-10-14 20:31:00"
    assert first["model"] == "GLM-5.3"
    assert first["model_served"] == "codely-core"
    assert first["is_stream"] is True
    assert first["prompt_tokens"] == 100
    assert first["completion_tokens"] == 10
    assert first["user_id"] == "u1"
    assert first["api_key_label"] == "key-1"
    assert first["sha256"] == "h1"

    second = objs[1]
    assert second["messages"] == '{"broken": '   # 坏 JSON 原样降级
    assert second["tool_calls"] == "not-json"
    assert second["response"] is None            # NULL 保持 null


def test_export_table_rich_incremental(env):
    _make_rich_db(env["base"] / "relay_corpus.db",
                  [_rich_row(i) for i in (1, 2, 3)])

    d1 = client.get(URL, query_string={"limit": 2},
                    headers=_auth()).get_json()
    assert [json.loads(x)["id"] for x in d1["lines"]] == [1, 2]
    assert d1["max_id"] == 2

    # 第二页只回更大 id（富列形态同样满足增量契约）
    d2 = client.get(URL, query_string={"since_id": d1["max_id"]},
                    headers=_auth()).get_json()
    assert [json.loads(x)["id"] for x in d2["lines"]] == [3]
    assert d2["max_id"] == 3

    assert client.get(URL, query_string={"since_id": 3},
                      headers=_auth()).get_json() == {"lines": [],
                                                      "max_id": 0}


def test_ack_table_rich_mode(env):
    _make_rich_db(env["base"] / "relay_corpus.db",
                  [_rich_row(i) for i in (1, 2, 3)])
    d = client.get(URL, headers=_auth()).get_json()
    assert [json.loads(x)["id"] for x in d["lines"]] == [1, 2, 3]

    # ack 删远端 + 真实删除计数
    r = client.get(ACK, query_string={"max_id": 2}, headers=_auth())
    assert r.status_code == 200
    assert r.get_json() == {"ok": True, "acked_upto": 2, "deleted": 2}
    d2 = client.get(URL, headers=_auth()).get_json()
    assert [json.loads(x)["id"] for x in d2["lines"]] == [3]

    # 重复 ack 幂等（删 0 行仍 200，水位兜住）
    r2 = client.get(ACK, query_string={"max_id": 2}, headers=_auth())
    assert r2.status_code == 200
    assert r2.get_json() == {"ok": True, "acked_upto": 2, "deleted": 0}

    # 超过可导出上限 → 400
    assert client.get(ACK, query_string={"max_id": 9},
                      headers=_auth()).status_code == 400

    # 追平：删最后一行
    r3 = client.get(ACK, query_string={"max_id": 3}, headers=_auth())
    assert r3.status_code == 200
    assert r3.get_json() == {"ok": True, "acked_upto": 3, "deleted": 1}
    assert client.get(URL, headers=_auth()).get_json() == {"lines": [],
                                                          "max_id": 0}


# ─── 18. 形态优先级 / 索引库独立文件 ────────────────

def test_mode_priority_rich_over_index(env):
    """同库同时能走 index（有 JSONL 语料）与 table_rich（富列表）
    → 必须走 table_rich，且不创建独立索引库。"""
    _write_jsonl(env["dir"] / "local_202610.jsonl",
                 [_sample(1), _sample(2)])
    _make_rich_db(env["base"] / "relay_corpus.db", [
        _rich_row(7, messages_json=json.dumps(
            [{"role": "user", "content": "rich"}]))])

    d = client.get(URL, headers=_auth()).get_json()
    # 只回富列表那 1 行（id 取库行 id=1），JSONL 的两行不参与
    assert [json.loads(x)["id"] for x in d["lines"]] == [1]
    assert json.loads(d["lines"][0])["messages"][0]["content"] == "rich"
    assert not (env["base"] / "relay_corpus_index.db").exists()


def test_index_db_separate_file(env):
    """index 形态索引落 relay_corpus_index.db，不碰 relay_corpus.db。"""
    _write_jsonl(env["dir"] / "local_202610.jsonl", [_sample(1)])

    d = client.get(URL, headers=_auth()).get_json()
    assert [json.loads(x)["id"] for x in d["lines"]] == [1]
    assert (env["base"] / "relay_corpus_index.db").exists()
    assert not (env["base"] / "relay_corpus.db").exists()

    # 再请求一次仍稳定（索引复用独立库）
    client.get(URL, headers=_auth())
    assert not (env["base"] / "relay_corpus.db").exists()
    assert (env["base"] / "relay_corpus_index.db").exists()
