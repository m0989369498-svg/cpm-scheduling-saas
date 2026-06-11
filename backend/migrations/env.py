"""Alembic 遷移環境 (async engine 版本)。

設計：
- 使用 SQLAlchemy async engine (asyncpg / aiosqlite) + ``connection.run_sync``
  執行遷移 —— 與應用程式 (app.database) 共用同一套 async driver，映像檔內
  不需額外安裝同步 driver (psycopg2)。
- 連線 URL 優先序：
    1. 環境變數 MIGRATE_DATABASE_URL (選填；供 ops 以「具 DDL 權限」的
       特權帳號執行遷移 —— 應用角色 cpm_app 刻意無 DDL 權限)
    2. app.config.settings.database_url (其本身已讀取環境變數 DATABASE_URL)
    3. 環境變數 DATABASE_URL (保險回退)
- target_metadata = app.database.Base.metadata；匯入 app.models.orm 以確保
  所有資料表都註冊進 metadata (供 0001_baseline 的 create_all 與未來的
  autogenerate 使用)。
"""
from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

# --- app 匯入：settings (DSN) + Base.metadata (遷移目標) ----------------------
# 注意：alembic CLI 需於 backend/ 目錄下執行 (alembic.ini prepend_sys_path = .)
# 才能 import app.*；python -m app.migrate 走 command API 於同一行程內，天然可匯入。
from app.config import settings  # noqa: E402
from app.database import Base  # noqa: E402
from app.models import orm  # noqa: F401,E402  確保所有 ORM 表註冊到 Base.metadata

# Alembic Config 物件 (對應 alembic.ini)
config = context.config

# Logging 設定 (alembic.ini 的 loggers/handlers/formatters 區塊)
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    """解析遷移用連線 URL (優先序見模組 docstring)。"""
    return (
        os.getenv("MIGRATE_DATABASE_URL")
        or settings.database_url
        or os.getenv("DATABASE_URL", "")
    )


def run_migrations_offline() -> None:
    """Offline 模式：不建立連線，輸出 SQL 腳本 (alembic upgrade --sql)。"""
    url = _database_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """於 (sync 化的) 連線上設定 context 並執行遷移。"""
    context.configure(connection=connection, target_metadata=target_metadata)

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Online 模式：以 async engine 連線，透過 run_sync 執行同步遷移邏輯。"""
    url = _database_url()
    section = config.get_section(config.config_ini_section, {}) or {}
    section["sqlalchemy.url"] = url

    engine_kwargs: dict = {"poolclass": pool.NullPool}
    if url.lower().startswith("sqlite"):
        # sqlite 無 schema 概念：與 app.database._build_engine 相同，把 ORM 的
        # "erp_integration" schema 映射為 None (single-file dev DB 防呆；
        # 正常情況 app.migrate 在 sqlite 下會整個跳過)。
        engine_kwargs["execution_options"] = {
            "schema_translate_map": {"erp_integration": None}
        }

    connectable = async_engine_from_config(
        section,
        prefix="sqlalchemy.",
        **engine_kwargs,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Online 模式進入點 (alembic 由 sync context 呼叫，故以 asyncio.run 包裝)。"""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
