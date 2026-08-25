"""公开数据接口：模型列表 / 用户提问高频词（对话页推荐词）。"""

from fastapi import APIRouter, Query

from .db import fetch_all
from .hotwords import hot_words

router = APIRouter(prefix="/api", tags=["models"])


@router.get("/models")
def list_public_models():
    rows = fetch_all(
        "SELECT model_key, name, free, vision, supports_search FROM models WHERE enabled = 1 ORDER BY sort_order, id"
    )
    return [
        {
            "id": r["model_key"],
            "name": r["name"],
            "free": bool(r["free"]),
            "vision": bool(r["vision"]),
            "supports_search": bool(r["supports_search"]),
        }
        for r in rows
    ]


@router.get("/hot-words")
def list_public_hot_words(limit: int = Query(4, ge=1, le=50)):
    """对话页推荐词：直接取用户提问高频词 TOP N（不区分语言，语言切换不影响）。"""
    return hot_words(period="month", limit=limit)