"""Schema 遷移入口 — ``python -m app.migrate`` (容器啟動時於主程序前執行)。

行為 (冪等，可重複執行)：
  1. sqlite (本機 dev 原生模式)：完全跳過 —— dev 的 schema 由 app 啟動時的
     ``create_all`` bootstrap 建立 (見 app.main lifespan)，不需 Alembic。
  2. PostgreSQL：
     - 「既有 DB」(已有 projects 等核心表) 且尚無 ``alembic_version``
       -> ``alembic stamp head``：把現況認定為 0001 baseline，不執行任何 DDL。
       (涵蓋：compose 由 db/init.sql 佈建、或 Batch 2 之前的舊部署)
     - 其餘情況 -> ``alembic upgrade head``：
       * 全新空 DB：0001_baseline 以 Base.metadata.create_all 建出完整 schema。
       * 已有 alembic_version：套用尚未執行的 revision (已在 head 則 no-op)。

權限注意 (PG15+)：
  應用角色 ``cpm_app`` 刻意「無 DDL 權限」(public schema 預設不開放 CREATE)，
  因此以 cpm_app 對 init.sql 佈建的既有 DB 執行 stamp 可能因權限不足而失敗。
  此情況「不視為致命」：schema 已存在、應用可正常啟動，僅記 warning 後以 0
  結束；請 ops 以特權帳號 (環境變數 ``MIGRATE_DATABASE_URL`` 指定 DSN，
  例如 DB owner ``cpm``) 補執行一次 ``python -m app.migrate`` 落地 stamp。
  反之，「全新空 DB」建置失敗則為致命錯誤 (exit 1) —— 應用無 schema 不可啟動。

RLS / 角色 / GRANT 仍由 db/init.sql 權威管理 (見 0001_baseline docstring)。
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("app.migrate")

# backend/ 目錄 (alembic.ini 與 migrations/ 所在處；容器內為 /app)
_BACKEND_DIR = Path(__file__).resolve().parents[1]


def _effective_url() -> str:
    """解析遷移用連線 URL (與 migrations/env.py 同一優先序)。"""
    from app.config import settings

    return (
        os.getenv("MIGRATE_DATABASE_URL")
        or settings.database_url
        or os.getenv("DATABASE_URL", "")
    )


def _is_sqlite_url(url: str) -> bool:
    return (url or "").lower().startswith("sqlite")


async def _inspect_state(url: str) -> tuple[bool, bool]:
    """回傳 ``(has_alembic_version, has_core_tables)``。

    以短暫的獨立 engine 連線檢查後立即 dispose (不污染應用連線池)。
    以 ``projects`` 表代表「核心 schema 已存在」(init.sql / 舊版 create_all
    皆會建立)。
    """
    from sqlalchemy import inspect
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(url, pool_pre_ping=True, future=True)
    try:
        async with engine.connect() as conn:

            def _check(sync_conn) -> tuple[bool, bool]:
                insp = inspect(sync_conn)
                return (
                    insp.has_table("alembic_version"),
                    insp.has_table("projects"),
                )

            return await conn.run_sync(_check)
    finally:
        await engine.dispose()


async def _inspect_with_retry(
    url: str, attempts: int = 5, delay_seconds: float = 2.0
) -> tuple[bool, bool]:
    """連線檢查 + 輕量重試 (compose 雖有 depends_on healthy，仍保留保險)。"""
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await _inspect_state(url)
        except Exception as exc:  # noqa: BLE001 - 連線期錯誤統一重試
            last_exc = exc
            logger.warning(
                "遷移前資料庫檢查失敗 (attempt %d/%d)：%s", attempt, attempts, exc
            )
            if attempt < attempts:
                await asyncio.sleep(delay_seconds)
    assert last_exc is not None
    raise last_exc


def _alembic_config():
    """以絕對路徑建立 alembic Config (不依賴 cwd)。"""
    from alembic.config import Config

    cfg = Config(str(_BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(_BACKEND_DIR / "migrations"))
    return cfg


def main() -> int:
    url = _effective_url()

    if _is_sqlite_url(url):
        # dev 原生模式：schema 由 startup create_all 建立，Alembic 完全跳過
        logger.info("偵測到 sqlite DATABASE_URL —— 跳過 Alembic (dev 由 create_all 建表)。")
        return 0

    try:
        has_version, has_core = asyncio.run(_inspect_with_retry(url))
    except Exception:  # noqa: BLE001
        logger.exception("無法連線資料庫進行遷移前檢查，中止啟動")
        return 1

    from alembic import command

    cfg = _alembic_config()

    if not has_version and has_core:
        # 既有部署 (init.sql / 舊版 create_all 建表)：認定現況即 baseline。
        logger.info("偵測到既有 schema 且無 alembic_version —— 執行 alembic stamp head")
        try:
            command.stamp(cfg, "head")
            logger.info("alembic stamp head 完成")
        except Exception as exc:  # noqa: BLE001
            # 典型情況：以 cpm_app (無 DDL 權限) 連線，無法建立 alembic_version 表。
            # schema 已存在，應用仍可正常啟動 —— 不阻斷，請 ops 以特權 DSN
            # (MIGRATE_DATABASE_URL) 補執行 stamp。
            logger.warning(
                "alembic stamp head 失敗 (schema 已存在，應用照常啟動；"
                "請以特權帳號設定 MIGRATE_DATABASE_URL 後重跑 python -m app.migrate)：%s",
                exc,
            )
        return 0

    logger.info(
        "執行 alembic upgrade head (alembic_version=%s, core_tables=%s)",
        has_version,
        has_core,
    )
    try:
        command.upgrade(cfg, "head")
    except Exception as exc:  # noqa: BLE001
        if has_core:
            # schema 已存在 (例如已 stamp 過、但後續 revision 需要 DDL 權限)：
            # 不阻斷既有部署啟動，僅告警 —— 待 ops 以特權 DSN 套用遷移。
            logger.warning(
                "alembic upgrade head 失敗，但核心 schema 已存在，應用照常啟動 "
                "(請以特權帳號 MIGRATE_DATABASE_URL 套用遷移)：%s",
                exc,
            )
            return 0
        # 全新空 DB 建置失敗 = 致命：應用無 schema 不可啟動
        logger.exception("全新資料庫的 alembic upgrade head 失敗，中止啟動")
        return 1

    logger.info("alembic upgrade head 完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
