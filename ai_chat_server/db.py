"""SQLite 数据库：初始化、建表、通用查询辅助。

使用标准库 sqlite3 + 原生 SQL，文件数据库，零外部依赖。
"""

import os
import sqlite3
import threading
from typing import Any, Sequence

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'user',
  is_active INTEGER NOT NULL DEFAULT 1,
  last_seen_at INTEGER,
  province TEXT DEFAULT '',
  city TEXT DEFAULT '',
  district TEXT DEFAULT '',
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS login_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  success INTEGER NOT NULL DEFAULT 1,
  ip TEXT,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS token_usage (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER,
  model_key TEXT NOT NULL,
  prompt_tokens INTEGER NOT NULL DEFAULT 0,
  completion_tokens INTEGER NOT NULL DEFAULT 0,
  total_tokens INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS models (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  model_key TEXT UNIQUE NOT NULL,
  name TEXT NOT NULL,
  provider TEXT NOT NULL DEFAULT 'openai',
  free INTEGER NOT NULL DEFAULT 0,
  vision INTEGER NOT NULL DEFAULT 0,
  enabled INTEGER NOT NULL DEFAULT 1,
  sort_order INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


def _connect() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        os.makedirs(os.path.dirname(settings.db_path) or ".", exist_ok=True)
        _conn = sqlite3.connect(settings.db_path, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
    return _conn


def _migrate() -> None:
    """轻量迁移：为旧库补加缺失列。"""
    conn = _connect()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(users)")}
    for ddl in (
        "province TEXT DEFAULT ''",
        "city TEXT DEFAULT ''",
        "district TEXT DEFAULT ''",
    ):
        col = ddl.split()[0]
        if col not in cols:
            conn.execute(f"ALTER TABLE users ADD COLUMN {ddl}")
    conn.commit()


def init_db() -> None:
    with _lock:
        conn = _connect()
        conn.executescript(SCHEMA)
        conn.commit()
        _migrate()


def fetch_all(sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
    with _lock:
        cur = _connect().execute(sql, params)
        return cur.fetchall()


def fetch_one(sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
    with _lock:
        cur = _connect().execute(sql, params)
        return cur.fetchone()


def execute(sql: str, params: Sequence[Any] = ()) -> int:
    """执行写入语句，返回 lastrowid。"""
    with _lock:
        conn = _connect()
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.lastrowid


def execute_many(sql: str, seq: Sequence[Sequence[Any]]) -> None:
    with _lock:
        conn = _connect()
        conn.executemany(sql, seq)
        conn.commit()


def now_ms() -> int:
    import time

    return int(time.time() * 1000)