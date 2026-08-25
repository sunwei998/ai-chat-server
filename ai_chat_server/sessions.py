"""会话/消息持久化 API：按账号隔离，服务端统一排序。

排序规则（GET /sessions）：
  置顶组按 pinned_at 倒序（最新置顶在前），非置顶组按 updated_at 倒序（最近活跃在前）。
"""

import json

from fastapi import APIRouter, Depends, HTTPException

from .auth import get_current_user
from .db import execute, fetch_all, fetch_one, now_ms, transaction
from .schemas import SessionCreate, SessionPatch

router = APIRouter(prefix="/api/sessions", tags=["sessions"])

LAST_PREVIEW_LEN = 50

DEFAULT_PAGE_SIZE = 10
MAX_PAGE_SIZE = 200


def _page_setting(key: str, default: int) -> int:
    """读取 settings 表的分页条数；缺失/禁用/非正整数回退默认值。"""
    row = fetch_one("SELECT value, enabled FROM settings WHERE key = ?", (key,))
    if not row or not row["enabled"]:
        return default
    try:
        n = int(str(row["value"]).strip())
    except (ValueError, TypeError):
        return default
    if n <= 0:
        return default
    return min(n, MAX_PAGE_SIZE)


def _new_id(prefix: str) -> str:
    import secrets

    return f"{prefix}_{now_ms()}_{secrets.token_hex(4)}"


def _session_rows(user_id: int) -> list[dict]:
    rows = fetch_all(
        """
        SELECT
          s.id, s.title, s.model, s.web_search, s.pinned, s.pinned_at,
          s.created_at, s.updated_at,
          (SELECT COUNT(*) FROM messages m WHERE m.session_id = s.id) AS message_count,
          (SELECT m.content FROM messages m WHERE m.session_id = s.id
             ORDER BY m.created_at DESC, m.id DESC LIMIT 1) AS last_content
        FROM sessions s
        WHERE s.user_id = ?
        ORDER BY s.pinned DESC, COALESCE(s.pinned_at, 0) DESC, s.updated_at DESC
        """,
        (user_id,),
    )
    return [
        {
            "id": r["id"],
            "title": r["title"],
            "model": r["model"],
            "web_search": bool(r["web_search"]),
            "pinned": bool(r["pinned"]),
            "pinned_at": r["pinned_at"],
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
            "message_count": r["message_count"],
            "last_preview": (r["last_content"] or "")[:LAST_PREVIEW_LEN],
        }
        for r in rows
    ]


def _get_owned(user_id: int, session_id: str) -> dict:
    row = fetch_one(
        "SELECT * FROM sessions WHERE id = ? AND user_id = ?", (session_id, user_id)
    )
    if not row:
        raise HTTPException(status_code=404, detail="会话不存在")
    return dict(row)


@router.get("")
def list_sessions(user: dict = Depends(get_current_user)):
    return _session_rows(user["id"])


@router.post("")
def create_session(
    body: SessionCreate = SessionCreate(), user: dict = Depends(get_current_user)
):
    sid = _new_id("session")
    now = now_ms()
    execute(
        "INSERT INTO sessions (id, user_id, title, model, web_search, pinned, pinned_at, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, 0, NULL, ?, ?)",
        (
            sid,
            user["id"],
            body.title,
            body.model,
            1 if body.web_search else 0,
            now,
            now,
        ),
    )
    return _session_rows(user["id"])


@router.get("/{session_id}/messages")
def get_messages(
    session_id: str,
    before_id: str | None = None,
    user: dict = Depends(get_current_user),
):
    _get_owned(user["id"], session_id)
    limit = _page_setting(
        "chat_initial_page_size" if not before_id else "chat_page_size",
        DEFAULT_PAGE_SIZE,
    )

    where = "session_id = ?"
    params: list = [session_id]
    if before_id:
        anchor = fetch_one(
            "SELECT created_at, id FROM messages WHERE id = ? AND session_id = ?",
            (before_id, session_id),
        )
        if anchor:
            where += " AND (created_at < ? OR (created_at = ? AND id < ?))"
            params += [anchor["created_at"], anchor["created_at"], before_id]

    rows = fetch_all(
        f"SELECT * FROM messages WHERE {where} ORDER BY created_at DESC, id DESC LIMIT ?",
        [*params, limit],
    )
    result = []
    for r in reversed(rows):
        try:
            images = json.loads(r["images"] or "[]")
        except json.JSONDecodeError:
            images = []
        try:
            citations = json.loads(r["citations"] or "[]")
        except json.JSONDecodeError:
            citations = []
        result.append(
            {
                "id": r["id"],
                "role": r["role"],
                "content": r["content"],
                "images": images,
                "citations": citations,
                "timestamp": r["created_at"],
            }
        )

    has_more = False
    if rows:
        oldest = rows[-1]
        has_more = bool(
            fetch_one(
                "SELECT 1 FROM messages WHERE session_id = ?"
                " AND (created_at < ? OR (created_at = ? AND id < ?)) LIMIT 1",
                [session_id, oldest["created_at"], oldest["created_at"], oldest["id"]],
            )
        )

    return {"messages": result, "has_more": has_more}


@router.patch("/{session_id}")
def patch_session(
    session_id: str,
    body: SessionPatch,
    user: dict = Depends(get_current_user),
):
    _get_owned(user["id"], session_id)
    updates: list[str] = []
    params: list = []

    if body.title is not None:
        updates.append("title = ?")
        params.append(body.title[:200])
    if body.model is not None:
        updates.append("model = ?")
        params.append(body.model[:100])
    if body.web_search is not None:
        updates.append("web_search = ?")
        params.append(1 if body.web_search else 0)
    if body.pinned is not None:
        updates.append("pinned = ?")
        params.append(1 if body.pinned else 0)
        updates.append("pinned_at = ?")
        params.append(now_ms() if body.pinned else None)

    if updates:
        updates.append("updated_at = ?")
        params.append(now_ms())
        params.append(session_id)
        params.append(user["id"])
        execute(
            f"UPDATE sessions SET {', '.join(updates)} WHERE id = ? AND user_id = ?",
            params,
        )
    return _session_rows(user["id"])


@router.delete("/{session_id}")
def delete_session(session_id: str, user: dict = Depends(get_current_user)):
    _get_owned(user["id"], session_id)
    execute("DELETE FROM sessions WHERE id = ? AND user_id = ?", (session_id, user["id"]))
    return _session_rows(user["id"])


@router.delete("/{session_id}/messages/{message_id}")
def delete_message(session_id: str, message_id: str, user: dict = Depends(get_current_user)):
    _get_owned(user["id"], session_id)
    transaction(
        [
            ("DELETE FROM messages WHERE session_id = ? AND id = ?", (session_id, message_id)),
            ("UPDATE sessions SET updated_at = ? WHERE id = ?", (now_ms(), session_id)),
        ]
    )
    return _session_rows(user["id"])


@router.delete("/{session_id}/messages")
def clear_messages(session_id: str, user: dict = Depends(get_current_user)):
    _get_owned(user["id"], session_id)
    transaction(
        [
            ("DELETE FROM messages WHERE session_id = ?", (session_id,)),
            (
                "UPDATE sessions SET title = '', updated_at = ? WHERE id = ?",
                (now_ms(), session_id),
            ),
        ]
    )
    return _session_rows(user["id"])
