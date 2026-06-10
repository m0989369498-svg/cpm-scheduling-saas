"""資料庫連線與 RLS (Row Level Security) Session 模式 — 權威來源。

所有 API 請求皆透過 ``get_db`` 取得 AsyncSession，並於交易內設定
``app.current_tenant`` GUC，PostgreSQL RLS 政策即依此值過濾租戶資料。

worker (app.erp.worker) 使用「自己的」engine/session，且不設定
app.current_tenant —— 它僅存取 erp_integration.* (無 RLS)。
"""
from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import settings


class Base(DeclarativeBase):
    """SQLAlchemy 2.0 declarative 基底類別。所有 ORM 模型繼承之。"""


# 非同步引擎 (asyncpg)。pool_pre_ping 確保連線健康；可由設定調整。
engine = create_async_engine(
    settings.database_url,
    echo=False,
    pool_pre_ping=True,
    future=True,
)

# Session factory。expire_on_commit=False：commit 後物件仍可讀取屬性，
# 便於 router 於回傳前序列化 ORM 物件。
SessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def set_tenant_guc(session: AsyncSession, tenant_id: str) -> None:
    """於目前交易內設定 RLS 租戶 GUC。

    is_local=true => 作用域限定於該交易，於連線池環境下安全
    (交易結束後自動失效，不會洩漏到下一個借用此連線的請求)。
    """
    await session.execute(
        text("SELECT set_config('app.current_tenant', :t, true)"),
        {"t": tenant_id},
    )


async def get_db_for_tenant(tenant_id: str) -> AsyncGenerator[AsyncSession, None]:
    """為指定租戶開啟 AsyncSession 並設定 RLS GUC。

    可被 FastAPI 依賴 (get_db) 或內部服務 (例如報表/通知) 重用。
    成功則 commit；發生例外則 rollback。
    """
    async with SessionLocal() as session:
        async with session.begin():
            # 進入交易後、yield 之前先設定租戶 GUC，RLS 政策才會生效。
            await set_tenant_guc(session, tenant_id)
            try:
                yield session
                # 離開內層 with (session.begin) 時若無例外會自動 commit。
            except Exception:
                # 交由 begin() context manager 觸發 rollback；重新拋出。
                raise


async def get_worker_session() -> AsyncSession:
    """供 ERP worker 使用的獨立 session。

    刻意不設定 app.current_tenant —— worker 跨租戶掃描 erp_integration.*
    (該 schema 無 RLS，由程式以 tenant_id 過濾)。呼叫端負責關閉/commit。
    """
    return SessionLocal()
