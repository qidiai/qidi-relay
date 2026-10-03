# -*- coding: utf-8 -*-
"""Rewrite db.py with all audit fixes (C/G/B/A/E)."""
import atexit
import json
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime

log = logging.getLogger("qidi-relay")

try:
    from .settings import BASE_DIR
except ImportError:
    BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DB_PATH = os.path.join(str(BASE_DIR), "relay_knowledge.db")
BATCH_SIZE = 50
FLUSH_INTERVAL = 5.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    user_id TEXT DEFAULT '',
    api_key_label TEXT DEFAULT '',
    model_requested TEXT DEFAULT '',
    model_served TEXT DEFAULT '',
    status_code INTEGER DEFAULT 0,
    prompt_tokens INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    total_tokens INTEGER DEFAULT 0,
    duration_ms REAL DEFAULT 0,
    has_media INTEGER DEFAULT 0,
    is_stream INTEGER DEFAULT 0,
    effort TEXT DEFAULT '',
    error_detail TEXT DEFAULT '',
    reroute_note TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_req_ts ON requests(ts);
CREATE INDEX IF NOT EXISTS idx_req_model ON requests(model_requested);
CREATE INDEX IF NOT EXISTS idx_req_status ON requests(status_code);

CREATE TABLE IF NOT EXISTS errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    user_id TEXT DEFAULT '',
    model TEXT DEFAULT '',
    status_code INTEGER DEFAULT 0,
    error_type TEXT DEFAULT '',
    error_detail TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_err_ts ON errors(ts);
