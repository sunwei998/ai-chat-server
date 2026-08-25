"""FastAPI 入口：装配应用、CORS、路由、启动初始化。

启动：uv run uvicorn ai_chat_server.main:app --reload --port 8000
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .admin import router as admin_router
from .auth import router as auth_router
from .chat import router as chat_router
from .config import settings
from .db import init_db
from .hotwords import router as hotwords_router
from .models import router as models_router
from .sessions import router as sessions_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
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