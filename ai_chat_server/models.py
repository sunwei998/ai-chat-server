"""公开数据接口：模型列表 / 首页推荐热词。"""

from fastapi import APIRouter

from .db import fetch_all

router = APIRouter(prefix="/api", tags=["models"])


@router.get("/models")
def list_public_models():
    rows = fetch_all(
        "SELECT model_key, name, free, vision FROM models WHERE enabled = 1 ORDER BY sort_order, id"
    )
    return [
        {
            "id": r["model_key"],
            "name": r["name"],
            "free": bool(r["free"]),
            "vision": bool(r["vision"]),
        }
        for r in rows
    ]


@router.get("/suggestions")
def list_public_suggestions():
    """首页推荐热词：仅 enabled，按排序取前 6 条。"""
    rows = fetch_all(
        "SELECT title_zh, title_en FROM suggestions "
        "WHERE enabled = 1 ORDER BY sort_order, id LIMIT 6"
    )
    return [{"title_zh": r["title_zh"], "title_en": r["title_en"]} for r in rows]