CREATE INDEX IF NOT EXISTS idx_err_model ON errors(model);
"""


class KnowledgeDB:
    """SQLite 知识库（0905 审计修正版）。

    修正项（对照审计 A-G）：
    A2/A3: flush 写库全程持 _write_lock；缓冲交换用原子引用赋值
    A4:   _get_conn 双检锁，并发首次调用不泄漏连接
    B2:   失败回灌缓冲 + sqlite timeout=5s（busy_timeout）防 database is locked 丢数据
    B3:   atexit.register(kdb.flush) 停机前刷尾
    C/G:  query() 用独立只读连接（mode=ro）+ fetchmany 限行，不持写锁 → 不阻塞请求
    """

    def __init__(self):
        self._conn = None
        self._write_lock = threading.Lock()
        self._lock = threading.Lock()
        self._buffer = []
        self._err_buffer = []
        self._init_db()

    def _get_conn(self):
        if self._conn is None:
            with self._lock:
                if self._conn is None:  # 双检锁（A4）
                    conn = sqlite3.connect(
                        DB_PATH, check_same_thread=False, timeout=5.0)
                    conn.execute("PRAGMA journal_mode=WAL")
                    conn.executescript(_SCHEMA)
                    conn.commit()
                    self._conn = conn
        return self._conn

    def _init_db(self):
        try:
            self._get_conn()
            log.info("[知识库] SQLite 已连接: %s", DB_PATH)
        except Exception as e:
            log.error("[知识库] SQLite 初始化失败: %s", e)

    def record_request(self, user_id: str = "", api_key_label: str = "",
                       model_requested: str = "", model_served: str = "",
                       status_code: int = 0, prompt_tokens: int = 0,
                       completion_tokens: int = 0, total_tokens: int = 0,
                       duration_ms: float = 0, has_media: bool = False,
                       is_stream: bool = False, effort: str = "",
                       error_detail: str = "", reroute_note: str = ""):
        """记录一次模型调用（非阻塞：入缓冲，后台批量落盘）。"""
        self._buffer.append({
            "user_id": user_id,
            "api_key_label": api_key_label,
            "model_requested": model_requested,
            "model_served": model_served,
            "status_code": status_code,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "duration_ms": round(duration_ms, 1),
            "has_media": 1 if has_media else 0,
            "is_stream": 1 if is_stream else 0,
            "effort": effort,
            "error_detail": error_detail[:500] if error_detail else "",
            "reroute_note": reroute_note[:200] if reroute_note else "",
        })
        if len(self._buffer) >= BATCH_SIZE:
            self.flush()

    def record_error(self, user_id: str, model: str, status_code: int,
                     error_type: str, error_detail: str = ""):
        """记录一次错误事件。"""
        self._err_buffer.append({
            "user_id": user_id,
            "model": model,
            "status_code": status_code,
            "error_type": error_type[:100] if error_type else "",
            "error_detail": error_detail[:500] if error_detail else "",
        })

    def flush(self):
        """将缓冲区数据批量写入 SQLite（写库全程持 _write_lock 防交错）。"""
        with self._write_lock:
            with self._lock:
                # 原子换引用（A3：不用 slice+clear，避免间隙丢条）
                reqs, self._buffer = self._buffer, []
                errs, self._err_buffer = self._err_buffer, []
            if not reqs and not errs:
                return
            try:
                conn = self._get_conn()
                with conn:
                    if reqs:
                        conn.executemany(
                            "INSERT INTO requests (user_id, api_key_label,"
                            " model_requested, model_served, status_code,"
                            " prompt_tokens, completion_tokens, total_tokens,"
                            " duration_ms, has_media, is_stream, effort,"
                            " error_detail, reroute_note)"
                            " VALUES (:user_id, :api_key_label,"
                            " :model_requested, :model_served, :status_code,"
                            " :prompt_tokens, :completion_tokens,"
                            " :total_tokens, :duration_ms, :has_media,"
                            " :is_stream, :effort, :error_detail, :reroute_note)",
                            reqs)
                    if errs:
                        conn.executemany(
                            "INSERT INTO errors (user_id, model, status_code,"
                            " error_type, error_detail)"
                            " VALUES (:user_id, :model, :status_code,"
                            " :error_type, :error_detail)",
                            errs)
            except Exception as e:
                # 失败回灌（B2）：数据回到缓冲区，下次 flush 重试
                log.error("[知识库] 批量写入失败（%d 条回灌待重试）: %s",
                          len(reqs) + len(errs), e)
                with self._lock:
                    self._buffer[:0] = reqs[:1000]  # 防止无限膨胀
                    self._err_buffer[:0] = errs[:1000]

    def query(self, sql: str, max_rows: int = 5000) -> list:
        """只读查询（C/G 修正）：独立只读连接 + fetchmany 限行，不持写锁。"""
        safe_path = DB_PATH.replace("\\", "/")
        conn = sqlite3.connect(
            f"file:{safe_path}?mode=ro", uri=True, timeout=5.0)
        try:
            cur = conn.execute(sql)
            rows = cur.fetchmany(max_rows + 1)
            truncated = len(rows) > max_rows
            rows = rows[:max_rows]
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in rows], truncated
        finally:
            conn.close()

    def stats_summary(self) -> dict:
        """知识库概要（面板展示用）。ts 是 localtime，筛选也用 localtime（修正时区）。"""
        conn = self._get_conn()
        out = {}
        try:
            out["total_requests"] = conn.execute(
                "SELECT COUNT(*) FROM requests").fetchone()[0]
            out["total_errors"] = conn.execute(
                "SELECT COUNT(*) FROM errors").fetchone()[0]
            out["models_used"] = conn.execute(
                "SELECT COUNT(DISTINCT model_requested) FROM requests").fetchone()[0]
            out["db_size_kb"] = round(os.path.getsize(DB_PATH) / 1024)
            # 修正时区（C6）：用 localtime 而非 UTC
            out["model_usage_24h"] = conn.execute(
                "SELECT model_requested, COUNT(*) as cnt,"
                " SUM(total_tokens) as tokens FROM requests"
                " WHERE ts >= datetime('now', 'localtime', '-1 day')"
                " GROUP BY model_requested ORDER BY cnt DESC LIMIT 10"
            ).fetchall()
        except Exception as e:
            out["error"] = str(e)
        return out

    def export_csv(self, table: str = "requests", limit: int = 10000) -> str:
        """导出为 CSV 格式字符串。"""
        import csv
        import io
        safe_table = table if table in ("requests", "errors") else "requests"
        limit = max(1, min(int(limit), 50000))  # D 修正：clamp limit
        conn = self._get_conn()
        cur = conn.execute(
            f"SELECT * FROM {safe_table} ORDER BY id DESC LIMIT ?", (limit,))
        cols = [d[0] for d in cur.description]
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(cols)
        for row in cur.fetchall():
            writer.writerow(row)
        return buf.getvalue()


# 模块级单例 + atexit 停机刷尾（B3）
kdb = KnowledgeDB()
atexit.register(kdb.flush)
