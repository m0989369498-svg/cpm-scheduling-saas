"""pytest 共用設定 (fixtures + collection hook) for the DB 整合測試。

設計目標
--------
1. 整合測試 (``@pytest.mark.integration``) 需要「真實」PostgreSQL。
   只有在同時滿足下列條件時才執行，否則一律「乾淨跳過」(skip)，
   讓開發機上的既有無狀態單元套件 (test_api.py / test_cpm_engine.py) 仍 20/20 通過：

       環境變數 RUN_DB_TESTS == "1"
       且 settings.database_url 以 "postgresql" 開頭 (asyncpg DSN)

   在開發機上 DATABASE_URL 通常為 sqlite (例如 sqlite+aiosqlite:///:memory:)，
   或未設定 RUN_DB_TESTS，因此整合測試自動跳過。

2. 本模組「在 sqlite 之下也必須可被匯入」。
   - 只匯入安全模組 (app.config / app.main / fastapi.testclient)，
     這些在 sqlite DSN 下匯入皆正常。
   - 「絕不」直接 import asyncpg；是否有真實 DB 完全由 app + DATABASE_URL 決定。
   - 需要真實 DB 的 worker / ORM 查詢相關匯入，延後到整合測試函式「執行時」
     才於函式內部 import，避免開發機 (無 asyncpg / 無 apscheduler) 連匯入都失敗。

3. 提供共用工具：
   - ``client``               : session 範圍的 FastAPI TestClient (走真實 get_db + RLS GUC)。
   - ``make_project_payload`` : 組裝 ProjectCreate 請求 body 的小工具。
   - ``run_async``            : 以 asyncio.run 執行協程 (worker.scan_once / DB 查詢) 的小工具。
"""
from __future__ import annotations

import asyncio
import os
from collections.abc import Coroutine
from typing import Any

import pytest
from fastapi.testclient import TestClient

# 僅匯入「在 sqlite 之下也安全」的應用物件。
#   - app.config：純設定，無 DB driver 依賴。
#   - app.main  ：建立 FastAPI app；在 sqlite DSN 下匯入正常 (不會觸發 asyncpg)。
from app.config import settings
from app.main import app


# --------------------------------------------------------------------------- #
# Collection hook：依環境決定是否跳過 integration 測試
# --------------------------------------------------------------------------- #
def _real_db_enabled() -> bool:
    """是否已備妥「真實 PostgreSQL」以執行整合測試。

    需同時滿足：
      RUN_DB_TESTS == "1"  且  DATABASE_URL 以 "postgresql" 開頭。
    任一不滿足即視為無真實 DB (例如 sqlite 開發機)，整合測試將被跳過。
    """
    if os.getenv("RUN_DB_TESTS") != "1":
        return False
    db_url = (settings.database_url or "").strip().lower()
    return db_url.startswith("postgresql")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """為所有 integration 測試項目掛上 skip 標記 (除非已備妥真實 DB)。

    這讓整合測試與既有單元測試共存：開發機 (sqlite / 未設 RUN_DB_TESTS) 自動跳過，
    CI (postgresql + RUN_DB_TESTS=1) 則正常執行。
    """
    if _real_db_enabled():
        return

    skip_integration = pytest.mark.skip(
        reason=(
            "需要真實 PostgreSQL：請設定 RUN_DB_TESTS=1 且 "
            "DATABASE_URL=postgresql+asyncpg://cpm_app:... 後再執行整合測試。"
        )
    )
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip_integration)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="session")
def client() -> TestClient:
    """session 範圍的 FastAPI TestClient。

    透過 TestClient 驅動可完整行經真實依賴鏈：
        verify_tenant -> get_db (開啟 AsyncSession + 設定 app.current_tenant RLS GUC)
        -> ORM -> PostgreSQL。
    因此這些整合測試會真正驗證 RLS 隔離與 DB 持久化行為。
    """
    with TestClient(app) as c:
        yield c


# --------------------------------------------------------------------------- #
# Helpers (供測試直接 import 使用)
# --------------------------------------------------------------------------- #
def make_project_payload(
    project_id: str,
    *,
    project_name: str = "整合測試專案",
    region: str = "TW",
    schedule_data: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """組裝 POST /api/v1/projects 的 ProjectCreate 請求 body。

    schedule_data 省略時，預設給一條線性鏈 T-01(5) -> T-02(3) -> T-03(2)，
    對應種子資料的工期關係 (專案總工期 10，三任務全要徑)。
    """
    if schedule_data is None:
        schedule_data = [
            {"task_id": "T-01", "task_name": "基地開挖", "duration": 5,
             "predecessors": [], "status": "PENDING"},
            {"task_id": "T-02", "task_name": "一樓鋼筋綁紮", "duration": 3,
             "predecessors": ["T-01"], "status": "PENDING"},
            {"task_id": "T-03", "task_name": "一樓混凝土澆置", "duration": 2,
             "predecessors": ["T-02"], "status": "PENDING"},
        ]
    return {
        "project_id": project_id,
        "project_name": project_name,
        "region": region,
        "schedule_data": schedule_data,
    }


def run_async(coro: Coroutine[Any, Any, Any]) -> Any:
    """以 asyncio.run 執行協程並回傳結果。

    供整合測試呼叫 worker.scan_once() 或直接以 SessionLocal 查 DB 之用。
    每次呼叫使用獨立的 event loop (asyncio.run 自行建立/關閉)，與 TestClient
    內部事件迴圈互不干擾。
    """
    return asyncio.run(coro)
