"""认证：注册 / 登录 / JWT 签发与校验 / 依赖注入。"""

import time

import bcrypt
import jwt
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from jwt import InvalidTokenError

from .config import settings
from .db import execute, fetch_one, now_ms
from .geo import resolve_ip
from .limiter import check_ip_limit
from .schemas import LoginRequest, ProfileUpdate, RegisterRequest

router = APIRouter(prefix="/api/auth", tags=["auth"])

_ALG = "HS256"


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        return False


def create_token(user_id: int) -> str:
    payload = {
        "sub": str(user_id),
        "exp": int(time.time()) + settings.jwt_expire_days * 86400,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=_ALG)


def decode_token(token: str) -> int | None:
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[_ALG])
        return int(payload["sub"])
    except (InvalidTokenError, KeyError, ValueError):
        return None


def get_current_user(request: Request) -> dict:
    """依赖：校验 Authorization: Bearer <token>，返回用户行。"""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="未登录")
    user_id = decode_token(auth.removeprefix("Bearer ").strip())
    if user_id is None:
        raise HTTPException(status_code=401, detail="登录已过期")
    row = fetch_one("SELECT * FROM users WHERE id = ?", (user_id,))
    if not row or not row["is_active"]:
        raise HTTPException(status_code=401, detail="账号不存在或已禁用")
    return dict(row)


def require_admin(user: dict = Depends(get_current_user)) -> dict:
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


def _log_login(user_id: int, success: bool, ip: str | None) -> None:
    execute(
        "INSERT INTO login_logs (user_id, success, ip, created_at) VALUES (?, ?, ?, ?)",
        (user_id, 1 if success else 0, ip, now_ms()),
    )


@router.post("/register")
def register(body: RegisterRequest, request: Request):
    check_ip_limit(request, "register", settings.register_rate_per_hour, 3600.0)
    exists = fetch_one("SELECT id FROM users WHERE username = ?", (body.username,))
    if exists:
        raise HTTPException(status_code=409, detail="用户名已存在")
    ip = request.client.host if request.client else None
    if body.province or body.city or body.district:
        province, city, district = body.province, body.city, body.district
    else:
        province, city, district = resolve_ip(ip)
    user_id = execute(
        "INSERT INTO users (username, password_hash, role, province, city, district, age, gender, updated_at, updated_by, created_at)"
        " VALUES (?, ?, 'user', ?, ?, ?, ?, ?, ?, ?, ?)",
        (body.username, hash_password(body.password), province, city, district, body.age, body.gender, now_ms(), body.username, now_ms()),
    )
    _log_login(user_id, True, ip)
    return {
        "token": create_token(user_id),
        "user": {
            "id": user_id,
            "username": body.username,
            "role": "user",
            "province": province,
            "city": city,
            "district": district,
            "age": body.age,
            "gender": body.gender,
        },
    }


@router.get("/check-username")
def check_username(username: str = Query(min_length=3, max_length=32)):
    exists = fetch_one("SELECT id FROM users WHERE username = ?", (username,))
    return {"available": not bool(exists)}


@router.post("/login")
def login(body: LoginRequest, request: Request):
    check_ip_limit(request, "login", settings.login_rate_per_minute, 60.0)
    row = fetch_one("SELECT * FROM users WHERE username = ?", (body.username,))
    ip = request.client.host if request.client else None
    if not row or not verify_password(body.password, row["password_hash"]):
        if row:
            _log_login(row["id"], False, ip)
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    if not row["is_active"]:
        raise HTTPException(status_code=403, detail="账号已被禁用")
    _log_login(row["id"], True, ip)
    return {
        "token": create_token(row["id"]),
        "user": {"id": row["id"], "username": row["username"], "role": row["role"]},
    }


_YEAR_MS = 365 * 24 * 3600 * 1000


def _username_changes_left(user: dict) -> int:
    """距当前时刻一年内，该用户名还可修改的次数（上限 3 次/年）。"""
    count = user.get("username_change_count") or 0
    if count < 3:
        return 3 - count
    last = user.get("username_changed_at")
    if last and now_ms() - last >= _YEAR_MS:
        return 3
    return 0


def _user_payload(user: dict) -> dict:
    return {
        "id": user["id"],
        "username": user["username"],
        "role": user["role"],
        "province": user.get("province", ""),
        "city": user.get("city", ""),
        "district": user.get("district", ""),
        "age": user.get("age"),
        "gender": user.get("gender", ""),
        "avatar": user.get("avatar", ""),
        "username_changes_left": _username_changes_left(user),
    }


@router.get("/me")
def me(user: dict = Depends(get_current_user)):
    return _user_payload(user)


@router.patch("/me")
def update_me(body: ProfileUpdate, user: dict = Depends(get_current_user)):
    fields = body.model_fields_set
    updates: list[str] = []
    params: list = []

    if "username" in fields:
        new_name = (body.username or "").strip()
        if new_name != user["username"]:
            if not (3 <= len(new_name) <= 32):
                raise HTTPException(status_code=400, detail="用户名需 3-32 个字符")
            if _username_changes_left(user) <= 0:
                raise HTTPException(status_code=400, detail="用户名每年最多只能修改 3 次")
            exists = fetch_one("SELECT id FROM users WHERE username = ?", (new_name,))
            if exists:
                raise HTTPException(status_code=409, detail="用户名已存在")
            updates.append("username = ?")
            params.append(new_name)
            updates.append("username_change_count = ?")
            params.append((user.get("username_change_count") or 0) + 1)
            updates.append("username_changed_at = ?")
            params.append(now_ms())

    for col in ("avatar", "age", "gender", "province", "city", "district"):
        if col in fields:
            updates.append(f"{col} = ?")
            params.append(getattr(body, col))

    if updates:
        updates.append("updated_at = ?")
        params.append(now_ms())
        updates.append("updated_by = ?")
        params.append(user["username"])
        params.append(user["id"])
        execute(f"UPDATE users SET {', '.join(updates)} WHERE id = ?", params)
        updated = fetch_one("SELECT * FROM users WHERE id = ?", (user["id"],))
        if updated:
            return _user_payload(dict(updated))

    return _user_payload(user)