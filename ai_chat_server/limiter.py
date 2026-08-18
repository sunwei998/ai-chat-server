"""内存滑动窗口限流（单进程内生效，重启即清零）。

生产环境建议换成 Redis 或独立限流服务；当前规模下内存实现足够。
"""

import time
from collections import defaultdict, deque
from functools import wraps

from fastapi import HTTPException, Request

_windows: dict[str, deque[float]] = defaultdict(deque)


def _check(key: str, limit: int, window_sec: float) -> bool:
    now = time.monotonic()
    dq = _windows[key]
    while dq and now - dq[0] > window_sec:
        dq.popleft()
    if len(dq) >= limit:
        return False
    dq.append(now)
    return True


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def check_ip_limit(request: Request, key_prefix: str, limit: int, window_sec: float) -> None:
    """按客户端 IP 限流，超限抛 429。供非依赖注入端点直接调用。"""
    if not _check(f"{key_prefix}:ip:{_client_ip(request)}", limit, window_sec):
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试")


def check_user_limit(user_id: int, key_prefix: str, limit: int, window_sec: float) -> None:
    """按用户 ID 限流，超限抛 429。"""
    if not _check(f"{key_prefix}:user:{user_id}", limit, window_sec):
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试")


def rate_limit(limit: int, window_sec: float, key_prefix: str, by_user: bool = False):
    """依赖注入式限流器。

    by_user=True 时按登录用户限流，否则按客户端 IP。
    """

    def dependency(request: Request, user: dict | None = None):
        key = key_prefix
        if by_user and user:
            key += f":user:{user['id']}"
        else:
            key += f":ip:{_client_ip(request)}"
        if not _check(key, limit, window_sec):
            raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试")
        return True

    return dependency


def limit_dependency(limit: int, window_sec: float, key_prefix: str):
    """给非依赖注入端点使用的装饰器（按 IP）。"""

    def decorator(func):
        @wraps(func)
        def wrapper(request: Request, *args, **kwargs):
            if not _check(f"{key_prefix}:ip:{_client_ip(request)}", limit, window_sec):
                raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试")
            return func(request, *args, **kwargs)

        return wrapper

    return decorator
