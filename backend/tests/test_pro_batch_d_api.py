"""Pro Batch D API 測試 (sqlite TestClient pattern) —— 不需 Postgres。

涵蓋:
  FEATURE D1 — GET /projects/{pid}/cost            成本負載 (唯讀)
  FEATURE D2 — GET /projects/{pid}/health           DCMA 14-point 排程健康評估 (唯讀)
  FEATURE D3 — GET/PUT /projects/{pid}/resources    資源日曆 (calendars) 讀寫往返;
               POST /projects/{pid}/level           availability 生效時 (有日曆 +
               start_date) 撫平結果與純量上限情境不同。

關鍵設計 (與 test_wbs_baselines.py 完全一致)：
1. 「import app 之前」即設定 DATABASE_URL=sqlite 檔案 + DEV_BOOTSTRAP=1。
2. client fixture 以 _rebind_sqlite_engine() 將整個 DB 層重綁至本檔 sqlite 檔，
   teardown 時 dispose engine、還原全域綁定、再刪暫存檔 (Windows 句柄安全)。
"""
from __future__ import annotations

import os
import tempfile

_DB_FD, _DB_PATH = tempfile.mkstemp(prefix="cpm_batch_d_test_", suffix=".db")
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
PROJECTS_URL = f"{PREFIX}/projects"

DEMO_PASSWORD = "demo1234"


# --------------------------------------------------------------------------- #
# sqlite rebind / teardown 工具 (與 test_wbs_baselines.py 同法)
# --------------------------------------------------------------------------- #
def _rebind_sqlite_engine() -> None:
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
    snap = _snapshot_app_db_state()
    try:
        _rebind_sqlite_engine()
        with TestClient(app) as c:
            yield c
    finally:
        _dispose_sqlite_engines()
        _restore_app_db_state(snap)
        _unlink_with_retry(_DB_PATH)


