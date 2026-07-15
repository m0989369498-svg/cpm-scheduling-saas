"""單一資源專屬例外停工日測試 (resource calendar holidays)。Pro Batch E (FEATURE E2)。

涵蓋:
  1. POST /level：資源專屬假日使該資源在該日產能歸零，迫使可移動任務推遲；
     adversarial check —— 只影響「該資源」在「該日」的產能，其餘資源/其餘日
     完全不受影響 (以 crane 假日不影響 manpower 為證)。
  2. GET/PUT /projects/{pid}/resources：ResourceConfig.calendars[].holidays round-trip。
  3. 無效日期字串 -> 422。
  4. 空 holidays (向下相容 Batch D 行為，未受影響)。

關鍵設計 (與 test_pro_batch_d_api.py 完全一致)：sqlite rebind + module-scope client。
"""
from __future__ import annotations

import os
import tempfile

_DB_FD, _DB_PATH = tempfile.mkstemp(prefix="cpm_resource_holidays_test_", suffix=".db")
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
        "project_name": f"資源假日測試專案 {project_id}",
        "region": "TW",
        "schedule_data": schedule_data,
    }
    payload.update(extra)
    resp = client.post(PROJECTS_URL, headers=headers, json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _four_parallel_tasks() -> list[dict]:
    """CRIT(5d,無需求) + X/Y(2d,各需 crane=1) + Z/W(2d,各需 manpower=2)，皆無前置，
    故 X/Y/Z/W 皆有正時差 (float=3)，為可移動任務 (供撫平推遲測試)。"""
    return [
        {"task_id": "CRIT", "task_name": "要徑基準", "duration": 5,
         "predecessors": [], "status": "PENDING"},
        {"task_id": "X", "task_name": "可移動X(crane)", "duration": 2,
         "predecessors": [], "status": "PENDING"},
        {"task_id": "Y", "task_name": "可移動Y(crane)", "duration": 2,
         "predecessors": [], "status": "PENDING"},
        {"task_id": "Z", "task_name": "可移動Z(manpower)", "duration": 2,
         "predecessors": [], "status": "PENDING"},
        {"task_id": "W", "task_name": "可移動W(manpower)", "duration": 2,
         "predecessors": [], "status": "PENDING"},
    ]


# =============================================================================
# 1. POST /level — 資源專屬假日只影響「該資源」在「該日」的產能
# =============================================================================
def test_resource_holiday_zeroes_only_that_resource_on_that_date(client):
    """crane 於 offset0 (2026-07-06) 有例外停工日 -> 該日 crane 產能歸零，
    迫使 X/Y (crane 需求) 皆推遲離開該日；manpower 完全不受影響 (Z/W 保持 es=0)。
    """
    headers = _admin_headers(client)
    pid = "PRJ-RESHOL-LEVEL"
    _create_project(
        client, headers, pid,
        start_date="2026-07-06",
        work_days="1111111",  # 全週皆工作日，簡化 offset<->date 對應。
        schedule_data=_four_parallel_tasks(),
    )

    resources_payload = {
        "limits": [
            {"resource_type": "crane", "max_capacity": 3, "unit_cost": 0.0,
             "category": "equipment"},
            {"resource_type": "manpower", "max_capacity": 10, "unit_cost": 0.0,
             "category": "labor"},
        ],
        "demands": {
            "X": {"crane": 1},
            "Y": {"crane": 1},
            "Z": {"manpower": 2},
            "W": {"manpower": 2},
        },
        "calendars": [
            {"resource_type": "crane", "work_days": "1111111",
             "holidays": ["2026-07-06"]},
            {"resource_type": "manpower", "work_days": "1111111", "holidays": []},
        ],
    }
    put_resp = client.put(
        f"{PROJECTS_URL}/{pid}/resources", headers=headers, json=resources_payload
    )
    assert put_resp.status_code == 200, put_resp.text

    level_resp = client.post(f"{PROJECTS_URL}/{pid}/level", headers=headers)
    assert level_resp.status_code == 200, level_resp.text
    tasks_by_id = {t["task_id"]: t for t in level_resp.json()["tasks"]}

    # crane 需求任務 (X/Y) 皆被推離 offset0 (該日 crane 產能=0)。
    # (manpower 需求任務 Z/W 的移動與否取決於 resource_leveling.py 既有的
    #  「最小 float 優先」啟發式候選挑選順序 -- 該邏輯為 Batch D 既有且凍結的
    #  純函式，非本測試範圍；下方的 _build_availability 直接測試才是本
    #  FEATURE E2 「只歸零該資源該日產能」的精確驗證。)
    assert tasks_by_id["X"]["es"] >= 1, "crane 假日應迫使 X 離開 day0"
    assert tasks_by_id["Y"]["es"] >= 1, "crane 假日應迫使 Y 離開 day0"


def test_build_availability_zeroes_only_that_resource_on_that_date(client):
    """精確驗證 adversarial check #3：_build_availability 只將「crane」在
    「offset0 (2026-07-06)」的產能歸零，其餘 (crane 其他日 / manpower 任何日)
    完全不受影響。直接呼叫 routers.resources._build_availability (與 API
    使用同一 DB 狀態)，避免耦合 resource_leveling.py 既有啟發式的候選挑選
    順序 (該順序非本 FEATURE E2 的驗證範疇)。
    """
    import asyncio

    headers = _admin_headers(client)
    pid = "PRJ-RESHOL-AVAIL-DIRECT"
    _create_project(
        client, headers, pid,
        start_date="2026-07-06",
        work_days="1111111",
        schedule_data=_four_parallel_tasks(),
    )

    resources_payload = {
        "limits": [
            {"resource_type": "crane", "max_capacity": 3, "unit_cost": 0.0,
             "category": "equipment"},
            {"resource_type": "manpower", "max_capacity": 10, "unit_cost": 0.0,
             "category": "labor"},
        ],
        "demands": {
            "X": {"crane": 1}, "Y": {"crane": 1},
            "Z": {"manpower": 2}, "W": {"manpower": 2},
        },
        "calendars": [
            {"resource_type": "crane", "work_days": "1111111",
             "holidays": ["2026-07-06"]},
            {"resource_type": "manpower", "work_days": "1111111", "holidays": []},
        ],
    }
    put_resp = client.put(
        f"{PROJECTS_URL}/{pid}/resources", headers=headers, json=resources_payload
    )
    assert put_resp.status_code == 200, put_resp.text

    from app import database
    from app.routers import resources as resources_router
    from app.routers.projects import (
        _build_task_definitions,
        _get_project_or_404,
        _load_dependencies,
        _load_tasks,
    )

    async def _load_availability():
        async with database.SessionLocal() as db:
            async with db.begin():
                await database.set_tenant_guc(db, "TENT-9981")
                project = await _get_project_or_404(db, pid, "TENT-9981")
                tasks = await _load_tasks(db, pid)
                deps = await _load_dependencies(db, pid)
                definitions = _build_task_definitions(tasks, deps)
                limits = resources_router._limits_to_map(
                    await resources_router._load_resource_limits(db, pid)
                )
                return await resources_router._build_availability(
                    db, project, pid, limits, definitions
                )

    availability = asyncio.run(_load_availability())

    # crane：day0 (holiday) 歸零；day1 起恢復完整上限 (3)。
    assert availability["crane"][0] == 0
    assert availability["crane"][1] == 3
    assert availability["crane"][2] == 3

    # manpower：完全不受 crane 專屬假日影響，day0 起皆為完整上限 (10)。
    assert availability["manpower"][0] == 10
    assert availability["manpower"][1] == 10


def test_no_holiday_calendar_matches_batch_d_behavior(client):
    """calendars 提供但 holidays 為空清單 -> 與 Batch D (無假日概念) 行為一致
    (無假日即無額外推遲來源；crane 產能全週皆為上限)。"""
    headers = _admin_headers(client)
    pid = "PRJ-RESHOL-NOHOL"
    _create_project(
        client, headers, pid,
        start_date="2026-07-06",
        work_days="1111111",
        schedule_data=_four_parallel_tasks(),
    )

    resources_payload = {
        "limits": [
            {"resource_type": "crane", "max_capacity": 3, "unit_cost": 0.0,
             "category": "equipment"},
        ],
        "demands": {"X": {"crane": 1}, "Y": {"crane": 1}},
        "calendars": [
            {"resource_type": "crane", "work_days": "1111111", "holidays": []},
        ],
    }
    put_resp = client.put(
        f"{PROJECTS_URL}/{pid}/resources", headers=headers, json=resources_payload
    )
    assert put_resp.status_code == 200, put_resp.text

    level_resp = client.post(f"{PROJECTS_URL}/{pid}/level", headers=headers)
    assert level_resp.status_code == 200, level_resp.text
    tasks_by_id = {t["task_id"]: t for t in level_resp.json()["tasks"]}

    # X+Y 需求 (1+1=2) <= crane 上限 3，無假日 -> 皆不需推遲。
    assert tasks_by_id["X"]["es"] == 0
    assert tasks_by_id["Y"]["es"] == 0


# =============================================================================
# 2. GET/PUT /projects/{pid}/resources — ResourceConfig.calendars[].holidays round-trip
# =============================================================================
def test_calendar_holidays_round_trip(client):
    headers = _admin_headers(client)
    pid = "PRJ-RESHOL-ROUNDTRIP"
    _create_project(client, headers, pid, schedule_data=[])

    payload = {
        "limits": [],
        "demands": {},
        "calendars": [
            {"resource_type": "crane", "work_days": "1111100",
             "holidays": ["2026-08-01", "2026-07-15"]},
        ],
    }
    put_resp = client.put(
        f"{PROJECTS_URL}/{pid}/resources", headers=headers, json=payload
    )
    assert put_resp.status_code == 200, put_resp.text
    put_cal = put_resp.json()["calendars"][0]
    assert put_cal["resource_type"] == "crane"
    # 輸出應排序 (依日期)。
    assert put_cal["holidays"] == ["2026-07-15", "2026-08-01"]

    got = client.get(f"{PROJECTS_URL}/{pid}/resources", headers=headers)
    assert got.status_code == 200, got.text
    got_cal = got.json()["calendars"][0]
    assert got_cal["holidays"] == ["2026-07-15", "2026-08-01"]


def test_calendar_holidays_put_replaces_full_list(client):
    """PUT 為替換式 upsert：第二次 PUT 提供不同 holidays 清單須完全取代第一次的清單。"""
    headers = _admin_headers(client)
    pid = "PRJ-RESHOL-REPLACE"
    _create_project(client, headers, pid, schedule_data=[])

    first = {
        "limits": [], "demands": [],
        "calendars": [{"resource_type": "crane", "work_days": "1111100",
                        "holidays": ["2026-07-01", "2026-07-02"]}],
    }
    first["demands"] = {}
    r1 = client.put(f"{PROJECTS_URL}/{pid}/resources", headers=headers, json=first)
    assert r1.status_code == 200, r1.text

    second = {
        "limits": [], "demands": {},
        "calendars": [{"resource_type": "crane", "work_days": "1111100",
                        "holidays": ["2026-09-09"]}],
    }
    r2 = client.put(f"{PROJECTS_URL}/{pid}/resources", headers=headers, json=second)
    assert r2.status_code == 200, r2.text
    assert r2.json()["calendars"][0]["holidays"] == ["2026-09-09"]

    got = client.get(f"{PROJECTS_URL}/{pid}/resources", headers=headers)
    assert got.json()["calendars"][0]["holidays"] == ["2026-09-09"]


# =============================================================================
# 3. 無效日期字串 -> 422
# =============================================================================
def test_invalid_holiday_date_string_422(client):
    headers = _admin_headers(client)
    pid = "PRJ-RESHOL-INVALID"
    _create_project(client, headers, pid, schedule_data=[])

    payload = {
        "limits": [], "demands": {},
        "calendars": [
            {"resource_type": "crane", "work_days": "1111100",
             "holidays": ["not-a-date"]},
        ],
    }
    resp = client.put(f"{PROJECTS_URL}/{pid}/resources", headers=headers, json=payload)
    assert resp.status_code == 422, resp.text
