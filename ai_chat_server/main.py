"""FastAPI 入口：装配应用、CORS、路由、启动初始化。

启动：uv run uvicorn ai_chat_server.main:app --reload --port 8000
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .admin import purge_expired_transfer_files, router as admin_router
from .auth import ROLE_SUPER_ADMIN, router as auth_router
from .chat import router as chat_router
from .config import settings
from .db import execute, init_db
from .hotwords import router as hotwords_router
from .models import router as models_router
from .sessions import router as sessions_router


def migrate_roles() -> None:
    """数据割接：旧角色 admin → super_admin（幂等，可重复执行）。"""
    execute(
        "UPDATE users SET role = ? WHERE role = 'admin'",
        (ROLE_SUPER_ADMIN,),
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    migrate_roles()
    # 启动兜底：按各自保留期清理导入源文件与导出产物（只删文件、保留记录），补上停机期间错过的清理
    try:
        purge_expired_transfer_files()
    except Exception:
        pass
    yield


app = FastAPI(title="AI Chat Server", version="0.1.0", lifespan=lifespan)

origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins if origins != ["*"] else ["*"],
    allow_credentials=origins != ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router)
app.include_router(chat_router)
app.include_router(models_router)
app.include_router(admin_router)
app.include_router(hotwords_router)
app.include_router(sessions_router)


@app.get("/api/health")
def health():
    return {"status": "ok"}