# --------------------------------------------------------------------------- #
# 共用小工具
# --------------------------------------------------------------------------- #
def _login(client: TestClient, username: str, password: str) -> dict:
    resp = client.post(LOGIN_URL, json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _admin_headers(client: TestClient) -> dict[str, str]:
    token = _login(client, "admin@tw", DEMO_PASSWORD)["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _create_project(
    client: TestClient,
    headers: dict[str, str],
    project_id: str,
    *,
    schedule_data: list[dict],
    **extra,
) -> dict:
    payload = {
        "project_id": project_id,
        "project_name": f"Pro Batch D 測試專案 {project_id}",
        "region": "TW",
        "schedule_data": schedule_data,
    }
    payload.update(extra)
    resp = client.post(PROJECTS_URL, headers=headers, json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _two_task_chain() -> list[dict]:
    # 注意：ProjectCreate.schedule_data (TaskDefinition) 不含 resource_demands
    # 欄位 (該欄位僅存在於 TaskCreate/TaskUpdate)；建立專案後須另以
    # PUT /resources 的 demands 寫入 tasks.resource_demands。
    return [
        {"task_id": "T-01", "task_name": "開挖", "duration": 5,
         "predecessors": [], "status": "PENDING"},
        {"task_id": "T-02", "task_name": "結構", "duration": 3,
         "predecessors": ["T-01"], "status": "PENDING"},
    ]


# =============================================================================
# FEATURE D1 — GET /projects/{pid}/cost
# =============================================================================
def test_cost_endpoint_computes_total_and_rollups(client):
    headers = _admin_headers(client)
    pid = "PRJ-BATCHD-COST"
    _create_project(client, headers, pid, schedule_data=_two_task_chain())

    # 設定費率 (unit_cost) + 類別 + 各任務資源需求 (demands)
    resources_payload = {
        "limits": [
            {"resource_type": "crane", "max_capacity": 2, "unit_cost": 3000.0,
             "category": "equipment"},
            {"resource_type": "manpower", "max_capacity": 20, "unit_cost": 250.0,
             "category": "labor"},
        ],
        "demands": {
            "T-01": {"crane": 1, "manpower": 10},
            "T-02": {"manpower": 8},
        },
    }
    put_resp = client.put(
        f"{PROJECTS_URL}/{pid}/resources", headers=headers, json=resources_payload
    )
    assert put_resp.status_code == 200, put_resp.text

    resp = client.get(f"{PROJECTS_URL}/{pid}/cost", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # T-01: dur5 * (1*3000 + 10*250) = 5*5500 = 27500
    # T-02: dur3 * (8*250) = 3*2000 = 6000
    assert body["total_cost"] == 27500.0 + 6000.0
    assert body["by_category"]["equipment"] == 15000.0
    assert body["by_category"]["labor"] == 12500.0 + 6000.0
    assert len(body["per_task"]) == 2
    assert len(body["cost_curve"]) == body["cost_curve"][-1]["day"] + 1


def test_cost_endpoint_readonly_no_role_required(client):
    """唯讀端點：viewer 亦可讀取 (不需 require_role)。"""
    headers = _admin_headers(client)
    pid = "PRJ-BATCHD-COST-RO"
    _create_project(client, headers, pid, schedule_data=[])

    resp = client.get(f"{PROJECTS_URL}/{pid}/cost", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["total_cost"] == 0.0


# =============================================================================
# FEATURE D2 — GET /projects/{pid}/health
# =============================================================================
def test_health_endpoint_returns_fourteen_checks(client):
    headers = _admin_headers(client)
    pid = "PRJ-BATCHD-HEALTH"
    _create_project(client, headers, pid, schedule_data=_two_task_chain())

    resp = client.get(f"{PROJECTS_URL}/{pid}/health", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["total_count"] == 14
    assert len(body["checks"]) == 14
    keys = {c["key"] for c in body["checks"]}
    assert keys == {
        "logic", "leads", "lags", "relationship_types", "hard_constraints",
        "high_float", "negative_float", "high_duration", "invalid_dates",
        "resources", "missed_tasks", "critical_path_test", "cpli", "bei",
    }
    # missed_tasks / bei 需要基準線；本專案尚無基準線 -> 應為資訊性 (None)。
    by_key = {c["key"]: c for c in body["checks"]}
    assert by_key["missed_tasks"]["passed"] is None
    assert by_key["bei"]["passed"] is None


def test_health_endpoint_accepts_data_date_query(client):
    headers = _admin_headers(client)
    pid = "PRJ-BATCHD-HEALTH-DD"
    _create_project(client, headers, pid, schedule_data=_two_task_chain())

    resp = client.get(
        f"{PROJECTS_URL}/{pid}/health", headers=headers, params={"data_date": 3}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data_date"] == 3


# =============================================================================
# FEATURE D3 — 資源日曆 round-trip + level 端點 availability
# =============================================================================
def test_resource_calendars_round_trip(client):
    headers = _admin_headers(client)
    pid = "PRJ-BATCHD-CAL"
    _create_project(client, headers, pid, schedule_data=[])

    payload = {
        "limits": [
            {"resource_type": "crane", "max_capacity": 2,
             "unit_cost": 3000.0, "category": "equipment"},
        ],
        "demands": {},
        "calendars": [{"resource_type": "crane", "work_days": "1111100"}],
    }
    put_resp = client.put(
        f"{PROJECTS_URL}/{pid}/resources", headers=headers, json=payload
    )
    assert put_resp.status_code == 200, put_resp.text
    assert put_resp.json()["calendars"] == [
        {"resource_type": "crane", "work_days": "1111100"}
    ]

    got = client.get(f"{PROJECTS_URL}/{pid}/resources", headers=headers)
    assert got.status_code == 200, got.text
    assert got.json()["calendars"] == [
        {"resource_type": "crane", "work_days": "1111100"}
    ]
    # ResourceLimit 的費率欄位 (unit_cost/category) 亦須完整往返 ——
    # 不可只靠 /cost 端點間接驗證 (該路由自行查 ProjectResourceLimit，
    # 不經 _build_resource_config)。
    assert got.json()["limits"] == [
        {"resource_type": "crane", "max_capacity": 2,
         "unit_cost": 3000.0, "category": "equipment"}
    ]


def test_resource_limits_rate_fields_round_trip_non_default(client):
    """非預設 unit_cost/category 經 PUT 後，GET /resources 必須原值回傳。"""
    headers = _admin_headers(client)
    pid = "PRJ-BATCHD-RATES"
    _create_project(client, headers, pid, schedule_data=[])

    payload = {
        "limits": [
            {"resource_type": "concrete", "max_capacity": 50,
             "unit_cost": 123.45, "category": "material"},
            {"resource_type": "manpower", "max_capacity": 20,
             "unit_cost": 0.0, "category": "labor"},
        ],
        "demands": {},
    }
    put_resp = client.put(
        f"{PROJECTS_URL}/{pid}/resources", headers=headers, json=payload
    )
    assert put_resp.status_code == 200, put_resp.text

    got = client.get(f"{PROJECTS_URL}/{pid}/resources", headers=headers)
    assert got.status_code == 200, got.text
    limits = {lim["resource_type"]: lim for lim in got.json()["limits"]}
    assert limits["concrete"]["max_capacity"] == 50
    assert limits["concrete"]["unit_cost"] == 123.45
    assert limits["concrete"]["category"] == "material"
    # 刻意設定 unit_cost=0 (例如自有免費資源) 亦須如實往返、不得被改寫。
    assert limits["manpower"]["unit_cost"] == 0.0
    assert limits["manpower"]["category"] == "labor"


def test_level_endpoint_still_works_without_calendars_or_start_date(client):
    """無日曆 / 無 start_date -> availability=None，行為與批次前一致 (仍可正常撫平)。"""
    headers = _admin_headers(client)
    pid = "PRJ-BATCHD-LEVEL-NOCAL"
    _create_project(client, headers, pid, schedule_data=_two_task_chain())

    resp = client.post(f"{PROJECTS_URL}/{pid}/level", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["original_duration"] == 8  # T-01(5) -> T-02(3)
