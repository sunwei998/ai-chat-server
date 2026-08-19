"""管理端 API：概览统计 / 用户管理 / 用量统计 / 模型配置 / 系统设置。"""

from fastapi import APIRouter, Depends, HTTPException

from .auth import hash_password, require_admin
from .db import execute, fetch_all, fetch_one, now_ms
from .schemas import (
    ModelPayload,
    ResetPasswordRequest,
    SettingsPayload,
    SuggestionPayload,
    UserUpdate,
)

router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(require_admin)])

_DAY_MS = 24 * 3600 * 1000


@router.get("/stats")
def stats():
    now = now_ms()
    users = fetch_one("SELECT COUNT(*) AS n FROM users")["n"]
    active_today = fetch_one(
        "SELECT COUNT(DISTINCT user_id) AS n FROM token_usage WHERE created_at >= ?",
        (now - _DAY_MS,),
    )["n"]
    active_7d = fetch_one(
        "SELECT COUNT(DISTINCT user_id) AS n FROM token_usage WHERE created_at >= ?",
        (now - 7 * _DAY_MS,),
    )["n"]
    total_tokens = fetch_one(
        "SELECT COALESCE(SUM(total_tokens), 0) AS n FROM token_usage"
    )["n"]
    today_tokens = fetch_one(
        "SELECT COALESCE(SUM(total_tokens), 0) AS n FROM token_usage WHERE created_at >= ?",
        (now - _DAY_MS,),
    )["n"]
    requests = fetch_one("SELECT COUNT(*) AS n FROM token_usage")["n"]
    return {
        "users": users,
        "active_today": active_today,
        "active_7d": active_7d,
        "total_tokens": total_tokens,
        "today_tokens": today_tokens,
        "requests": requests,
    }


@router.get("/users")
def list_users():
    rows = fetch_all(
        """
        SELECT u.id, u.username, u.role, u.is_active, u.created_at, u.last_seen_at,
               u.province, u.city, u.district, u.age, u.gender,
               COALESCE((SELECT COUNT(*) FROM login_logs l WHERE l.user_id = u.id AND l.success = 1), 0) AS logins,
               COALESCE((SELECT SUM(t.total_tokens) FROM token_usage t WHERE t.user_id = u.id), 0) AS total_tokens
        FROM users u ORDER BY u.created_at DESC
        """
    )
    return [dict(r) for r in rows]


@router.patch("/users/{user_id}")
def update_user(user_id: int, body: UserUpdate):
    row = fetch_one("SELECT * FROM users WHERE id = ?", (user_id,))
    if not row:
        raise HTTPException(status_code=404, detail="用户不存在")
    sets, params = [], []
    if body.is_active is not None:
        sets.append("is_active = ?")
        params.append(1 if body.is_active else 0)
    if body.role is not None:
        sets.append("role = ?")
        params.append(body.role)
    if body.province is not None:
        sets.append("province = ?")
        params.append(body.province)
    if body.city is not None:
        sets.append("city = ?")
        params.append(body.city)
    if body.district is not None:
        sets.append("district = ?")
        params.append(body.district)
    if body.age is not None:
        sets.append("age = ?")
        params.append(body.age)
    if body.gender is not None:
        sets.append("gender = ?")
        params.append(body.gender)
    if not sets:
        return {"ok": True}
    params.append(user_id)
    execute(f"UPDATE users SET {', '.join(sets)} WHERE id = ?", params)
    return {"ok": True}


@router.post("/users/{user_id}/reset-password")
def reset_password(user_id: int, body: ResetPasswordRequest):
    row = fetch_one("SELECT id FROM users WHERE id = ?", (user_id,))
    if not row:
        raise HTTPException(status_code=404, detail="用户不存在")
    execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(body.password), user_id))
    return {"ok": True}


@router.get("/usage")
def usage():
    now = now_ms()
    by_user = fetch_all(
        """
        SELECT u.username, COALESCE(SUM(t.total_tokens), 0) AS total,
               COALESCE(SUM(t.prompt_tokens), 0) AS prompt,
               COALESCE(SUM(t.completion_tokens), 0) AS completion,
               COUNT(t.id) AS requests
        FROM users u LEFT JOIN token_usage t ON t.user_id = u.id
        GROUP BY u.id ORDER BY total DESC
        """
    )
    by_model = fetch_all(
        """
        SELECT model_key, COUNT(*) AS requests,
               COALESCE(SUM(prompt_tokens), 0) AS prompt,
               COALESCE(SUM(completion_tokens), 0) AS completion,
               COALESCE(SUM(total_tokens), 0) AS total
        FROM token_usage GROUP BY model_key ORDER BY total DESC
        """
    )
    daily = fetch_all(
        """
        SELECT (created_at / 86400000) AS day, COUNT(*) AS requests,
               COALESCE(SUM(total_tokens), 0) AS total
        FROM token_usage WHERE created_at >= ? GROUP BY day ORDER BY day
        """,
        (now - 30 * _DAY_MS,),
    )
    age_dist, gender_dist = _aggregate_demographics()
    return {
        "by_user": [dict(r) for r in by_user],
        "by_model": [dict(r) for r in by_model],
        "daily": [dict(r) for r in daily],
        "age_dist": age_dist,
        "gender_dist": gender_dist,
    }


_AGE_ORDER = ["0-17", "18-24", "25-34", "35-44", "45-54", "55-64", "65+", "unknown"]
_GENDER_ORDER = ["male", "female", "other", "unknown"]


