"""管理端 API：概览统计 / 用户管理 / 用量统计 / 模型配置 / 系统设置 / 导入导出记录。"""

import io
import json
import os

import openpyxl
from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from openpyxl.worksheet.datavalidation import DataValidation

from .auth import (
    ROLE_SUPER_ADMIN,
    compute_age,
    hash_password,
    require_admin,
    require_model_admin,
    require_settings_admin,
    require_super_admin,
)
from .config import settings
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
    """按行业标准年龄段与性别聚合：
    - 年龄分布统计 Token 消耗量（按年龄分组的 token_usage 总量）
    - 性别分布统计用户人数；未填写(生日/性别)统一归为 unknown。"""
    rows = fetch_all(
        """
        SELECT u.age, u.birthday, u.gender, COALESCE(SUM(t.total_tokens), 0) AS tokens
        FROM users u LEFT JOIN token_usage t ON t.user_id = u.id
        GROUP BY u.id
        """
    )
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
        age_map[age_key] += r["tokens"]
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
def list_models(
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
    search: str = "",
    enabled: str = "",
    free: str = "",
    provider: str = "",
    sort: str = "",
    order: str = "asc",
):
    offset = (page - 1) * page_size
    clauses: list[str] = []
    params: list = []
    if search:
        clauses.append("(model_key LIKE ? OR name LIKE ? OR provider LIKE ?)")
        params.extend([f"%{search}%", f"%{search}%", f"%{search}%"])
    if enabled:
        clauses.append("enabled = ?")
        params.append(1 if enabled.strip().lower() == "true" else 0)
    if free:
        clauses.append("free = ?")
        params.append(1 if free.strip().lower() == "true" else 0)
    if provider:
        providers = [p.strip() for p in provider.split(",") if p.strip()]
        if providers:
            clauses.append("provider IN (" + ",".join("?" for _ in providers) + ")")
            params.extend(providers)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    # 排序（字段白名单，防注入）
    sort_columns = {
        "model_key": "model_key",
        "name": "name",
        "provider": "provider",
        "sort_order": "sort_order",
        "free": "free",
        "enabled": "enabled",
        "vision": "vision",
        "supports_search": "supports_search",
    }
    order_by = "sort_order, id"
    if sort in sort_columns:
        direction = "ASC" if order.strip().lower() == "asc" else "DESC"
        order_by = f"{sort_columns[sort]} {direction}, id"
    total = fetch_one(f"SELECT COUNT(*) AS n FROM models {where}", params)["n"]
    rows = fetch_all(
        f"SELECT * FROM models {where} ORDER BY {order_by} LIMIT ? OFFSET ?",
        (*params, page_size, offset),
    )
    return {"items": [dict(r) for r in rows], "total": total, "page": page, "pageSize": page_size}


# 模型可导入导出的字段顺序（与 xlsx 表头一致）
_MODEL_FIELDS = ["model_key", "name", "provider", "free", "vision", "supports_search", "enabled", "sort_order", "is_default"]


def _model_rows() -> list[list]:
    rows = fetch_all("SELECT * FROM models ORDER BY sort_order, id")
    return [[r[f] if r[f] is not None else "" for f in _MODEL_FIELDS] for r in rows]


def _write_transfer_record(type_: str, username: str, filename: str, data: bytes, mime_type: str, remark: str = "") -> str:
    """把源文件落盘到 transfers 目录，并写入导入/导出记录。返回文件绝对路径。"""
    os.makedirs(_TRANSFER_DIR, exist_ok=True)
    ts = now_ms()
    safe_name = f"{ts}_{filename}"
    path = os.path.join(_TRANSFER_DIR, safe_name)
    with open(path, "wb") as f:
        f.write(data)
    execute(
        "INSERT INTO transfer_records (type, username, filename, file_size, mime_type, file_path, remark, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (type_, username, filename, len(data), mime_type, path, remark, ts),
    )
    return path


