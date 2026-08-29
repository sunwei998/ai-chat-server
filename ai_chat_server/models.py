"""公开数据接口：模型列表 / 用户提问高频词（对话页推荐词）。"""

from fastapi import APIRouter, Query, Request

from .db import fetch_all
from .hotwords import hot_words

router = APIRouter(prefix="/api", tags=["models"])


@router.get("/models")
def list_public_models(request: Request):
    is_en = "en" in (request.headers.get("accept-language") or "").lower()
    rows = fetch_all(
        "SELECT id, model_key, name, name_en, free, vision, supports_search, is_default FROM models WHERE enabled = 1 ORDER BY sort_order, id"
    )
    return [
        {
            # model_key 仅 provider 级唯一，稳定引用用数字 id（前端按不透明字符串处理）
            "id": str(r["id"]),
            "model_key": r["model_key"],
            # 名称按语言环境本地化：英文优先 name_en，缺失回退中文名
            "name": (r["name_en"] or r["name"]) if is_en else r["name"],
            "name_en": r["name_en"],
            "free": bool(r["free"]),
            "vision": bool(r["vision"]),
            "supports_search": bool(r["supports_search"]),
            "is_default": bool(r["is_default"]),
        }
        for r in rows
    ]


@router.get("/hot-words")
def list_public_hot_words(limit: int = Query(4, ge=1, le=50)):
    """对话页推荐词：直接取用户提问高频词 TOP N（不区分语言，语言切换不影响）。"""
    return hot_words(period="month", limit=limit)