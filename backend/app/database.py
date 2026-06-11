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

from app.config import is_sqlite, settings


class Base(DeclarativeBase):
    """SQLAlchemy 2.0 declarative 基底類別。所有 ORM 模型繼承之。"""


# 非同步引擎 (LAZY / 延遲建立)。
#   設計理由：``create_async_engine`` 會「在建立當下」即匯入對應 DBAPI driver
#   (PostgreSQL+asyncpg 會 import asyncpg)。在 Windows-ARM64 等「無 asyncpg
#   wheel」的開發機上，若於 module import 時就以預設的 postgres DSN 建立 engine，
#   單純「匯入 app」即會 ModuleNotFoundError: asyncpg 而整個應用無法啟動 / 測試
#   無法收集。為支援「sqlite 原生 dev 模式」與「production postgres」共用同一份
#   程式碼，engine / sessionmaker 改為「首次存取時才建立」(lazy)：
#     - dev 機只要 DATABASE_URL=sqlite，import 全程不碰 asyncpg。
#     - 若 DATABASE_URL 仍為 postgres 但啟動時切到 sqlite (測試 rebind)，亦不會
#       於 import 階段就嘗試 asyncpg。
#     - production (Linux, 有 asyncpg) 行為不變：首次取用 SessionLocal 即建立。
#   對外契約不變：``engine`` 與 ``SessionLocal`` 仍可如常以模組屬性存取
#   (透過 PEP 562 module __getattr__ 於首次存取時建立並快取)。
#
#   - PostgreSQL (asyncpg)：pool_pre_ping 確保連線健康。
#   - sqlite (aiosqlite, dev 原生模式)：sqlite 無 schema 概念，故以
#     schema_translate_map 將 ORM 中的 "erp_integration" schema 映射為 None
#     (即 main schema)，使 create_all 與一般查詢都能在單一 sqlite 檔/記憶體運作。
_engine = None
_SessionLocal = None


def _build_engine():
    """依目前 settings.database_url 建立 AsyncEngine (sqlite / postgres 分支)。"""
    if is_sqlite():
        return create_async_engine(
            settings.database_url,
            echo=False,
            future=True,
            # aiosqlite：允許跨 thread 使用 (TestClient/uvicorn 場景);
            # 不啟用 pool_pre_ping (sqlite 無此需求)。
            connect_args={"check_same_thread": False},
            execution_options={"schema_translate_map": {"erp_integration": None}},
        )
    return create_async_engine(
        settings.database_url,
        echo=False,
        pool_pre_ping=True,
        future=True,
        # --- 連線池設定 (production readiness；見 config.py DB_* 環境變數) ---
        # pool_size / max_overflow：常駐連線數與尖峰可暫借的額外連線數。
        # pool_recycle：超過秒數的連線於下次借用前重建，避免被 LB / firewall
        # 靜默斷線後才發現失效。
        # command_timeout：asyncpg 連線層的單一指令逾時 (秒)，防止慢查詢
        # 長期佔住池內連線。
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_recycle=settings.db_pool_recycle,
        connect_args={"command_timeout": settings.db_command_timeout},
    )


def get_engine():
    """回傳 (必要時建立並快取) 全域 AsyncEngine。"""
    global _engine
    if _engine is None:
        _engine = _build_engine()
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """回傳 (必要時建立並快取) 全域 async_sessionmaker。

    expire_on_commit=False：commit 後物件仍可讀取屬性，便於 router 於回傳前
    序列化 ORM 物件。
    """
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = async_sessionmaker(
            bind=get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )
    return _SessionLocal


def __getattr__(name: str):
    """PEP 562 模組層級延遲屬性：``app.database.engine`` / ``.SessionLocal``。

    使既有 ``database.engine`` / ``database.SessionLocal`` 存取方式維持不變，
    但實際建立延後到「首次存取」(此時 DSN 已穩定)，避免 import 階段觸發
    asyncpg 匯入。注意：``from app.database import SessionLocal`` 亦會觸發此函式，
    因此「匯入時就 from-import」的模組 (deps / routers.auth) 已改為改用
    ``import app.database`` 並於呼叫時引用 ``database.SessionLocal``，以保持延遲。
    """
    if name == "engine":
        return get_engine()
    if name == "SessionLocal":
        return get_sessionmaker()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


async def set_tenant_guc(session: AsyncSession, tenant_id: str) -> None:
    """於目前交易內設定 RLS 租戶 GUC。

    is_local=true => 作用域限定於該交易，於連線池環境下安全
    (交易結束後自動失效，不會洩漏到下一個借用此連線的請求)。

    sqlite (dev 原生模式) 無 RLS / set_config，故為 no-op —— 多租戶隔離
    在 sqlite 下不生效 (僅供單機開發/示範)，生產仍以 PostgreSQL RLS 隔離。
    """
    if is_sqlite():
        return
    await session.execute(
        text("SELECT set_config('app.current_tenant', :t, true)"),
        {"t": tenant_id},
    )


async def get_db_for_tenant(tenant_id: str) -> AsyncGenerator[AsyncSession, None]:
    """為指定租戶開啟 AsyncSession 並設定 RLS GUC。

    可被 FastAPI 依賴 (get_db) 或內部服務 (例如報表/通知) 重用。
    成功則 commit；發生例外則 rollback。
    """
    async with get_sessionmaker()() as session:
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
    return get_sessionmaker()()


async def create_all() -> None:
    """以目前 engine 建立所有 ORM 表 (Base.metadata)。

    用於 sqlite / dev_bootstrap 模式 (PostgreSQL 正式環境的 schema 由
    db/init.sql 權威建立，不走此路徑)。

    透過 engine.begin() 取得連線後以 run_sync 執行 create_all；engine 本身
    已帶 schema_translate_map (sqlite 下將 erp_integration 映射為 None)，故
    create_all 產生的 DDL 會落在單一 sqlite 資料庫中。import 延後至此函式內，
    避免於模組載入時就觸發 ORM 匯入順序問題。
    """
    from app.models import orm  # noqa: F401  確保所有表都註冊到 Base.metadata

    async with get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
