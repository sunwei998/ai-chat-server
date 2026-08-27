"""管理端 API：概览统计 / 用户管理 / 用量统计 / 模型配置 / 系统设置。"""

from fastapi import APIRouter, Depends, HTTPException, Query

from .auth import (
    ROLE_SUPER_ADMIN,
    compute_age,
    hash_password,
    require_admin,
    require_model_admin,
    require_settings_admin,
    require_super_admin,
)
from .db import execute, fetch_all, fetch_one, now_ms, transaction
from .schemas import (
    ModelPayload,
    ResetPasswordRequest,
    SettingsPayload,
    UserUpdate,
)

router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(require_admin)])

_DAY_MS = 24 * 3600 * 1000

_PERIODS: dict[str, int] = {
    "day": 1 * _DAY_MS,
    "week": 7 * _DAY_MS,
    "month": 30 * _DAY_MS,
    "year": 365 * _DAY_MS,
}


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


@router.get("/overview")
def overview():
    """管理台概览：核心指标 + 多维图表数据（趋势/时段/新增/排行/画像）。"""
    now = now_ms()
    start30 = now - 30 * _DAY_MS

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

    daily = fetch_all(
        """
        SELECT (created_at / 86400000) AS day, COUNT(*) AS requests,
               COALESCE(SUM(total_tokens), 0) AS total
        FROM token_usage WHERE created_at >= ? GROUP BY day ORDER BY day
        """,
        (start30,),
    )

    hourly = fetch_all(
        """
        SELECT (created_at / 3600000) % 24 AS hour, COUNT(*) AS requests,
               COALESCE(SUM(total_tokens), 0) AS total
        FROM token_usage WHERE created_at >= ? GROUP BY hour ORDER BY hour
        """,
        (start30,),
    )

    new_users = fetch_all(
        """
        SELECT (created_at / 86400000) AS day, COUNT(*) AS n
        FROM users WHERE created_at >= ? GROUP BY day ORDER BY day
        """,
        (start30,),
    )

    by_model = fetch_all(
        """
        SELECT model_key, COUNT(*) AS requests,
               COALESCE(SUM(total_tokens), 0) AS total
        FROM token_usage WHERE created_at >= ?
        GROUP BY model_key ORDER BY total DESC LIMIT 8
        """,
        (start30,),
    )

    top_users = fetch_all(
        """
        SELECT u.username, u.avatar, u.province, u.city,
               COALESCE(SUM(t.total_tokens), 0) AS total,
               COUNT(t.id) AS requests
        FROM users u JOIN token_usage t ON t.user_id = u.id
        WHERE t.created_at >= ?
        GROUP BY u.id ORDER BY total DESC LIMIT 8
        """,
        (start30,),
    )

    top_provinces = fetch_all(
        """
        SELECT u.province,
               COUNT(DISTINCT t.user_id) AS active_users,
               COUNT(t.id) AS requests,
               COALESCE(SUM(t.total_tokens), 0) AS total_tokens
        FROM users u JOIN token_usage t ON t.user_id = u.id
        WHERE u.province != '' AND t.created_at >= ?
        GROUP BY u.province ORDER BY active_users DESC, total_tokens DESC LIMIT 8
        """,
        (start30,),
    )

    recent_users = fetch_all(
        """
        SELECT username, avatar, province, city, district, created_at
        FROM users ORDER BY created_at DESC LIMIT 8
        """
    )

    age_dist, gender_dist = _aggregate_demographics()

    return {
        "stats": {
            "users": users,
            "active_today": active_today,
            "active_7d": active_7d,
            "total_tokens": total_tokens,
            "today_tokens": today_tokens,
            "requests": requests,
        },
        "daily": [dict(r) for r in daily],
        "hourly": [dict(r) for r in hourly],
        "new_users": [dict(r) for r in new_users],
        "by_model": [dict(r) for r in by_model],
        "top_users": [dict(r) for r in top_users],
        "top_provinces": [dict(r) for r in top_provinces],
        "recent_users": [dict(r) for r in recent_users],
        "age_dist": age_dist,
        "gender_dist": gender_dist,
    }


