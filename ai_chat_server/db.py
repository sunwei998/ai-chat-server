"""SQLite 数据库：初始化、建表、通用查询辅助。

使用标准库 sqlite3 + 原生 SQL，文件数据库，零外部依赖。
"""

import json
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
  age INTEGER,
  gender TEXT DEFAULT '',
  avatar TEXT DEFAULT '',
  username_changed_at INTEGER,
  username_change_count INTEGER NOT NULL DEFAULT 0,
  updated_at INTEGER,
  updated_by TEXT DEFAULT '',
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
  value TEXT NOT NULL,
  remark TEXT NOT NULL DEFAULT '',
  enabled INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS suggestions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title_zh TEXT NOT NULL,
  title_en TEXT NOT NULL,
  sort_order INTEGER NOT NULL DEFAULT 0,
  enabled INTEGER NOT NULL DEFAULT 1,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  model TEXT NOT NULL DEFAULT '',
  web_search INTEGER NOT NULL DEFAULT 0,
  pinned INTEGER NOT NULL DEFAULT 0,
  pinned_at INTEGER,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_sessions_user_updated ON sessions (user_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS messages (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  role TEXT NOT NULL,
  content TEXT NOT NULL DEFAULT '',
  images TEXT NOT NULL DEFAULT '[]',
  created_at INTEGER NOT NULL,
  FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_messages_session ON messages (session_id, created_at, id);
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
        _conn.execute("PRAGMA foreign_keys=ON")
    return _conn


def _migrate() -> None:
    """轻量迁移：为旧库补加缺失列。"""
    conn = _connect()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(users)")}
    for ddl in (
        "province TEXT DEFAULT ''",
        "city TEXT DEFAULT ''",
        "district TEXT DEFAULT ''",
        "age INTEGER",
        "birthday TEXT DEFAULT ''",
        "gender TEXT DEFAULT ''",
        "avatar TEXT DEFAULT ''",
        "username_changed_at INTEGER",
        "username_change_count INTEGER NOT NULL DEFAULT 0",
        "updated_at INTEGER",
        "updated_by TEXT DEFAULT ''",
    ):
        col = ddl.split()[0]
        if col not in cols:
            conn.execute(f"ALTER TABLE users ADD COLUMN {ddl}")
    settings_cols = {r[1] for r in conn.execute("PRAGMA table_info(settings)")}
    for ddl in ("remark TEXT NOT NULL DEFAULT ''", "enabled INTEGER NOT NULL DEFAULT 1"):
        col = ddl.split()[0]
        if col not in settings_cols:
            conn.execute(f"ALTER TABLE settings ADD COLUMN {ddl}")
    conn.commit()


def _seed_settings(conn: sqlite3.Connection) -> None:
    """对话分页、模型提供方数据字典等默认配置项：缺失时写入默认值。"""
    defaults = [
        ("chat_initial_page_size", "10", "会话首次打开时加载的最新消息条数"),
        ("chat_page_size", "10", "向上滚动时每次加载的更早消息条数"),
        (
            "model_providers",
            json.dumps(
                [
                    {"id": "openai", "name": "OpenAI"},
                    {"id": "anthropic", "name": "Anthropic Claude"},
                    {"id": "google", "name": "Google Gemini"},
                    {"id": "deepseek", "name": "DeepSeek"},
                    {"id": "qwen", "name": "通义千问 Qwen"},
                    {"id": "zhipu", "name": "智谱 GLM"},
                    {"id": "moonshot", "name": "Moonshot Kimi"},
                    {"id": "doubao", "name": "豆包 Doubao"},
                    {"id": "baidu", "name": "百度文心 ERNIE"},
                    {"id": "tencent", "name": "腾讯混元 Hunyuan"},
                    {"id": "minimax", "name": "MiniMax"},
                    {"id": "mistral", "name": "Mistral"},
                    {"id": "meta", "name": "Meta Llama"},
                    {"id": "groq", "name": "Groq"},
                    {"id": "openrouter", "name": "OpenRouter"},
                    {"id": "cohere", "name": "Cohere"},
                    {"id": "xai", "name": "xAI Grok"},
                    {"id": "stepfun", "name": "阶跃星辰 StepFun"},
                    {"id": "siliconflow", "name": "硅基流动 SiliconFlow"},
                    {"id": "ollama", "name": "Ollama"},
                ],
                ensure_ascii=False,
            ),
            "模型提供方数据字典（JSON 数组：[{\"id\",\"name\"}]）",
        ),
    ]
    for key, value, remark in defaults:
        conn.execute(
            "INSERT OR IGNORE INTO settings (key, value, remark, enabled) VALUES (?, ?, ?, 1)",
            (key, value, remark),
        )
    conn.commit()


def _seed_suggestions(conn: sqlite3.Connection) -> None:
    """热词表为空时写入默认推荐词（与前端 i18n 默认一致）。"""
    n = conn.execute("SELECT COUNT(*) AS n FROM suggestions").fetchone()["n"]
    if n:
        return
    defaults = [
        ("请解释一下量子计算", "Explain quantum computing"),
        ("如何学习编程？", "How do I learn programming?"),
        ("写一个Python函数", "Write a Python function"),
        ("讲一个有趣的笑话", "Tell me a funny joke"),
    ]
    conn.executemany(
        "INSERT INTO suggestions (title_zh, title_en, sort_order, enabled, created_at)"
        " VALUES (?, ?, ?, 1, ?)",
        [(zh, en, i + 1, now_ms()) for i, (zh, en) in enumerate(defaults)],
    )
    conn.commit()


def init_db() -> None:
    with _lock:
        conn = _connect()
        conn.executescript(SCHEMA)
        conn.commit()
        _migrate()
        _seed_suggestions(conn)
        _seed_settings(conn)


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


def transaction(queries: Sequence[tuple[str, Sequence[Any]]]) -> None:
    """多条写入在同一事务内提交；任一条失败则整体回滚。"""
    with _lock:
        conn = _connect()
        try:
            conn.execute("BEGIN")
            for sql, params in queries:
                conn.execute(sql, params)
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def now_ms() -> int:
    import time

    return int(time.time() * 1000)