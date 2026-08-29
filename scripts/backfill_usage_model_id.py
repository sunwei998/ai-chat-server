#!/usr/bin/env python3
"""一次性脚本：回填历史 token_usage 的 model_id（只触碰 model_id IS NULL 的行）。

在仓库根目录运行：
    .venv/bin/python scripts/backfill_usage_model_id.py            # 执行回填
    .venv/bin/python scripts/backfill_usage_model_id.py --dry-run  # 仅预览影响行数

匹配规则：同 model_key 的模型行；同 key 跨多个 provider 时取 sort_order, id
顺序的首行（与聊天链路 model_key 回退解析语义一致）。幂等，可安全重跑。
"""

import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)  # 保证 .env 与相对 db_path 解析一致
sys.path.insert(0, str(ROOT))

from ai_chat_server.config import settings  # noqa: E402

MATCH = (
    "SELECT m.id FROM models m WHERE m.model_key = token_usage.model_key "
    "ORDER BY m.sort_order, m.id LIMIT 1"
)


def main() -> None:
    dry = "--dry-run" in sys.argv
    conn = sqlite3.connect(settings.db_path)
    pending = conn.execute(
        f"SELECT COUNT(*) FROM token_usage WHERE model_id IS NULL AND EXISTS ({MATCH})"
    ).fetchone()[0]
    orphan = conn.execute(
        f"SELECT COUNT(*) FROM token_usage WHERE model_id IS NULL AND NOT EXISTS ({MATCH})"
    ).fetchone()[0]
    print(f"待回填: {pending} 行；无匹配模型的孤儿行（跳过）: {orphan} 行")
    if dry:
        print("[dry-run] 不写入任何变更")
        return
    cur = conn.execute(
        f"UPDATE token_usage SET model_id = ({MATCH}) "
        f"WHERE model_id IS NULL AND EXISTS ({MATCH})"
    )
    conn.commit()
    conn.close()
    print(f"已回填: {cur.rowcount} 行")


if __name__ == "__main__":
    main()