@router.get("/users")
def list_users(
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
    search: str = "",
    username: str = "",
    gender: str = "",
    role: str = "",
    is_active: str = "",
    sort: str = "",
    order: str = "desc",
):
    offset = (page - 1) * page_size
    clauses: list[str] = []
    params: list = []
    if search:
        clauses.append("u.username LIKE ?")
        params.append(f"%{search}%")
    if username:
        clauses.append("u.username LIKE ?")
        params.append(f"%{username}%")
    if gender:
        genders = [g.strip() for g in gender.split(",") if g.strip()]
        if genders:
            clauses.append("u.gender IN (" + ",".join("?" for _ in genders) + ")")
            params.extend(genders)
    if role:
        roles = [r.strip() for r in role.split(",") if r.strip()]
        if roles:
            clauses.append("u.role IN (" + ",".join("?" for _ in roles) + ")")
            params.extend(roles)
    if is_active:
        clauses.append("u.is_active = ?")
        params.append(1 if is_active.strip().lower() == "true" else 0)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    # 排序（字段白名单，防 SQL 注入）
    sort_columns = {
        "username": "u.username",
        "role": "u.role",
        "is_active": "u.is_active",
        "gender": "u.gender",
        "birthday": "u.birthday",
        "created_at": "u.created_at",
        "updated_at": "u.updated_at",
        "last_seen_at": "u.last_seen_at",
        "logins": "logins",
        "total_tokens": "total_tokens",
    }
    order_by = "u.created_at DESC"
    if sort in sort_columns:
        direction = "ASC" if order.strip().lower() == "asc" else "DESC"
        order_by = f"{sort_columns[sort]} {direction}"
    total = fetch_one(f"SELECT COUNT(*) AS n FROM users u {where}", params)["n"]
    rows = fetch_all(
        f"""
        SELECT u.id, u.username, u.role, u.is_active, u.created_at, u.last_seen_at,
               u.province, u.city, u.district, u.age, u.birthday, u.gender,
               u.updated_at, u.updated_by,
               COALESCE((SELECT COUNT(*) FROM login_logs l WHERE l.user_id = u.id AND l.success = 1), 0) AS logins,
               COALESCE((SELECT SUM(t.total_tokens) FROM token_usage t WHERE t.user_id = u.id), 0) AS total_tokens
        FROM users u {where} ORDER BY {order_by}
        LIMIT ? OFFSET ?
        """,
        (*params, page_size, offset),
    )
    return {"items": [dict(r) for r in rows], "total": total, "page": page, "pageSize": page_size}


@router.get("/users/{user_id}")
def get_user(user_id: int):
    row = fetch_one(
        """
        SELECT u.id, u.username, u.role, u.is_active, u.created_at, u.last_seen_at,
               u.province, u.city, u.district, u.age, u.birthday, u.gender,
               u.updated_at, u.updated_by,
               COALESCE((SELECT COUNT(*) FROM login_logs l WHERE l.user_id = u.id AND l.success = 1), 0) AS logins,
               COALESCE((SELECT SUM(t.total_tokens) FROM token_usage t WHERE t.user_id = u.id), 0) AS total_tokens
        FROM users u WHERE u.id = ?
        """,
        (user_id,),
    )
    if not row:
        raise HTTPException(status_code=404, detail="用户不存在")
    return dict(row)


@router.patch("/users/{user_id}")
def update_user(user_id: int, body: UserUpdate, admin: dict = Depends(require_super_admin)):
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
    if body.birthday is not None:
        sets.append("birthday = ?")
        params.append(body.birthday)
        sets.append("age = ?")
        params.append(compute_age(body.birthday) if body.birthday else None)
    if body.gender is not None:
        sets.append("gender = ?")
        params.append(body.gender)
    if not sets:
        return {"ok": True}
    sets.append("updated_at = ?")
    params.append(now_ms())
    sets.append("updated_by = ?")
    params.append(admin["username"])
    params.append(user_id)
    execute(f"UPDATE users SET {', '.join(sets)} WHERE id = ?", params)
    return {"ok": True}


