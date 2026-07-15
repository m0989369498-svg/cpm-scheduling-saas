"""企業級 (enterprise / tenant-level) 資源 API 測試 (sqlite TestClient pattern)。

Pro Batch E (FEATURE E1)：
  GET/PUT /resources/pool         租戶層級資源池 round-trip。
  GET     /resources/allocation   投資組合資源分配 (跨專案週別峰值需求 vs 產能)。

關鍵設計 (與 test_pro_batch_d_api.py 完全一致)：
  1. 「import app 之前」即設定 DATABASE_URL=sqlite 檔案 + DEV_BOOTSTRAP=1。
  2. client fixture 以 _rebind_sqlite_engine() 將整個 DB 層重綁至本檔 sqlite 檔，
     teardown 時 dispose engine、還原全域綁定、再刪暫存檔 (Windows 句柄安全)。
"""
from __future__ import annotations

import os
import tempfile

_DB_FD, _DB_PATH = tempfile.mkstemp(prefix="cpm_enterprise_test_", suffix=".db")
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
POOL_URL = f"{PREFIX}/resources/pool"
ALLOCATION_URL = f"{PREFIX}/resources/allocation"

DEMO_PASSWORD = "demo1234"


# --------------------------------------------------------------------------- #
# sqlite rebind / teardown 工具 (與 test_pro_batch_d_api.py 同法)
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


