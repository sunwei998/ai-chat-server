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
  supports_search INTEGER NOT NULL DEFAULT 1,
  enabled INTEGER NOT NULL DEFAULT 1,
  sort_order INTEGER NOT NULL DEFAULT 0,
  is_default INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  remark TEXT NOT NULL DEFAULT '',
  enabled INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS dim_tables (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  code TEXT UNIQUE NOT NULL,
  name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  sort_order INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL,
  updated_at INTEGER,
  updated_by TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS dim_values (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  table_id INTEGER NOT NULL,
  code TEXT NOT NULL,
  name TEXT NOT NULL,
  sort_order INTEGER NOT NULL DEFAULT 0,
  enabled INTEGER NOT NULL DEFAULT 1,
  remark TEXT NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL,
  updated_at INTEGER,
  UNIQUE (table_id, code),
  FOREIGN KEY (table_id) REFERENCES dim_tables(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_dim_values_table ON dim_values (table_id);

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
  citations TEXT NOT NULL DEFAULT '[]',
  created_at INTEGER NOT NULL,
  FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_messages_session ON messages (session_id, created_at, id);
CREATE INDEX IF NOT EXISTS idx_messages_role_created ON messages (role, created_at);

CREATE TABLE IF NOT EXISTS transfer_records (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  type TEXT NOT NULL DEFAULT 'import',
  user_id INTEGER,
  username TEXT NOT NULL DEFAULT '',
  filename TEXT NOT NULL,
  file_size INTEGER NOT NULL DEFAULT 0,
  mime_type TEXT NOT NULL DEFAULT '',
  file_path TEXT NOT NULL DEFAULT '',
  remark TEXT NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_transfer_type_created ON transfer_records (type, created_at DESC);
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
    msg_cols = {r[1] for r in conn.execute("PRAGMA table_info(messages)")}
    if "citations" not in msg_cols:
        conn.execute("ALTER TABLE messages ADD COLUMN citations TEXT NOT NULL DEFAULT '[]'")
    model_cols = {r[1] for r in conn.execute("PRAGMA table_info(models)")}
    if "supports_search" not in model_cols:
        conn.execute("ALTER TABLE models ADD COLUMN supports_search INTEGER NOT NULL DEFAULT 1")
    if "is_default" not in model_cols:
        conn.execute("ALTER TABLE models ADD COLUMN is_default INTEGER NOT NULL DEFAULT 0")
    conn.commit()


# 默认模型供应商（同时作为维表 model_provider 的 seed 与前端极端降级）
DEFAULT_MODEL_PROVIDERS = [
    ("openai", "OpenAI"),
    ("anthropic", "Anthropic Claude"),
    ("google", "Google Gemini"),
    ("deepseek", "DeepSeek"),
    ("qwen", "通义千问 Qwen"),
    ("zhipu", "智谱 GLM"),
    ("moonshot", "Moonshot Kimi"),
    ("doubao", "豆包 Doubao"),
    ("baidu", "百度文心 ERNIE"),
    ("tencent", "腾讯混元 Hunyuan"),
    ("minimax", "MiniMax"),
    ("mistral", "Mistral"),
    ("meta", "Meta Llama"),
    ("groq", "Groq"),
    ("openrouter", "OpenRouter"),
    ("cohere", "Cohere"),
    ("xai", "xAI Grok"),
    ("stepfun", "阶跃星辰 StepFun"),
    ("siliconflow", "硅基流动 SiliconFlow"),
    ("ollama", "Ollama"),
]


def _seed_dim_tables(conn: sqlite3.Connection) -> None:
    """维表默认数据：model_provider（模型供应商）。已存在则跳过。"""
    row = conn.execute("SELECT id FROM dim_tables WHERE code = 'model_provider'").fetchone()
    if row:
        return
    ts = now_ms()
    cur = conn.execute(
        "INSERT INTO dim_tables (code, name, description, sort_order, created_at, updated_at, updated_by) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("model_provider", "模型供应商", "模型 provider 维表，供模型管理下拉与导入模板复用", 0, ts, ts, "system"),
    )
    table_id = cur.lastrowid
    for idx, (code, name) in enumerate(DEFAULT_MODEL_PROVIDERS):
        conn.execute(
            "INSERT OR IGNORE INTO dim_values (table_id, code, name, sort_order, enabled, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 1, ?, ?)",
            (table_id, code, name, idx, ts, ts),
        )
    conn.commit()


def _migrate_model_providers(conn: sqlite3.Connection) -> None:
    """旧库兼容：把 settings.model_providers（JSON 数组）一次性迁为 model_provider 维表。"""
    existing = conn.execute("SELECT id FROM dim_tables WHERE code = 'model_provider'").fetchone()
    if existing:
        return
    row = conn.execute("SELECT value FROM settings WHERE key = 'model_providers'").fetchone()
    if not row or not row["value"]:
        return
    try:
        parsed = json.loads(row["value"])
    except (json.JSONDecodeError, TypeError):
        return
    if not isinstance(parsed, list):
        return
    ts = now_ms()
    cur = conn.execute(
        "INSERT INTO dim_tables (code, name, description, sort_order, created_at, updated_at, updated_by) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("model_provider", "模型供应商", "由 settings.model_providers 迁移而来", 0, ts, ts, "system"),
    )
    table_id = cur.lastrowid
    for idx, item in enumerate(parsed):
        if not isinstance(item, dict) or not item.get("id"):
            continue
        conn.execute(
            "INSERT OR IGNORE INTO dim_values (table_id, code, name, sort_order, enabled, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 1, ?, ?)",
            (table_id, str(item["id"]), str(item.get("name", item["id"])), idx, ts, ts),
        )
    conn.commit()
    # 迁移完成，删除旧 settings 行，避免双源
    conn.execute("DELETE FROM settings WHERE key = 'model_providers'")


def _seed_settings(conn: sqlite3.Connection) -> None:
    """对话分页、模型提供方数据字典等默认配置项：缺失时写入默认值。"""
    defaults = [
        ("chat_initial_page_size", "10", "会话首次打开时加载的最新消息条数"),
        ("chat_page_size", "10", "向上滚动时每次加载的更早消息条数"),
        # 联网搜索默认配置（缺失时写入，使 init 即生效、后台可读可改）
        (
            "websearch_providers",
            json.dumps(
                [
                    {"id": "baidu", "label": "百度 (中文最佳)", "enabled": True},
                    {"id": "searxng", "label": "SearXNG (推荐)", "enabled": True},
                    {"id": "bing", "label": "Bing RSS", "enabled": True},
                    {"id": "ddg", "label": "DuckDuckGo HTML", "enabled": True},
                ],
                ensure_ascii=False,
            ),
            "搜索供应商（JSON 数组：[{\"id\",\"label\",\"enabled\"}]，顺序即优先级）",
        ),
        ("searxng_url", "https://search.bus-hit.me", "SearXNG 实例地址"),
        ("searxng_timeout", "10", "SearXNG 请求超时（秒）"),
        ("websearch_max_results", "6", "搜索结果条数（1-20）"),
        ("websearch_max_pages", "3", "抓取正文的条数"),
        ("websearch_fetch_content", "true", "是否抓取网页正文"),
        ("websearch_max_content", "12000", "单条正文最大长度（字符）"),
    ]
    for key, value, remark in defaults:
        conn.execute(
            "INSERT OR IGNORE INTO settings (key, value, remark, enabled) VALUES (?, ?, ?, 1)",
            (key, value, remark),
        )
    conn.commit()


def _ensure_default_model(conn: sqlite3.Connection) -> None:
    """保证 models 表恰好有一个默认模型：无默认时优先保留历史默认
    tencent/Hunyuan-MT-7B，否则取第一个启用模型；已存在则保持不变。"""
    row = conn.execute("SELECT id FROM models WHERE is_default = 1 LIMIT 1").fetchone()
    if row:
        return
    candidate = conn.execute(
        "SELECT id FROM models WHERE model_key = ? AND enabled = 1",
        ("tencent/Hunyuan-MT-7B",),
    ).fetchone()
    if not candidate:
        candidate = conn.execute(
            "SELECT id FROM models WHERE enabled = 1 ORDER BY sort_order, id LIMIT 1"
        ).fetchone()
    if candidate:
        conn.execute("UPDATE models SET is_default = 1 WHERE id = ?", (candidate["id"],))
        conn.commit()


def init_db() -> None:
    with _lock:
        conn = _connect()
        conn.executescript(SCHEMA)
        conn.commit()
        _migrate()
        _seed_settings(conn)
        _migrate_model_providers(conn)
        _seed_dim_tables(conn)
        _ensure_default_model(conn)


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