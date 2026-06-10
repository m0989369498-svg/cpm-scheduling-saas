"""儀表板 + 匯出測試 (dashboard + xlsx/pdf exports) —— sqlite dev 模式 (不需 Postgres)。

本檔「不」標記 @pytest.mark.integration，故於開發機 / CI backend-tests 皆執行。

涵蓋契約 (Feature 1 — Dashboard + Exports)
------------------------------------------
1. GET /dashboard -> 200，回傳 {projects:[ProjectKpi], totals:{...}}。
   projects 內含種子專案 PRJ-2026-TW-001，其 (因已種基準線 + 進度) spi / cpi
   皆為非 None 數值，且 has_baseline=True；各 KPI 欄位齊備。
2. GET /projects/{pid}/export.xlsx -> 200，content-type 為 openpyxl xlsx，
   body 非空且以 ZIP magic "PK" 起始 (xlsx 即 zip 容器)。
3. GET /projects/{pid}/export.pdf -> 200，content-type application/pdf，
   body 以 "%PDF" magic 起始。

說明：
  * 以 admin@tw 登入 (Bearer)；dashboard / export 為唯讀 (viewer 亦可)，
    本檔僅驗證 admin 路徑與回應內容/二進位 magic。
  * sqlite rebind / teardown 與 test_auth.py / test_permissions.py 一致。
"""
from __future__ import annotations

import os
import tempfile

# --------------------------------------------------------------------------- #
# 於匯入 app 之前先設定環境變數 (env at top before importing app)。
# --------------------------------------------------------------------------- #
_DB_FD, _DB_PATH = tempfile.mkstemp(prefix="cpm_dash_test_", suffix=".db")
os.close(_DB_FD)
_DB_URL = "sqlite+aiosqlite:///" + _DB_PATH.replace("\\", "/")
os.environ["DATABASE_URL"] = _DB_URL
os.environ["DEV_BOOTSTRAP"] = "1"
os.environ.setdefault("AUTH_REQUIRED", "false")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402

PREFIX = settings.api_v1_prefix
LOGIN_URL = f"{PREFIX}/auth/login"
DASHBOARD_URL = f"{PREFIX}/dashboard"
PROJECTS_URL = f"{PREFIX}/projects"

DEMO_PASSWORD = "demo1234"
TENANT_TW = "TENT-9981"
SEED_PROJECT_TW = "PRJ-2026-TW-001"

# openpyxl 產生的 xlsx 正式 MIME 型別。
XLSX_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)


def _rebind_sqlite_engine() -> None:
    """把 app 的 DB 層強制指向本檔 sqlite 暫存檔 (與 test_auth.py 同法)。"""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.ext.asyncio import AsyncSession

    import app.database as database

    settings.database_url = _DB_URL
    settings.dev_bootstrap = True

    new_engine = create_async_engine(
        _DB_URL,
        echo=False,
        future=True,
        connect_args={"check_same_thread": False},
        execution_options={"schema_translate_map": {"erp_integration": None}},
    )
    new_sessionmaker = async_sessionmaker(
        bind=new_engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )

    database._engine = new_engine
    database._SessionLocal = new_sessionmaker
    database.engine = new_engine
    database.SessionLocal = new_sessionmaker

    import app.deps as deps
    import app.routers.auth as auth_router_mod

    deps.SessionLocal = new_sessionmaker
    auth_router_mod.SessionLocal = new_sessionmaker


def _dispose_sqlite_engines() -> None:
    """釋放所有綁到本檔 sqlite 暫存檔的 async engine (Windows 釋放檔案句柄)。"""
    import asyncio

    import app.database as database

    async def _dispose_all() -> None:
        engines = []
        try:
            engines.append(database.get_engine())
        except Exception:
            pass
        rebound = database.__dict__.get("engine")
        if rebound is not None and rebound not in engines:
            engines.append(rebound)
        for eng in engines:
            try:
                await eng.dispose()
            except Exception:
                pass

    try:
        asyncio.run(_dispose_all())
    except Exception:
        pass


def _unlink_with_retry(path: str, attempts: int = 10, delay: float = 0.05) -> None:
    """刪除暫存 sqlite 檔，對 Windows 句柄釋放延遲做短暫重試。"""
    import gc
    import time

    gc.collect()
    for _ in range(attempts):
        try:
            os.unlink(path)
            return
        except FileNotFoundError:
            return
        except OSError:
            time.sleep(delay)
    try:
        os.unlink(path)
    except OSError:
        pass


_MISSING = object()


def _snapshot_app_db_state() -> dict:
    """快照 _rebind_sqlite_engine()/lifespan 會變動的全域 DB 綁定。"""
    import app.database as database
    import app.deps as deps
    import app.routers.auth as auth_router_mod

    snap: dict = {
        "database_url": settings.database_url,
        "dev_bootstrap": settings.dev_bootstrap,
        "_engine": database.__dict__.get("_engine"),
        "_SessionLocal": database.__dict__.get("_SessionLocal"),
        "attrs": {},
    }
    for mod, name in (
        (database, "engine"),
        (database, "SessionLocal"),
        (deps, "SessionLocal"),
        (auth_router_mod, "SessionLocal"),
    ):
        snap["attrs"][(mod.__name__, name)] = mod.__dict__.get(name, _MISSING)
    return snap


