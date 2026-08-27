"""初始化数据库 + 导入默认模型列表 + 创建管理员账号。

用法：uv run python -m ai_chat_server.seed
"""

import json
import time

from .auth import hash_password
from .config import settings
from .db import execute, execute_many, fetch_one, init_db, now_ms
from .models_seed import MODEL_SEEDS


def seed_models() -> int:
    count = fetch_one("SELECT COUNT(*) AS n FROM models")["n"]
    if count:
        print(f"models 表已有 {count} 条记录，跳过导入")
        return count
    rows = [
        (m.id, m.name, "openai", 1 if m.free else 0, 1 if m.vision else 0, 1, i)
        for i, m in enumerate(MODEL_SEEDS)
    ]
    execute_many(
        "INSERT INTO models (model_key, name, provider, free, vision, enabled, sort_order, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [(r[0], r[1], r[2], r[3], r[4], r[5], r[6], now_ms()) for r in rows],
    )
    # 默认模型：优先历史默认 Hunyuan-MT-7B（禁用则该条不生效，由保底逻辑兜底）
    execute(
        "UPDATE models SET is_default = 1 WHERE model_key = ? AND enabled = 1",
        ("tencent/Hunyuan-MT-7B",),
    )
    print(f"导入 {len(rows)} 个模型")
    return len(rows)


def seed_admin() -> None:
    if not settings.admin_username:
        return
    exists = fetch_one("SELECT id FROM users WHERE username = ?", (settings.admin_username,))
    if exists:
        print(f"管理员 {settings.admin_username} 已存在，跳过")
        return
    if not settings.admin_password:
        print("警告：ADMIN_PASSWORD 未设置，跳过创建管理员")
        return
    execute(
        "INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, 'super_admin', ?)",
        (settings.admin_username, hash_password(settings.admin_password), now_ms()),
    )
    print(f"已创建管理员: {settings.admin_username}")


DEFAULT_SETTINGS = {
    "site_name": "AI Chat",
    "announcement": "",
    "websearch_providers": json.dumps(
        [
            {"id": "baidu", "label": "百度 (中文最佳)", "enabled": True},
            {"id": "searxng", "label": "SearXNG (推荐)", "enabled": True},
            {"id": "bing", "label": "Bing RSS", "enabled": True},
            {"id": "ddg", "label": "DuckDuckGo HTML", "enabled": True},
        ],
        ensure_ascii=False,
    ),
    "searxng_url": "https://search.bus-hit.me",
    "searxng_timeout": "10",
    "websearch_max_results": "6",
    "websearch_max_pages": "3",
    "websearch_fetch_content": "true",
    "websearch_max_content": "12000",
}


def seed_settings() -> None:
    for key, value in DEFAULT_SETTINGS.items():
        exists = fetch_one("SELECT key FROM settings WHERE key = ?", (key,))
        if not exists:
            execute("INSERT INTO settings (key, value, remark, enabled) VALUES (?, ?, ?, 1)", (key, value, f"默认配置: {key}"))
    print(f"已初始化 {len(DEFAULT_SETTINGS)} 个默认设置")


def main() -> None:
    t0 = time.time()
    init_db()
    seed_models()
    seed_admin()
    seed_settings()
    print(f"完成，耗时 {time.time() - t0:.2f}s")


if __name__ == "__main__":
    main()