@router.get("/models/export")
def export_models(admin: dict = Depends(require_model_admin)):
    """导出全部模型为 xlsx（Excel 表格），并写入导出记录。"""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "models"
    ws.append(_MODEL_FIELDS)
    for row in _model_rows():
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    data = buf.getvalue()

    filename = f"models_{now_ms()}.xlsx"
    username = admin.get("username") or "admin"
    _write_transfer_record(
        "export", username, filename, data,
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        remark="模型数据导出",
    )
    return StreamingResponse(
        iter([data]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _get_provider_ids() -> list[str]:
    """读取 settings.model_providers 字典，返回 provider id 列表。"""
    row = fetch_one("SELECT value FROM settings WHERE key = 'model_providers'")
    if not row or not row["value"]:
        return []
    try:
        parsed = json.loads(row["value"])
        if isinstance(parsed, list):
            return [str(p.get("id")) for p in parsed if isinstance(p, dict) and p.get("id")]
    except (json.JSONDecodeError, TypeError):
        pass
    return []


@router.get("/models/template")
def download_model_template(admin: dict = Depends(require_model_admin)):
    """下载模型导入模板：表头 + 示例行 + 枚举列下拉校验（provider / 布尔列）。"""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "models"
    ws.append(_MODEL_FIELDS)
    # 示例行：与导入解析逻辑对齐（布尔列用「是/否」，_parse_bool 可正确解析）
    ws.append(["example/model-key", "示例模型名称", "openai", "是", "否", "是", "是", 100, "否"])

    # provider 列（第 3 列 C）下拉：从数据字典取 id
    provider_ids = _get_provider_ids()
    if provider_ids:
        dv_provider = DataValidation(
            type="list",
            formula1='"' + ",".join(provider_ids) + '"',
            allow_blank=True,
            showDropDown=False,
        )
        dv_provider.error = "请从下拉列表选择 provider"
        dv_provider.errorTitle = "非法值"
        ws.add_data_validation(dv_provider)
        dv_provider.add("C2:C1000")

    # 布尔列下拉（是/否）：free(D) / vision(E) / supports_search(F) / enabled(G) / is_default(I)
    dv_bool = DataValidation(
        type="list",
        formula1='"是,否"',
        allow_blank=True,
        showDropDown=False,
    )
    dv_bool.error = "请选择「是」或「否」"
    dv_bool.errorTitle = "非法值"
    ws.add_data_validation(dv_bool)
    for col in ("D", "E", "F", "G", "I"):
        dv_bool.add(f"{col}2:{col}1000")

    buf = io.BytesIO()
    wb.save(buf)
    data = buf.getvalue()
    return StreamingResponse(
        iter([data]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="models_template.xlsx"'},
    )


def _parse_bool(v) -> int:
    if isinstance(v, bool):
        return 1 if v else 0
    if isinstance(v, (int, float)):
        return 1 if v else 0
    s = str(v or "").strip().lower()
    return 1 if s in ("1", "true", "yes", "是", "y", "on") else 0


@router.post("/models/import")
async def import_models(file: UploadFile = File(...), admin: dict = Depends(require_model_admin)):
    """从 xlsx（Excel 表格）批量导入模型：model_key 已存在则更新，否则新增。"""
    raw = await file.read()
    if len(raw) > 10 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="文件过大（上限 10MB）")
    original_name = file.filename or "models_import.xlsx"
    username = admin.get("username") or "admin"

    # 先落盘源文件并写导入记录（无论后续解析是否成功，都保留导入痕迹）
    saved_path = _write_transfer_record(
        "import", username, original_name, raw,
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        remark="模型数据导入",
    )

    try:
        wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True)
    except Exception:
        raise HTTPException(status_code=400, detail="无法解析文件，请上传有效的 .xlsx 文件")

    ws = wb.active
    rows_iter = ws.iter_rows(values_only=True)
    try:
        header_row = next(rows_iter)
    except StopIteration:
        raise HTTPException(status_code=400, detail="空文件")

    # 表头兼容：去空格、小写
    header = [str(h).strip().lower() if h is not None else "" for h in header_row]
    if "model_key" not in header:
        raise HTTPException(status_code=400, detail="xlsx 缺少 model_key 列")
    if "name" not in header:
        raise HTTPException(status_code=400, detail="xlsx 缺少 name 列")

    idx = {h: i for i, h in enumerate(header)}
    def cell(row, key: str, default=""):
        i = idx.get(key)
        if i is None or i >= len(row):
            return default
        v = row[i]
        return "" if v is None else str(v).strip()

    created = 0
    updated = 0
    errors: list[str] = []
    for n, row in enumerate(rows_iter, start=2):
        if row is None or not any(c is not None and str(c).strip() for c in row):
            continue
        model_key = cell(row, "model_key")
        name = cell(row, "name")
        if not model_key or not name:
            errors.append(f"第 {n} 行：model_key/name 不能为空")
            continue
        provider = cell(row, "provider", "openai") or "openai"
        try:
            sort_order = int(float(cell(row, "sort_order", "0") or "0"))
        except ValueError:
            sort_order = 0
        free = _parse_bool(cell(row, "free", "0"))
        vision = _parse_bool(cell(row, "vision", "0"))
        supports_search = _parse_bool(cell(row, "supports_search", "1"))
        enabled = _parse_bool(cell(row, "enabled", "1"))
        is_default = _parse_bool(cell(row, "is_default", "0"))

        exists = fetch_one("SELECT id FROM models WHERE model_key = ?", (model_key,))
        if exists:
            execute(
                "UPDATE models SET name=?, provider=?, free=?, vision=?, supports_search=?, enabled=?, sort_order=?, is_default=? WHERE model_key=?",
                (name, provider, free, vision, supports_search, enabled, sort_order, is_default, model_key),
            )
            updated += 1
        else:
            execute(
                "INSERT INTO models (model_key, name, provider, free, vision, supports_search, enabled, sort_order, is_default, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (model_key, name, provider, free, vision, supports_search, enabled, sort_order, is_default, now_ms()),
            )
            created += 1

    # 若导入导致多个默认模型，则收敛为唯一默认（优先 sort_order 最小且 enabled 的）
    if fetch_one("SELECT COUNT(*) AS n FROM models WHERE is_default = 1")["n"] > 1:
        execute("UPDATE models SET is_default = 0")
        d = fetch_one("SELECT id FROM models WHERE enabled = 1 ORDER BY sort_order, id LIMIT 1")
        if d:
            execute("UPDATE models SET is_default = 1 WHERE id = ?", (d["id"],))

    return {"ok": True, "created": created, "updated": updated, "errors": errors}


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
def list_settings(
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
    search: str = "",
    enabled: str = "",
    sort: str = "",
    order: str = "asc",
):
    offset = (page - 1) * page_size
    clauses: list[str] = []
    params: list = []
    if search:
        clauses.append("(key LIKE ? OR value LIKE ? OR remark LIKE ?)")
        params.extend([f"%{search}%", f"%{search}%", f"%{search}%"])
    if enabled:
        clauses.append("enabled = ?")
        params.append(1 if enabled.strip().lower() == "true" else 0)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    sort_columns = {"key": "key", "value": "value", "remark": "remark", "enabled": "enabled"}
    order_by = "key"
    if sort in sort_columns:
        direction = "ASC" if order.strip().lower() == "asc" else "DESC"
        order_by = f"{sort_columns[sort]} {direction}"
    total = fetch_one(f"SELECT COUNT(*) AS n FROM settings {where}", params)["n"]
    rows = fetch_all(
        f"SELECT * FROM settings {where} ORDER BY {order_by} LIMIT ? OFFSET ?",
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


# ============ 导入 / 导出记录（所有管理员可见，可下载源文件） ============

_TRANSFER_DIR = os.path.join(os.path.dirname(settings.db_path) or ".", "transfers")


@router.get("/transfers")
def list_transfers(
    type: str = Query("import", description="import / export"),
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
    sort: str = "",
    order: str = "desc",
):
    if type not in ("import", "export"):
        raise HTTPException(status_code=400, detail="type 仅支持 import/export")
    offset = (page - 1) * page_size
    clauses = ["type = ?"]
    params: list = [type]
    where = "WHERE " + " AND ".join(clauses)
    sort_columns = {
        "id": "id",
        "filename": "filename",
        "username": "username",
        "file_size": "file_size",
        "created_at": "created_at",
    }
    order_by = "created_at DESC, id DESC"
    if sort in sort_columns:
        direction = "ASC" if order.strip().lower() == "asc" else "DESC"
        order_by = f"{sort_columns[sort]} {direction}, id DESC"
    total = fetch_one(f"SELECT COUNT(*) AS n FROM transfer_records {where}", params)["n"]
    rows = fetch_all(
        f"SELECT * FROM transfer_records {where} ORDER BY {order_by} LIMIT ? OFFSET ?",
        (*params, page_size, offset),
    )
    items = []
    for r in rows:
        d = dict(r)
        d["has_file"] = bool(d.get("file_path")) and os.path.isfile(d["file_path"])
        items.append(d)
    return {"items": items, "total": total, "page": page, "pageSize": page_size}


@router.get("/transfers/{record_id}/download")
def download_transfer(record_id: int):
    row = fetch_one("SELECT * FROM transfer_records WHERE id = ?", (record_id,))
    if not row:
        raise HTTPException(status_code=404, detail="记录不存在")
    file_path = row["file_path"]
    if not file_path:
        raise HTTPException(status_code=404, detail="该记录没有源文件")
    # 防路径穿越：强制限制在 transfers 目录内
    base = os.path.abspath(_TRANSFER_DIR)
    full = os.path.abspath(file_path)
    if not (full == base or full.startswith(base + os.sep)):
        raise HTTPException(status_code=400, detail="非法文件路径")
    if not os.path.isfile(full):
        raise HTTPException(status_code=404, detail="源文件不存在或已被清理")
    media_type = row["mime_type"] or "application/octet-stream"
    return FileResponse(full, filename=row["filename"], media_type=media_type)