def _headers_for(client: TestClient, username: str) -> dict[str, str]:
    token = _login(client, username, DEMO_PASSWORD)["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _admin_headers(client: TestClient) -> dict[str, str]:
    return _headers_for(client, "admin@tw")


def _editor_headers(client: TestClient) -> dict[str, str]:
    return _headers_for(client, "editor@tw")


def _viewer_headers(client: TestClient) -> dict[str, str]:
    return _headers_for(client, "viewer@tw")


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
        "project_name": f"Pro Batch E 測試專案 {project_id}",
        "region": "TW",
        "schedule_data": schedule_data,
    }
    payload.update(extra)
    resp = client.post(PROJECTS_URL, headers=headers, json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


# =============================================================================
# GET/PUT /resources/pool
# =============================================================================
def test_pool_round_trip_all_fields(client):
    headers = _admin_headers(client)
    payload = [
        {
            "resource_type": "crane", "name": "吊車", "category": "equipment",
            "capacity": 3, "unit_cost": 3200.0, "work_days": "1111100",
        },
        {
            "resource_type": "manpower", "name": "人力", "category": "labor",
            "capacity": 50, "unit_cost": 260.0, "work_days": "1111110",
        },
    ]
    put_resp = client.put(POOL_URL, headers=headers, json=payload)
    assert put_resp.status_code == 200, put_resp.text

    got = client.get(POOL_URL, headers=headers)
    assert got.status_code == 200, got.text
    by_type = {r["resource_type"]: r for r in got.json()}
    assert by_type["crane"]["name"] == "吊車"
    assert by_type["crane"]["category"] == "equipment"
    assert by_type["crane"]["capacity"] == 3
    assert by_type["crane"]["unit_cost"] == 3200.0
    assert by_type["crane"]["work_days"] == "1111100"
    assert by_type["manpower"]["capacity"] == 50


def test_pool_put_preserves_unlisted_existing_rows(client):
    headers = _admin_headers(client)
    first = [{"resource_type": "welder", "capacity": 4, "unit_cost": 500.0}]
    r1 = client.put(POOL_URL, headers=headers, json=first)
    assert r1.status_code == 200, r1.text

    second = [{"resource_type": "concrete_pump", "capacity": 1, "unit_cost": 5000.0}]
    r2 = client.put(POOL_URL, headers=headers, json=second)
    assert r2.status_code == 200, r2.text

    got = client.get(POOL_URL, headers=headers)
    types = {r["resource_type"] for r in got.json()}
    assert "welder" in types  # 先前寫入的列未被清空 (payload 未列出 -> 保留)。
    assert "concrete_pump" in types


def test_pool_put_requires_editor_role_403_for_viewer(client):
    headers = _viewer_headers(client)
    resp = client.put(
        POOL_URL, headers=headers,
        json=[{"resource_type": "crane", "capacity": 1, "unit_cost": 100.0}],
    )
    assert resp.status_code == 403, resp.text


def test_pool_get_readonly_viewer_allowed(client):
    headers = _viewer_headers(client)
    resp = client.get(POOL_URL, headers=headers)
    assert resp.status_code == 200, resp.text


def test_pool_invalid_work_days_422(client):
    headers = _admin_headers(client)
    resp = client.put(
        POOL_URL, headers=headers,
        json=[{"resource_type": "crane", "capacity": 1, "unit_cost": 100.0,
               "work_days": "bad"}],
    )
    assert resp.status_code == 422, resp.text


# =============================================================================
# GET /resources/allocation
# =============================================================================
def test_allocation_returns_weeks_rows_and_peak(client):
    headers = _admin_headers(client)

    pool_resp = client.put(
        POOL_URL, headers=headers,
        json=[{"resource_type": "crane", "capacity": 1, "unit_cost": 3000.0,
               "category": "equipment"}],
    )
    assert pool_resp.status_code == 200, pool_resp.text

    pid = "PRJ-ENTERPRISE-ALLOC"
    _create_project(
        client, headers, pid,
        start_date="2026-07-06",
        work_days="1111100",
        schedule_data=[
            {"task_id": "T-01", "task_name": "A", "duration": 2,
             "predecessors": [], "status": "PENDING"},
        ],
    )
    # T-01 duration=2 -> es=0 ef=2；賦與資源需求。
    put_resources = client.put(
        f"{PROJECTS_URL}/{pid}/resources", headers=headers,
        json={
            "limits": [],
            "demands": {"T-01": {"crane": 2}},
        },
    )
    assert put_resources.status_code == 200, put_resources.text

    resp = client.get(ALLOCATION_URL, headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert "2026-W28" in body["weeks"]
    crane_row = next(r for r in body["resources"] if r["resource_type"] == "crane")
    assert crane_row["capacity"] == 1
    assert crane_row["peak"] == 2  # 兩天內每天需求 2
    assert "2026-W28" in crane_row["over_weeks"]  # 2 > capacity(1)


def test_allocation_readonly_viewer_allowed(client):
    headers = _viewer_headers(client)
    resp = client.get(ALLOCATION_URL, headers=headers)
    assert resp.status_code == 200, resp.text


def test_allocation_unscheduled_project_flagged(client):
    headers = _admin_headers(client)
    pid = "PRJ-ENTERPRISE-UNSCHED"
    _create_project(
        client, headers, pid,
        schedule_data=[
            {"task_id": "T-01", "task_name": "A", "duration": 3,
             "predecessors": [], "status": "PENDING"},
        ],
    )
    put_resources = client.put(
        f"{PROJECTS_URL}/{pid}/resources", headers=headers,
        json={"limits": [], "demands": {"T-01": {"manpower": 5}}},
    )
    assert put_resources.status_code == 200, put_resources.text

    resp = client.get(ALLOCATION_URL, headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert pid in body["unscheduled_projects"]


# =============================================================================
# 租戶隔離 (tenant isolation)：TENT-9981 (admin@tw) vs TENT-CN-002 (admin@cn)
# =============================================================================
def _cn_admin_headers(client: TestClient) -> dict[str, str]:
    return _headers_for(client, "admin@cn")


def test_pool_tenant_isolation_cross_tenant_rows_invisible(client):
    """A 租戶寫入的資源池列，B 租戶的 GET /resources/pool 絕不可見（反向亦然）。"""
    tw_headers = _admin_headers(client)
    cn_headers = _cn_admin_headers(client)

    r_tw = client.put(
        POOL_URL, headers=tw_headers,
        json=[{"resource_type": "tw_only_tower_crane", "capacity": 2,
               "unit_cost": 8800.0, "category": "equipment"}],
    )
    assert r_tw.status_code == 200, r_tw.text
    r_cn = client.put(
        POOL_URL, headers=cn_headers,
        json=[{"resource_type": "cn_only_pile_driver", "capacity": 1,
               "unit_cost": 6600.0, "category": "equipment"}],
    )
    assert r_cn.status_code == 200, r_cn.text

    tw_types = {r["resource_type"] for r in client.get(POOL_URL, headers=tw_headers).json()}
    cn_types = {r["resource_type"] for r in client.get(POOL_URL, headers=cn_headers).json()}
    assert "tw_only_tower_crane" in tw_types
    assert "cn_only_pile_driver" in cn_types
    assert "cn_only_pile_driver" not in tw_types  # 跨租戶不可見。
    assert "tw_only_tower_crane" not in cn_types  # 跨租戶不可見。


def test_allocation_tenant_isolation_cross_tenant_demand_invisible(client):
    """A 租戶專案的資源需求，B 租戶的 GET /resources/allocation 絕不可見。"""
    tw_headers = _admin_headers(client)
    cn_headers = _cn_admin_headers(client)

    pid = "PRJ-ENTERPRISE-ISO-TW"
    _create_project(
        client, tw_headers, pid,
        start_date="2026-07-06",
        work_days="1111100",
        schedule_data=[
            {"task_id": "T-01", "task_name": "隔離測試工項", "duration": 2,
             "predecessors": [], "status": "PENDING"},
        ],
    )
    put_resources = client.put(
        f"{PROJECTS_URL}/{pid}/resources", headers=tw_headers,
        json={"limits": [], "demands": {"T-01": {"tw_iso_only_resource": 9}}},
    )
    assert put_resources.status_code == 200, put_resources.text

    # TW 租戶看得到自己的需求列。
    tw_body = client.get(ALLOCATION_URL, headers=tw_headers).json()
    assert any(
        r["resource_type"] == "tw_iso_only_resource" for r in tw_body["resources"]
    )

    # CN 租戶：TW 專案的需求列與專案 id 一律不可見。
    cn_resp = client.get(ALLOCATION_URL, headers=cn_headers)
    assert cn_resp.status_code == 200, cn_resp.text
    cn_body = cn_resp.json()
    assert all(
        r["resource_type"] != "tw_iso_only_resource" for r in cn_body["resources"]
    )
    assert pid not in cn_body["unscheduled_projects"]
    assert all(pid not in w for w in cn_body["warnings"])