def _restore_app_db_state(snap: dict) -> None:
    """還原 _snapshot_app_db_state() 取得的全域 DB 綁定 (避免污染後續測試)。"""
    import importlib

    import app.database as database

    settings.database_url = snap["database_url"]
    settings.dev_bootstrap = snap["dev_bootstrap"]
    database._engine = snap["_engine"]
    database._SessionLocal = snap["_SessionLocal"]
    for (modname, name), val in snap["attrs"].items():
        mod = importlib.import_module(modname)
        if val is _MISSING:
            mod.__dict__.pop(name, None)
        else:
            setattr(mod, name, val)


@pytest.fixture(scope="module")
def client():
    """module 範圍 TestClient；先把 DB 綁到 sqlite 暫存檔，再以 with 進入觸發 lifespan
    (create_all + 種核心資料 + 種 app users)。結束後 dispose、還原全域狀態、刪暫存檔。
    """
    snap = _snapshot_app_db_state()
    try:
        _rebind_sqlite_engine()
        with TestClient(app) as c:
            yield c
    finally:
        _dispose_sqlite_engines()
        _restore_app_db_state(snap)
        _unlink_with_retry(_DB_PATH)


@pytest.fixture(scope="module")
def admin_headers(client) -> dict:
    """以 admin@tw 登入並回傳 Bearer 標頭 (dashboard / export 唯讀皆走 Bearer)。"""
    resp = client.post(
        LOGIN_URL, json={"username": "admin@tw", "password": DEMO_PASSWORD}
    )
    assert resp.status_code == 200, resp.text
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------------- #
# (1) GET /dashboard
# --------------------------------------------------------------------------- #
def test_dashboard_shape_and_kpis(client, admin_headers):
    """GET /dashboard -> 200，含 projects 陣列與 totals；種子 TW 專案有 spi/cpi。"""
    resp = client.get(DASHBOARD_URL, headers=admin_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert isinstance(body, dict)
    assert "projects" in body and isinstance(body["projects"], list)
    assert "totals" in body and isinstance(body["totals"], dict)

    by_id = {p["project_id"]: p for p in body["projects"]}
    assert SEED_PROJECT_TW in by_id, f"dashboard 應含種子專案: {list(by_id)}"

    kpi = by_id[SEED_PROJECT_TW]
    # ProjectKpi 契約欄位齊備。
    for field in (
        "project_id",
        "project_name",
        "region",
        "task_count",
        "project_duration",
        "critical_count",
        "has_baseline",
        "spi",
        "cpi",
        "pending_risk_events",
    ):
        assert field in kpi, f"ProjectKpi 缺少欄位 {field}: {kpi}"

    # 種子 PRJ-2026-TW-001 已種基準線 + 進度 -> 應可算出 SPI / CPI。
    assert kpi["has_baseline"] is True
    assert kpi["spi"] is not None, f"已種基準線，spi 不應為 None: {kpi}"
    assert kpi["cpi"] is not None, f"已種基準線，cpi 不應為 None: {kpi}"
    assert isinstance(kpi["spi"], (int, float))
    assert isinstance(kpi["cpi"], (int, float))

    # 任務數與要徑數為合理整數 (三任務全要徑)。
    assert kpi["task_count"] == 3
    assert kpi["critical_count"] >= 1
    assert kpi["region"] == "TW"


def test_dashboard_scoped_to_tenant(client, admin_headers):
    """dashboard 僅含當前租戶 (TENT-9981) 之專案，不外洩 CN 專案。"""
    resp = client.get(DASHBOARD_URL, headers=admin_headers)
    assert resp.status_code == 200, resp.text
    project_ids = {p["project_id"] for p in resp.json()["projects"]}
    assert SEED_PROJECT_TW in project_ids
    # CN 租戶的種子專案不應出現在 TW 租戶的儀表板。
    assert "PRJ-2026-CN-001" not in project_ids


# --------------------------------------------------------------------------- #
# (2) GET /projects/{pid}/export.xlsx
# --------------------------------------------------------------------------- #
def test_export_xlsx(client, admin_headers):
    """export.xlsx -> 200，content-type 為 xlsx，body 非空且以 ZIP magic 'PK' 起始。"""
    resp = client.get(
        f"{PROJECTS_URL}/{SEED_PROJECT_TW}/export.xlsx", headers=admin_headers
    )
    assert resp.status_code == 200, resp.text

    content_type = resp.headers.get("content-type", "")
    assert XLSX_MEDIA_TYPE in content_type, f"非預期 content-type: {content_type}"

    body = resp.content
    assert body, "xlsx body 不應為空"
    # xlsx 為 zip 容器，必以本地檔案標頭 magic "PK\x03\x04" 起始。
    assert body[:2] == b"PK", f"xlsx body 應以 ZIP magic PK 起始: {body[:8]!r}"


# --------------------------------------------------------------------------- #
# (3) GET /projects/{pid}/export.pdf
# --------------------------------------------------------------------------- #
def test_export_pdf(client, admin_headers):
    """export.pdf -> 200，content-type application/pdf，body 以 '%PDF' magic 起始。"""
    resp = client.get(
        f"{PROJECTS_URL}/{SEED_PROJECT_TW}/export.pdf", headers=admin_headers
    )
    assert resp.status_code == 200, resp.text

    content_type = resp.headers.get("content-type", "")
    assert "application/pdf" in content_type, f"非預期 content-type: {content_type}"

    body = resp.content
    assert body, "pdf body 不應為空"
    assert body[:4] == b"%PDF", f"pdf body 應以 %PDF magic 起始: {body[:8]!r}"