@router.post("/users/{user_id}/reset-password")
def reset_password(user_id: int, body: ResetPasswordRequest, admin: dict = Depends(require_super_admin)):
    row = fetch_one("SELECT id FROM users WHERE id = ?", (user_id,))
    if not row:
        raise HTTPException(status_code=404, detail="用户不存在")
    execute(
        "UPDATE users SET password_hash = ?, updated_at = ?, updated_by = ? WHERE id = ?",
        (hash_password(body.password), now_ms(), admin["username"], user_id),
    )
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
    """按行业标准年龄段与性别聚合用户分布；未填写(生日/性别)统一归为 unknown。"""
    rows = fetch_all("SELECT age, birthday, gender FROM users")
    age_map = {k: 0 for k in _AGE_ORDER}
    gender_map = {k: 0 for k in _GENDER_ORDER}
    for r in rows:
        age = compute_age(r["birthday"]) if r["birthday"] else r["age"]
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
def region_stats(period: str = "month"):
    """按统计周期聚合省份热度指标与省/市/区用户分布。

    - provinces: 每个省份的 新增用户数 / 活跃用户数 / 对话次数 / Token 消耗量（仅统计周期内）
    - regions:   省/市/区粒度用户分布 + 活跃 TOP3（requests/tokens 限定统计周期）
    """
    if period not in _PERIODS:
        raise HTTPException(status_code=400, detail="period 仅支持 day/week/month/year")
    start = now_ms() - _PERIODS[period]

    prov_rows = fetch_all(
        """
        SELECT u.province,
               COUNT(DISTINCT CASE WHEN u.created_at >= ? THEN u.id END) AS new_users,
               COUNT(DISTINCT t.user_id) AS active_users,
               COUNT(t.id) AS requests,
               COALESCE(SUM(t.total_tokens), 0) AS total_tokens
        FROM users u
        LEFT JOIN token_usage t ON t.user_id = u.id AND t.created_at >= ?
        WHERE u.province != ''
        GROUP BY u.province
        """,
        (start, start),
    )
    provinces = [
        {
            "province": r["province"],
            "new_users": r["new_users"],
            "active_users": r["active_users"],
            "requests": r["requests"],
            "total_tokens": r["total_tokens"],
        }
        for r in prov_rows
    ]

    rows = fetch_all(
        """
        SELECT u.username, u.avatar, u.province, u.city, u.district,
               COUNT(t.id) AS requests,
               COALESCE(SUM(t.total_tokens), 0) AS total_tokens
        FROM users u
        LEFT JOIN token_usage t ON t.user_id = u.id AND t.created_at >= ?
        WHERE u.province != '' OR u.city != '' OR u.district != ''
        GROUP BY u.id
        ORDER BY u.province, u.city, u.district
        """,
        (start,),
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
                "avatar": r["avatar"] if r["avatar"] else "",
                "requests": r["requests"],
                "total_tokens": r["total_tokens"],
            }
        )
    for reg in regions.values():
        reg["top_users"].sort(key=lambda u: (u["requests"], u["total_tokens"]), reverse=True)
        reg["top_users"] = reg["top_users"][:10]
    return {"period": period, "provinces": provinces, "regions": list(regions.values())}


@router.get("/models")
def list_models(page: int = Query(1, ge=1), page_size: int = Query(10, ge=1, le=100), search: str = ""):
    offset = (page - 1) * page_size
    where = ""
    params: list = []
    if search:
        where = "WHERE model_key LIKE ? OR name LIKE ? OR provider LIKE ?"
        params.extend([f"%{search}%", f"%{search}%", f"%{search}%"])
    total = fetch_one(f"SELECT COUNT(*) AS n FROM models {where}", params)["n"]
    rows = fetch_all(
        f"SELECT * FROM models {where} ORDER BY sort_order, id LIMIT ? OFFSET ?",
        (*params, page_size, offset),
    )
    return {"items": [dict(r) for r in rows], "total": total, "page": page, "pageSize": page_size}


@router.get("/models/{model_id}")
def get_model(model_id: int):
    row = fetch_one("SELECT * FROM models WHERE id = ?", (model_id,))
    if not row:
        raise HTTPException(status_code=404, detail="模型不存在")
    return dict(row)


@router.post("/models")
def create_model(body: ModelPayload, admin: dict = Depends(require_model_admin)):
    exists = fetch_one("SELECT id FROM models WHERE model_key = ?", (body.model_key,))
    if exists:
        raise HTTPException(status_code=409, detail="model_key 已存在")
    if body.is_default and not body.enabled:
        raise HTTPException(status_code=400, detail="禁用模型不能设为默认模型")
    new_id = execute(
        "INSERT INTO models (model_key, name, provider, free, vision, supports_search, enabled, sort_order, is_default, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            body.model_key,
            body.name,
            body.provider,
            1 if body.free else 0,
            1 if body.vision else 0,
            1 if body.supports_search else 0,
            1 if body.enabled else 0,
            body.sort_order,
            1 if body.is_default else 0,
            now_ms(),
        ),
    )
    if body.is_default:
        # 设默认时清除其它默认，保证全局唯一
        transaction([
            ("UPDATE models SET is_default = 0 WHERE id <> ?", (new_id,)),
            ("UPDATE models SET is_default = 1 WHERE id = ?", (new_id,)),
        ])
    return {"ok": True}