def _aggregate_demographics() -> tuple[list[dict], list[dict]]:
    """按行业标准年龄段与性别聚合用户分布；空值归为 unknown。"""
    rows = fetch_all("SELECT age, gender FROM users")
    age_map = {k: 0 for k in _AGE_ORDER}
    gender_map = {k: 0 for k in _GENDER_ORDER}
    for r in rows:
        age = r["age"]
        if age is None:
            age_key = "unknown"
        elif age < 18:
            age_key = "0-17"
        elif age <= 24:
            age_key = "18-24"
        elif age <= 34:
            age_key = "25-34"
        elif age <= 44:
            age_key = "35-44"
        elif age <= 54:
            age_key = "45-54"
        elif age <= 64:
            age_key = "55-64"
        else:
            age_key = "65+"
        age_map[age_key] += 1
        g = (r["gender"] or "").strip().lower()
        gender_key = g if g in ("male", "female", "other") else "unknown"
        gender_map[gender_key] += 1
    age_dist = [{"key": k, "count": age_map[k]} for k in _AGE_ORDER]
    gender_dist = [{"key": k, "count": gender_map[k]} for k in _GENDER_ORDER]
    return age_dist, gender_dist


@router.get("/region-stats")
def region_stats():
    """按省/市/区聚合用户分布 + 每个地区活跃 TOP3 用户，供前端热点图使用。"""
    rows = fetch_all(
        """
        SELECT u.username, u.province, u.city, u.district,
               COALESCE((SELECT COUNT(*) FROM token_usage t WHERE t.user_id = u.id), 0) AS requests,
               COALESCE((SELECT SUM(t.total_tokens) FROM token_usage t WHERE t.user_id = u.id), 0) AS total_tokens
        FROM users u
        WHERE u.province != '' OR u.city != '' OR u.district != ''
        ORDER BY u.province, u.city, u.district
        """
    )
    regions: dict[tuple[str, str, str], dict] = {}
    for r in rows:
        key = (r["province"], r["city"], r["district"])
        reg = regions.setdefault(
            key,
            {
                "province": r["province"],
                "city": r["city"],
                "district": r["district"],
                "count": 0,
                "top_users": [],
            },
        )
        reg["count"] += 1
        reg["top_users"].append(
            {
                "username": r["username"],
                "requests": r["requests"],
                "total_tokens": r["total_tokens"],
            }
        )
    for reg in regions.values():
        reg["top_users"].sort(key=lambda u: (u["requests"], u["total_tokens"]), reverse=True)
        reg["top_users"] = reg["top_users"][:3]
    return list(regions.values())


@router.get("/models")
def list_models():
    rows = fetch_all("SELECT * FROM models ORDER BY sort_order, id")
    return [dict(r) for r in rows]


@router.post("/models")
def create_model(body: ModelPayload):
    exists = fetch_one("SELECT id FROM models WHERE model_key = ?", (body.model_key,))
    if exists:
        raise HTTPException(status_code=409, detail="model_key 已存在")
    execute(
        "INSERT INTO models (model_key, name, provider, free, vision, enabled, sort_order, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            body.model_key,
            body.name,
            body.provider,
            1 if body.free else 0,
            1 if body.vision else 0,
            1 if body.enabled else 0,
            body.sort_order,
            now_ms(),
        ),
    )
    return {"ok": True}


@router.put("/models/{model_id}")
def update_model(model_id: int, body: ModelPayload):
    row = fetch_one("SELECT id FROM models WHERE id = ?", (model_id,))
    if not row:
        raise HTTPException(status_code=404, detail="模型不存在")
    execute(
        "UPDATE models SET model_key=?, name=?, provider=?, free=?, vision=?, enabled=?, sort_order=? WHERE id=?",
        (
            body.model_key,
            body.name,
            body.provider,
            1 if body.free else 0,
            1 if body.vision else 0,
            1 if body.enabled else 0,
            body.sort_order,
            model_id,
        ),
    )
    return {"ok": True}


@router.delete("/models/{model_id}")
def delete_model(model_id: int):
    execute("DELETE FROM models WHERE id = ?", (model_id,))
    return {"ok": True}


@router.get("/settings")
def list_settings():
    rows = fetch_all("SELECT * FROM settings ORDER BY key")
    return [dict(r) for r in rows]


@router.patch("/settings/{key}")
def update_setting(key: str, body: SettingsPayload):
    execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, body.value),
    )
    return {"ok": True}


@router.get("/suggestions")
def list_suggestions():
    rows = fetch_all("SELECT * FROM suggestions ORDER BY sort_order, id")
    return [dict(r) for r in rows]


@router.post("/suggestions")
def create_suggestion(body: SuggestionPayload):
    execute(
        "INSERT INTO suggestions (title_zh, title_en, sort_order, enabled, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (
            body.title_zh,
            body.title_en,
            body.sort_order,
            1 if body.enabled else 0,
            now_ms(),
        ),
    )
    return {"ok": True}


@router.put("/suggestions/{suggestion_id}")
def update_suggestion(suggestion_id: int, body: SuggestionPayload):
    row = fetch_one("SELECT id FROM suggestions WHERE id = ?", (suggestion_id,))
    if not row:
        raise HTTPException(status_code=404, detail="热词不存在")
    execute(
        "UPDATE suggestions SET title_zh=?, title_en=?, sort_order=?, enabled=? WHERE id=?",
        (
            body.title_zh,
            body.title_en,
            body.sort_order,
            1 if body.enabled else 0,
            suggestion_id,
        ),
    )
    return {"ok": True}


@router.delete("/suggestions/{suggestion_id}")
def delete_suggestion(suggestion_id: int):
    execute("DELETE FROM suggestions WHERE id = ?", (suggestion_id,))
    return {"ok": True}