"""FastAPI 應用程式入口（企業級工程排程與自動化 SaaS / CPM）。

職責：
  - 建立 FastAPI app，掛載 CORS
  - 在 settings.api_v1_prefix 之下註冊所有路由器
  - 提供 /health 健康檢查
  - 啟動時 best-effort ping Redis（失敗不阻斷啟動）

多租戶：所有 /api/v1 端點皆需 X-Tenant-Id 標頭，並透過 RLS 隔離資料。
雙區域：X-Region（TW / CN）影響在地化與通知通道（LINE / 釘釘）。
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.routers import (
    schedule_router,
    projects_router,
    tasks_router,
    erp_router,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("cpm.main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """應用生命週期：啟動時 best-effort 連線 Redis（失敗只記錄、不中斷）。"""
    redis_client = None
    try:
        import redis.asyncio as aioredis

        redis_client = aioredis.from_url(
            settings.redis_url, encoding="utf-8", decode_responses=True
        )
        pong = await redis_client.ping()
        logger.info("Redis connected (PING -> %s)", pong)
        app.state.redis = redis_client
    except Exception as exc:  # noqa: BLE001 - Redis 非啟動必要條件
        logger.warning("Redis ping failed (continuing without cache): %s", exc)
        app.state.redis = None

    try:
        yield
    finally:
        if redis_client is not None:
            try:
                await redis_client.aclose()
            except Exception:  # noqa: BLE001
                pass


app = FastAPI(
    title="企業級工程排程與自動化 SaaS (CPM / Critical Path)",
    description=(
        "Cross-strait (TW/CN) multi-tenant CPM scheduling SaaS for construction "
        "firms & engineering consultancies. 跨海峽多租戶要徑工程排程平台。"
    ),
    version="1.0.0",
    lifespan=lifespan,
)

# CORS（來源由設定檔提供，逗號分隔已於 config 解析為 list）
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 在 API 前綴下註冊所有路由器
_prefix = settings.api_v1_prefix
app.include_router(schedule_router, prefix=_prefix)
app.include_router(projects_router, prefix=_prefix)
app.include_router(tasks_router, prefix=_prefix)
app.include_router(erp_router, prefix=_prefix)


@app.get("/health", tags=["meta"])
async def health() -> dict:
    """健康檢查端點（不需 tenant 標頭）。"""
    return {"status": "ok"}