@router.put("/models/{model_id}")
def update_model(model_id: int, body: ModelPayload, admin: dict = Depends(require_model_admin)):
    row = fetch_one("SELECT id, enabled, is_default FROM models WHERE id = ?", (model_id,))
    if not row:
        raise HTTPException(status_code=404, detail="模型不存在")

    effective_enabled = body.enabled if body.enabled is not None else bool(row["enabled"])

    if not body.is_default:
        # 未请求设为默认：保护与默认相关的约束
        if body.enabled is False and row["is_default"]:
            raise HTTPException(status_code=400, detail="默认模型不能被禁用，请先指定其它默认模型")
        if body.is_default is False and row["is_default"]:
            raise HTTPException(status_code=400, detail="必须保留一个默认模型，请先指定其它默认模型")
        # 普通更新，不动 is_default
        execute(
            "UPDATE models SET model_key=?, name=?, provider=?, free=?, vision=?, supports_search=?, enabled=?, sort_order=? WHERE id=?",
            (
                body.model_key,
                body.name,
                body.provider,
                1 if body.free else 0,
                1 if body.vision else 0,
                1 if body.supports_search else 0,
                1 if body.enabled else 0,
                body.sort_order,
                model_id,
            ),
        )
        return {"ok": True}

    # 请求设为默认：禁用模型不允许
    if not effective_enabled:
        raise HTTPException(status_code=400, detail="禁用模型不能设为默认模型")
    # 清除其它默认 + 更新本行字段并置为默认，保证全局唯一
    params = (
        body.model_key,
        body.name,
        body.provider,
        1 if body.free else 0,
        1 if body.vision else 0,
        1 if body.supports_search else 0,
        1 if body.enabled else 0,
        body.sort_order,
        model_id,
    )
    transaction([
        ("UPDATE models SET is_default = 0 WHERE id <> ?", (model_id,)),
        (
            "UPDATE models SET model_key=?, name=?, provider=?, free=?, vision=?, supports_search=?, enabled=?, sort_order=?, is_default=1 WHERE id=?",
            params,
        ),
    ])
    return {"ok": True}


@router.delete("/models/{model_id}")
def delete_model(model_id: int, admin: dict = Depends(require_model_admin)):
    execute("DELETE FROM models WHERE id = ?", (model_id,))
    return {"ok": True}


@router.get("/settings")
def list_settings(page: int = Query(1, ge=1), page_size: int = Query(10, ge=1, le=100), search: str = ""):
    offset = (page - 1) * page_size
    where = ""
    params: list = []
    if search:
        where = "WHERE key LIKE ? OR value LIKE ? OR remark LIKE ?"
        params.extend([f"%{search}%", f"%{search}%", f"%{search}%"])
    total = fetch_one(f"SELECT COUNT(*) AS n FROM settings {where}", params)["n"]
    rows = fetch_all(
        f"SELECT * FROM settings {where} ORDER BY key LIMIT ? OFFSET ?",
        (*params, page_size, offset),
    )
    return {"items": [dict(r) for r in rows], "total": total, "page": page, "pageSize": page_size}


@router.patch("/settings/{key}")
def update_setting(key: str, body: SettingsPayload, admin: dict = Depends(require_settings_admin)):
    execute(
        "INSERT OR IGNORE INTO settings (key, value, remark, enabled) VALUES (?, '', '', 1)",
        (key,),
    )
    updates: list[str] = []
    params: list = []
    if body.value is not None:
        updates.append("value = ?")
        params.append(body.value)
    if body.remark is not None:
        updates.append("remark = ?")
        params.append(body.remark)
    if body.enabled is not None:
        updates.append("enabled = ?")
        params.append(1 if body.enabled else 0)
    if updates:
        params.append(key)
        execute(f"UPDATE settings SET {', '.join(updates)} WHERE key = ?", params)
    return {"ok": True}