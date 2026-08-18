"""公开模型列表：GET /api/models（仅 enabled，前端 ModelSelector 数据源）。"""

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