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
  model_id INTEGER,
  prompt_tokens INTEGER NOT NULL DEFAULT 0,
  completion_tokens INTEGER NOT NULL DEFAULT 0,
  total_tokens INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS models (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  model_key TEXT NOT NULL,
  name TEXT NOT NULL,
  name_en TEXT NOT NULL DEFAULT '',
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

CREATE TABLE IF NOT EXISTS setting_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  setting_key TEXT NOT NULL,
  content TEXT NOT NULL DEFAULT '',
  content_en TEXT NOT NULL DEFAULT '',
  operator TEXT NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_setting_logs_key ON setting_logs (setting_key, created_at);

CREATE TABLE IF NOT EXISTS admin_operation_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  entity TEXT NOT NULL,
  entity_id INTEGER NOT NULL,
  content TEXT NOT NULL DEFAULT '',
  content_en TEXT NOT NULL DEFAULT '',
  operator TEXT NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_admin_op_logs_entity ON admin_operation_logs (entity, entity_id, created_at);

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
  name_en TEXT NOT NULL DEFAULT '',
  api_key TEXT NOT NULL DEFAULT '',
  sort_order INTEGER NOT NULL DEFAULT 0,
  enabled INTEGER NOT NULL DEFAULT 1,
  remark TEXT NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL,
  updated_at INTEGER,
  UNIQUE (table_id, code),
  FOREIGN KEY (table_id) REFERENCES dim_tables(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_dim_values_table ON dim_values (table_id);

CREATE TABLE IF NOT EXISTS dim_table_fields (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  table_id INTEGER NOT NULL,
  field_key TEXT NOT NULL,
  label_zh TEXT NOT NULL DEFAULT '',
  label_en TEXT NOT NULL DEFAULT '',
  field_type TEXT NOT NULL DEFAULT 'text',
  required INTEGER NOT NULL DEFAULT 0,
  max_len INTEGER NOT NULL DEFAULT 0,
  no_cjk INTEGER NOT NULL DEFAULT 0,
  sort_order INTEGER NOT NULL DEFAULT 0,
  enabled INTEGER NOT NULL DEFAULT 1,
  UNIQUE (table_id, field_key),
  FOREIGN KEY (table_id) REFERENCES dim_tables(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_dim_fields_table ON dim_table_fields (table_id);

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
  status TEXT NOT NULL DEFAULT '',
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
    slog_cols = {r[1] for r in conn.execute("PRAGMA table_info(setting_logs)")}
    if "content_en" not in slog_cols:
        conn.execute("ALTER TABLE setting_logs ADD COLUMN content_en TEXT NOT NULL DEFAULT ''")
    dv_cols = {r[1] for r in conn.execute("PRAGMA table_info(dim_values)")}
    if "name_en" not in dv_cols:
        conn.execute("ALTER TABLE dim_values ADD COLUMN name_en TEXT NOT NULL DEFAULT ''")
    if "api_key" not in dv_cols:
        conn.execute("ALTER TABLE dim_values ADD COLUMN api_key TEXT NOT NULL DEFAULT ''")
    model_cols = {r[1] for r in conn.execute("PRAGMA table_info(models)")}
    if "supports_search" not in model_cols:
        conn.execute("ALTER TABLE models ADD COLUMN supports_search INTEGER NOT NULL DEFAULT 1")
    if "is_default" not in model_cols:
        conn.execute("ALTER TABLE models ADD COLUMN is_default INTEGER NOT NULL DEFAULT 0")
    if "name_en" not in model_cols:
        conn.execute("ALTER TABLE models ADD COLUMN name_en TEXT NOT NULL DEFAULT ''")
    has_transfer = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='transfer_records'"
    ).fetchone()
    if has_transfer:
        tr_cols = {r[1] for r in conn.execute("PRAGMA table_info(transfer_records)")}
        if "status" not in tr_cols:
            conn.execute("ALTER TABLE transfer_records ADD COLUMN status TEXT NOT NULL DEFAULT ''")
    # model_key 放宽为 provider 级唯一：旧表带列级 UNIQUE，SQLite 只能重建表去除
    tu_cols = {r[1] for r in conn.execute("PRAGMA table_info(token_usage)")}
    if "model_id" not in tu_cols:
        conn.execute("ALTER TABLE token_usage ADD COLUMN model_id INTEGER")
    models_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='models'"
    ).fetchone()
    if models_sql and "UNIQUE" in (models_sql[0] or "").upper():
        conn.executescript(
            """
            CREATE TABLE models_new (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              model_key TEXT NOT NULL,
              name TEXT NOT NULL,
              name_en TEXT NOT NULL DEFAULT '',
              provider TEXT NOT NULL DEFAULT 'openai',
              free INTEGER NOT NULL DEFAULT 0,
              vision INTEGER NOT NULL DEFAULT 0,
              supports_search INTEGER NOT NULL DEFAULT 1,
              enabled INTEGER NOT NULL DEFAULT 1,
              sort_order INTEGER NOT NULL DEFAULT 0,
              is_default INTEGER NOT NULL DEFAULT 0,
              created_at INTEGER NOT NULL
            );
            INSERT INTO models_new (id, model_key, name, name_en, provider, free, vision,
              supports_search, enabled, sort_order, is_default, created_at)
            SELECT id, model_key, name, name_en, provider, free, vision,
              supports_search, enabled, sort_order, is_default, created_at
            FROM models;
            DROP TABLE models;
            ALTER TABLE models_new RENAME TO models;
            """
        )
    conn.commit()


# 默认模型供应商（同时作为维表 model_provider 的 seed 与前端极端降级）
DEFAULT_MODEL_PROVIDERS = [
    ("openai", "OpenAI", "OpenAI"),
    ("anthropic", "Anthropic Claude", "Anthropic Claude"),
    ("google", "Google Gemini", "Google Gemini"),
    ("deepseek", "DeepSeek", "DeepSeek"),
    ("qwen", "通义千问", "Qwen"),
    ("zhipu", "智谱", "GLM"),
    ("moonshot", "Moonshot Kimi", "Moonshot Kimi"),
    ("doubao", "豆包", "Doubao"),
    ("baidu", "百度文心", "ERNIE"),
    ("tencent", "腾讯混元", "Hunyuan"),
    ("minimax", "MiniMax", "MiniMax"),
    ("mistral", "Mistral", "Mistral"),
    ("meta", "Meta Llama", "Meta Llama"),
    ("groq", "Groq", "Groq"),
    ("openrouter", "OpenRouter", "OpenRouter"),
    ("cohere", "Cohere", "Cohere"),
    ("xai", "xAI Grok", "xAI Grok"),
    ("stepfun", "阶跃星辰", "StepFun"),
    ("siliconflow", "硅基流动", "SiliconFlow"),
    ("ollama", "Ollama", "Ollama"),
]


# ============ 维表字段配置（dim_table_fields） ============
# dim_values 的物理列是固定 7 列超集，字段配置只控制「该维表启用哪些列、列头叫什么、怎么校验」，
# 因此新增一种维表不需要改表结构，只需要在 dim_table_fields 里写几行。
#
# 元组含义：
#   field_key, label_zh, label_en, field_type, required, max_len, no_cjk, sort_order, enabled
_DIM_FIELD_TEMPLATE: tuple[tuple, ...] = (
    ("code", "编码", "Code", "text", 1, 64, 0, 10, 1),
    ("name", "名称", "Name", "text", 1, 128, 0, 20, 1),
    ("sort_order", "排序", "Sort", "int", 0, 0, 0, 50, 1),
    ("enabled", "启用", "Enabled", "bool", 0, 0, 0, 60, 1),
    ("remark", "备注", "Remark", "text", 0, 255, 0, 70, 1),
)

# 特定维表在通用列之外追加的字段（按 table_code）
_DIM_EXTRA_FIELDS: dict[str, tuple[tuple, ...]] = {
    "model_provider": (
        ("name_en", "英文名称", "English Name", "text", 1, 128, 1, 30, 1),
        ("api_key", "API密钥", "API Key", "secret", 0, 512, 0, 40, 1),
    ),
}

# dim_values 真实存在的物理列。field_key 会被拼进 INSERT/UPDATE 语句，必须白名单校验。
DIM_PHYSICAL_COLUMNS: frozenset[str] = frozenset(
    {"code", "name", "name_en", "api_key", "sort_order", "enabled", "remark"}
)

# 导入时定位行的依据列，配置层面不允许关闭/置为非必填
DIM_CORE_FIELDS: tuple[str, ...] = ("code", "name")


def default_dim_fields(table_code: str) -> list[tuple]:
    """某维表的默认字段配置行（通用列 + 该表专属列，按 sort_order 升序）。"""
    rows = list(_DIM_FIELD_TEMPLATE) + list(_DIM_EXTRA_FIELDS.get(table_code, ()))
    return sorted(rows, key=lambda r: r[7])


def _seed_dim_fields_locked(conn: sqlite3.Connection, table_id: int | None = None) -> None:
    """为尚无字段配置的维表按模板回填默认配置。幂等：已有任何一行配置就不再动，尊重用户改动。
    调用方需持有 _lock（init_db 在同一把锁内调用）。"""
    if table_id is None:
        tables = conn.execute("SELECT id, code FROM dim_tables").fetchall()
    else:
        tables = conn.execute(
            "SELECT id, code FROM dim_tables WHERE id = ?", (table_id,)
        ).fetchall()
    for t in tables:
        has = conn.execute(
            "SELECT id FROM dim_table_fields WHERE table_id = ? LIMIT 1", (t["id"],)
        ).fetchone()
        if has:
            continue
        for row in default_dim_fields(t["code"]):
            conn.execute(
                "INSERT OR IGNORE INTO dim_table_fields "
                "(table_id, field_key, label_zh, label_en, field_type, required, max_len, no_cjk, sort_order, enabled) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (t["id"], *row),
            )
    conn.commit()


def seed_dim_table_fields(table_id: int | None = None) -> None:
    """为尚无字段配置的维表回填默认字段配置（幂等）。table_id 为空时处理全部维表。"""
    with _lock:
        _seed_dim_fields_locked(_connect(), table_id)


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
    for idx, (code, name, name_en) in enumerate(DEFAULT_MODEL_PROVIDERS):
        conn.execute(
            "INSERT OR IGNORE INTO dim_values (table_id, code, name, name_en, sort_order, enabled, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 1, ?, ?)",
            (table_id, code, name, name_en, idx, ts, ts),
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
        # 旧配置项可能只有 name（中英混合），name_en 留空由展示回退 name
        conn.execute(
            "INSERT OR IGNORE INTO dim_values (table_id, code, name, name_en, sort_order, enabled, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 1, ?, ?)",
            (table_id, str(item["id"]), str(item.get("name", item["id"])), "", idx, ts, ts),
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
        ("import_file_retention_hours", "720", "导入源文件磁盘保留时长（小时，正整数；超期只删源文件、保留导入记录，720=30天）"),
        ("export_file_retention_hours", "720", "导出文件磁盘保留时长（小时，正整数；超期只删导出文件、保留导出记录，720=30天）"),
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


def _refresh_provider_names(conn: sqlite3.Connection) -> None:
    """刷写模型供应商维表的中英文名：按 code 用默认字典把 name 拆成纯中文、name_en 填英文。
    幂等：默认字典外的自定义项（用户新增）只补 name_en 缺失时的不做破坏，保留原名。
    """
    table = conn.execute("SELECT id FROM dim_tables WHERE code = 'model_provider'").fetchone()
    if not table:
        return
    table_id = table["id"]
    now = now_ms()
    for code, name, name_en in DEFAULT_MODEL_PROVIDERS:
        row = conn.execute(
            "SELECT id, name, name_en FROM dim_values WHERE table_id = ? AND code = ?",
            (table_id, code),
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE dim_values SET name = ?, name_en = ?, updated_at = ? WHERE id = ?",
                (name, name_en, now, row["id"]),
            )
        else:
            conn.execute(
                "INSERT OR IGNORE INTO dim_values (table_id, code, name, name_en, sort_order, enabled, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 999, 1, ?, ?)",
                (table_id, code, name, name_en, now, now),
            )
    conn.commit()


def _enforce_provider_api_key(conn: sqlite3.Connection) -> None:
    """业务规则落地：模型供应商（本地 ollama 无需密钥、予以豁免）必须配置 api_key 才能启用。
    幂等：每次启动把「非 ollama 且 api_key 为空」的供应商强制置为禁用。"""
    table = conn.execute("SELECT id FROM dim_tables WHERE code = 'model_provider'").fetchone()
    if not table:
        return
    conn.execute(
        "UPDATE dim_values SET enabled = 0 WHERE table_id = ? AND code <> 'ollama' "
        "AND (api_key IS NULL OR api_key = '')",
        (table["id"],),
    )
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
        _seed_dim_fields_locked(conn)
        _refresh_provider_names(conn)
        _enforce_provider_api_key(conn)
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