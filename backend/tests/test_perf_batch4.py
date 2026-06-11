"""Batch 4 效能測試 (performance batch 4, sqlite TestClient pattern) —— 不需 Postgres。

本檔「不」標記 @pytest.mark.integration，於開發機 / CI backend-tests 皆執行
(與 test_auth.py / test_product_batch3.py 一致)。

涵蓋契約 (BATCH 4 — PERF-1 ~ PERF-4)
------------------------------------
(a) PERF-4 讀取路徑不重算：monkeypatch app.routers.projects.calculate_cpm 與
    app.routers.tasks.calculate_cpm 為「一呼叫即 AssertionError」；
    GET /projects/PRJ-2026-TW-001 與 GET .../tasks 仍 200，且 es/ef 與種子
    持久化之 CPM 結果一致 (種子已回寫 es/ef/ls/lf/float/critical)。
    還原 monkeypatch 後，PUT duration 仍正常重算 (PERF-2 bulk 寫回路徑)。
(b) PERF-1 儀表板正確性不變：GET /dashboard 含 PRJ-2026-TW-001，spi/cpi 為
    數值且 critical/task 數與 GET /projects/{pid} 一致；limit=1 僅回 1 專案。
    monkeypatch dashboard 的 calculate_cpm 參考 (raising=False) 確保彙總
    「不」執行 CPM。
(c) PERF-1 list_projects limit/offset：預設回全部；limit/offset 正確分頁；
    摘要 (task_count / project_duration) 與種子一致 (聚合查詢正確性)。
(d) PERF-3 sync_event_log.project_id：POST erp/sync 入列之事件列 project_id
    已填；POST evm/alert 之 RISK_PROVISION 列 project_id 已填。
(e) PERF-3 保留清理 (retention sweep)：人工插入 created_at 40 天前之 SUCCESS
    列與 100 天前之 DEAD 列 (另含新鮮 PENDING 與 40 天前 DEAD)；執行
    sweep_event_logs_once() 後：舊 SUCCESS / 舊 DEAD 已刪、新鮮 PENDING 與
    「未滿 90 天的 DEAD」保留。notification_outbox 同規則。

關鍵設計 (與 test_auth.py / test_product_batch3.py 完全一致)
------------------------------------------------------------
1. 「import app 之前」即設定 DATABASE_URL=sqlite 檔案 + DEV_BOOTSTRAP=1。
2. client fixture 以 _rebind_sqlite_engine() 將整個 DB 層重綁至本檔 sqlite 檔，
   teardown 時 dispose engine、還原全域綁定、再刪暫存檔 (Windows 句柄安全)。
3. (d)(e) 之 DB 直查 / worker 皆於「單一 asyncio.run 事件圈」內以「本測試自建的
   fresh engine/sessionmaker」執行，避免跨 event loop 重用 aiosqlite 連線。

種子已知值 (main._seed_core_data, PRJ-2026-TW-001)
--------------------------------------------------
  T-01 es=0 ef=5、T-02 es=5 ef=8、T-03 es=8 ef=10，全要徑，總工期 10。
  TW 租戶 (TENT-9981) 共兩個種子專案：PRJ-2026-TW-001、PRJ-2026-TW-PARALLEL。
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import uuid
from datetime import datetime, timedelta, timezone

# --------------------------------------------------------------------------- #
# 於匯入 app 之前先設定環境變數 (env at top before importing app)。
# 使用具名暫存「檔案」作為 sqlite DB，確保跨連線共享資料且測試可重現。
# --------------------------------------------------------------------------- #
_DB_FD, _DB_PATH = tempfile.mkstemp(prefix="cpm_perf4_test_", suffix=".db")
os.close(_DB_FD)
# sqlite URL 以正斜線表示路徑 (Windows 亦適用)。
_DB_URL = "sqlite+aiosqlite:///" + _DB_PATH.replace("\\", "/")
os.environ["DATABASE_URL"] = _DB_URL
os.environ["DEV_BOOTSTRAP"] = "1"
# 預設維持 header mode (本檔以 Bearer token 取得 admin 角色)。
os.environ.setdefault("AUTH_REQUIRED", "false")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402

PREFIX = settings.api_v1_prefix
LOGIN_URL = f"{PREFIX}/auth/login"
PROJECTS_URL = f"{PREFIX}/projects"
DASHBOARD_URL = f"{PREFIX}/dashboard"

DEMO_PASSWORD = "demo1234"
TENANT_TW = "TENT-9981"
SEED_PROJECT_TW = "PRJ-2026-TW-001"
SEED_PROJECT_PARALLEL = "PRJ-2026-TW-PARALLEL"

# 種子 PRJ-2026-TW-001 已持久化之 CPM 結果 (見 main._seed_core_data)。
SEED_TW_RESULTS = {
    "T-01": {"es": 0, "ef": 5},
    "T-02": {"es": 5, "ef": 8},
    "T-03": {"es": 8, "ef": 10},
}
SEED_TW_DURATION = 10


# --------------------------------------------------------------------------- #
# sqlite rebind / teardown 工具 (與 test_auth.py / test_product_batch3.py 同法)
# --------------------------------------------------------------------------- #
def _rebind_sqlite_engine() -> None:
    """把 app 的 DB 層強制指向本檔的 sqlite 暫存檔。

    conftest 會在收集階段先 import app，故 engine/SessionLocal 可能早已綁到別的
    DSN。這裡重建 async engine + sessionmaker (與 database.py 的 sqlite 分支一致：
    schema_translate_map 把 erp_integration 映射為 None、check_same_thread=False)，
    並更新所有「以 from app.database import SessionLocal 綁定」的模組參考。
    """
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
    """快照 _rebind_sqlite_engine()/lifespan 會變動的全域 DB 綁定 (避免污染後續測試)。"""
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
    """還原 _snapshot_app_db_state() 取得的全域 DB 綁定。"""
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


# --------------------------------------------------------------------------- #
# 共用小工具
# --------------------------------------------------------------------------- #
def _login(client: TestClient, username: str, password: str) -> dict:
    resp = client.post(LOGIN_URL, json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _admin_headers(client: TestClient) -> dict[str, str]:
    """以 demo admin@tw 登入並回傳 Authorization 標頭 (role=admin)。"""
    token = _login(client, "admin@tw", DEMO_PASSWORD)["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _make_fresh_engine():
    """本測試專用 fresh engine/sessionmaker (與 database.py sqlite 分支一致)。

    (d)(e) 之 DB 直查 / worker 全部跑在「同一個」asyncio.run 事件圈並使用此
    工廠，避免跨 event loop 重用 aiosqlite 連線。呼叫端負責 engine.dispose()。
    """
    from sqlalchemy.ext.asyncio import (
        AsyncSession,
        async_sessionmaker,
        create_async_engine,
    )

    engine = create_async_engine(
        _DB_URL,
        future=True,
        connect_args={"check_same_thread": False},
        execution_options={"schema_translate_map": {"erp_integration": None}},
    )
    factory = async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )
    return engine, factory


def _boom_calculate_cpm(*_args, **_kwargs):
    """PERF 契約守門：讀取/彙總路徑「絕不」呼叫 calculate_cpm。"""
    raise AssertionError(
        "calculate_cpm must NOT run on read/aggregate paths (PERF-1 / PERF-4)"
    )


def _task_map(tasks: list[dict]) -> dict[str, dict]:
    return {t["task_id"]: t for t in tasks}


# --------------------------------------------------------------------------- #
# (a) PERF-4 — 讀取路徑直接回傳持久化 CPM 欄位，不重算；還原後寫入路徑仍可重算。
# --------------------------------------------------------------------------- #
def test_read_paths_serve_persisted_results_without_cpm(client, monkeypatch):
    headers = _admin_headers(client)

    # 讀取期間 calculate_cpm 一被呼叫即炸 (raising=False：若 PERF-4 已移除
    # 該模組參考，patch 仍不報錯 —— 讀取路徑自然不可能呼叫)。
    monkeypatch.setattr(
        "app.routers.projects.calculate_cpm", _boom_calculate_cpm, raising=False
    )
    monkeypatch.setattr(
        "app.routers.tasks.calculate_cpm", _boom_calculate_cpm, raising=False
    )

    # GET /projects/{pid}：200 且 es/ef 與種子持久化值一致。
    got = client.get(f"{PROJECTS_URL}/{SEED_PROJECT_TW}", headers=headers)
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["project_duration"] == SEED_TW_DURATION
    tasks = _task_map(body["tasks"])
    for task_id, expected in SEED_TW_RESULTS.items():
        assert tasks[task_id]["es"] == expected["es"], tasks[task_id]
        assert tasks[task_id]["ef"] == expected["ef"], tasks[task_id]
        assert tasks[task_id]["is_critical"] is True
        assert tasks[task_id]["float_time"] == 0
    # 相依視圖 (links/predecessors) 仍由 deps 組裝，不依賴 CPM。
    assert tasks["T-02"]["predecessors"] == ["T-01"]

    # GET /projects/{pid}/tasks：200 且 es/ef 同樣取自持久化欄位。
    got_tasks = client.get(f"{PROJECTS_URL}/{SEED_PROJECT_TW}/tasks", headers=headers)
    assert got_tasks.status_code == 200, got_tasks.text
    listed = _task_map(got_tasks.json())
    assert set(SEED_TW_RESULTS) <= set(listed)
    for task_id, expected in SEED_TW_RESULTS.items():
        assert listed[task_id]["es"] == expected["es"], listed[task_id]
        assert listed[task_id]["ef"] == expected["ef"], listed[task_id]
        assert listed[task_id]["is_critical"] is True

    # 還原 monkeypatch —— 寫入路徑 (PUT duration) 仍須正常重算。
    monkeypatch.undo()

    duration_url = f"{PROJECTS_URL}/{SEED_PROJECT_TW}/tasks/T-01/duration"
    bumped = client.put(duration_url, headers=headers, json={"duration": 7})
    assert bumped.status_code == 200, bumped.text
    bumped_tasks = _task_map(bumped.json()["tasks"])
    assert bumped_tasks["T-01"]["ef"] == 7
    assert bumped_tasks["T-03"]["ef"] == 12
    assert bumped.json()["project_duration"] == 12

    # 重算結果確實「持久化」(PERF-2 bulk 寫回)：後續 GET (讀取路徑，不重算)
    # 看得到新值。
    reread = client.get(f"{PROJECTS_URL}/{SEED_PROJECT_TW}", headers=headers)
    assert reread.status_code == 200, reread.text
    assert reread.json()["project_duration"] == 12
    assert _task_map(reread.json()["tasks"])["T-01"]["ef"] == 7

    # 還原種子狀態 (duration 5 -> 總工期 10)，避免影響後續測試。
    restored = client.put(duration_url, headers=headers, json={"duration": 5})
    assert restored.status_code == 200, restored.text
    assert restored.json()["project_duration"] == SEED_TW_DURATION
    restored_tasks = _task_map(restored.json()["tasks"])
    for task_id, expected in SEED_TW_RESULTS.items():
        assert restored_tasks[task_id]["es"] == expected["es"]
        assert restored_tasks[task_id]["ef"] == expected["ef"]


# --------------------------------------------------------------------------- #
# (b) PERF-1 — 儀表板彙總正確性不變 (常數查詢數重構)；limit=1 僅回 1 專案。
# --------------------------------------------------------------------------- #
def test_dashboard_correctness_preserved_and_limit(client, monkeypatch):
    headers = _admin_headers(client)

    # 以 GET /projects/{pid} (持久化 CPM 結果) 推導期望值。
    project = client.get(f"{PROJECTS_URL}/{SEED_PROJECT_TW}", headers=headers)
    assert project.status_code == 200, project.text
    pbody = project.json()
    expected_task_count = len(pbody["tasks"])
    expected_critical = sum(1 for t in pbody["tasks"] if t["is_critical"])
    expected_duration = pbody["project_duration"]
    assert expected_task_count == 3  # 種子已知形狀 (防回歸基準)

    # PERF-1 契約：儀表板「不」執行 calculate_cpm (raising=False 容忍 import 已移除)。
    monkeypatch.setattr(
        "app.routers.dashboard.calculate_cpm", _boom_calculate_cpm, raising=False
    )

    resp = client.get(DASHBOARD_URL, headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert isinstance(body.get("projects"), list)
    assert isinstance(body.get("totals"), dict)

    by_id = {p["project_id"]: p for p in body["projects"]}
    assert SEED_PROJECT_TW in by_id, f"dashboard 應含種子專案: {list(by_id)}"
    kpi = by_id[SEED_PROJECT_TW]

    # spi / cpi 為數值 (種子已含基準線 + 進度)。
    assert kpi["has_baseline"] is True
    assert isinstance(kpi["spi"], (int, float)), kpi
    assert isinstance(kpi["cpi"], (int, float)), kpi

    # 彙總 (SQL 聚合) 與單專案讀取 (持久化欄位) 一致。
    assert kpi["task_count"] == expected_task_count
    assert kpi["critical_count"] == expected_critical
    assert kpi["project_duration"] == expected_duration
    assert isinstance(kpi["pending_risk_events"], int)
    assert kpi["pending_risk_events"] >= 0

    # totals 仍與 projects 彙整一致 (回應形狀不變)。
    totals = body["totals"]
    assert totals["project_count"] == len(body["projects"])
    assert totals["task_count"] == sum(p["task_count"] for p in body["projects"])

    # limit=1 -> 僅回 1 個專案。
    limited = client.get(DASHBOARD_URL, params={"limit": 1}, headers=headers)
    assert limited.status_code == 200, limited.text
    limited_projects = limited.json()["projects"]
    assert len(limited_projects) == 1, limited_projects


# --------------------------------------------------------------------------- #
# (c) PERF-1 — list_projects：預設回全部；limit/offset 正確分頁；聚合摘要正確。
# --------------------------------------------------------------------------- #
def test_list_projects_limit_offset(client):
    headers = _admin_headers(client)

    # 預設 (無參數) = 全部 (向下相容)。TW 租戶有兩個種子專案。
    all_resp = client.get(PROJECTS_URL, headers=headers)
    assert all_resp.status_code == 200, all_resp.text
    all_rows = all_resp.json()
    all_ids = {p["project_id"] for p in all_rows}
    assert {SEED_PROJECT_TW, SEED_PROJECT_PARALLEL} <= all_ids
    total = len(all_rows)
    assert total >= 2

    # 聚合查詢的摘要正確性 (取代逐專案 Task hydration 後形狀/數值不變)。
    seed_summary = next(p for p in all_rows if p["project_id"] == SEED_PROJECT_TW)
    assert seed_summary["task_count"] == 3
    assert seed_summary["project_duration"] == SEED_TW_DURATION
    assert seed_summary["tenant_id"] == TENANT_TW

    # limit=1 -> 恰 1 筆，且為全集合成員。
    page0 = client.get(PROJECTS_URL, params={"limit": 1}, headers=headers)
    assert page0.status_code == 200, page0.text
    page0_rows = page0.json()
    assert len(page0_rows) == 1
    assert page0_rows[0]["project_id"] in all_ids

    # offset=1 -> 其餘 total-1 筆。
    rest = client.get(PROJECTS_URL, params={"offset": 1}, headers=headers)
    assert rest.status_code == 200, rest.text
    rest_rows = rest.json()
    assert len(rest_rows) == total - 1
    assert {p["project_id"] for p in rest_rows} <= all_ids

    # limit+offset 分頁互斥：第 0 頁與第 1 頁 (各 1 筆) 不重複。
    page1 = client.get(
        PROJECTS_URL, params={"limit": 1, "offset": 1}, headers=headers
    )
    assert page1.status_code == 200, page1.text
    page1_rows = page1.json()
    assert len(page1_rows) == 1
    assert page1_rows[0]["project_id"] != page0_rows[0]["project_id"]

    # offset 超出範圍 -> 空清單 (而非錯誤)。
    beyond = client.get(PROJECTS_URL, params={"offset": total}, headers=headers)
    assert beyond.status_code == 200, beyond.text
    assert beyond.json() == []


# --------------------------------------------------------------------------- #
# (d) PERF-3 — sync_event_log.project_id：erp/sync 與 evm/alert 入列即填欄位。
# --------------------------------------------------------------------------- #
def test_sync_event_log_project_id_populated(client, monkeypatch):
    # 清空全域通知憑證 -> evm/alert 的通知入列走 LOG 退路 (deterministic，
    # 且絕不對外發出真實 HTTP 通知)。
    monkeypatch.setattr(settings, "line_channel_access_token", "")
    monkeypatch.setattr(settings, "dingtalk_webhook_url", "")
    monkeypatch.setattr(settings, "wecom_webhook_url", "", raising=False)

    headers = _admin_headers(client)

    # 1) ERP 拋轉入列：每任務一筆 SCHEDULE_PUSH 事件。
    sync_resp = client.post(
        f"{PROJECTS_URL}/{SEED_PROJECT_TW}/erp/sync",
        headers=headers,
        json={"sync_type": "SCHEDULE_PUSH"},
    )
    assert sync_resp.status_code == 202, sync_resp.text
    sync_body = sync_resp.json()
    assert sync_body["enqueued"] == 3
    push_ids = {uuid.UUID(e) for e in sync_body["event_ids"]}
    assert len(push_ids) == 3

    # 2) EVM 風險拋轉：種子 TW 專案「落後 + 超支」-> RISK_PROVISION 事件。
    alert = client.post(
        f"{PROJECTS_URL}/{SEED_PROJECT_TW}/evm/alert", headers=headers
    )
    assert alert.status_code == 200, alert.text
    alert_body = alert.json()
    assert alert_body["dispatched"] is True
    assert alert_body["risk_flagged"] is True
    risk_event_id = uuid.UUID(alert_body["event_id"])

    # 3) DB 直查：兩類事件列的 project_id 欄位 (PERF-3 新欄位) 皆已填。
    from sqlalchemy import select

    from app.core.risk_listener import RISK_PROVISION_SYNC_TYPE
    from app.models.orm import SyncEvent

    engine, factory = _make_fresh_engine()

    async def _scenario() -> None:
        try:
            async with factory() as session:
                rows = (
                    (
                        await session.execute(
                            select(SyncEvent).where(SyncEvent.event_id.in_(push_ids))
                        )
                    )
                    .scalars()
                    .all()
                )
                assert len(rows) == len(push_ids), "拋轉事件列應全部存在"
                for row in rows:
                    assert row.tenant_id == TENANT_TW
                    assert row.sync_type == "SCHEDULE_PUSH"
                    assert row.project_id == SEED_PROJECT_TW, (
                        f"event {row.event_id} 的 project_id 應已填: {row.project_id!r}"
                    )

                risk_row = (
                    await session.execute(
                        select(SyncEvent).where(SyncEvent.event_id == risk_event_id)
                    )
                ).scalar_one()
                assert risk_row.sync_type == RISK_PROVISION_SYNC_TYPE
                assert risk_row.tenant_id == TENANT_TW
                assert risk_row.status == "PENDING"
                assert risk_row.project_id == SEED_PROJECT_TW, (
                    f"RISK_PROVISION 列的 project_id 應已填: {risk_row.project_id!r}"
                )
        finally:
            await engine.dispose()

    asyncio.run(_scenario())


# --------------------------------------------------------------------------- #
# (e) PERF-3 — 保留清理：SUCCESS > 30 天、DEAD > 90 天刪除；其餘保留。
#     sync_event_log 與 notification_outbox 同規則。
# --------------------------------------------------------------------------- #
def test_retention_sweep_deletes_only_expired_rows(client, monkeypatch):
    pytest.importorskip("apscheduler")  # worker 模組依賴 (dev 機缺套件時乾淨跳過)

    from sqlalchemy import select

    import app.erp.worker as worker
    from app.models.orm import NotificationOutbox, SyncEvent

    # 本測試專用 fresh engine/sessionmaker：插入、sweep、驗證全部跑在
    # 「同一個」asyncio.run 事件圈 (避免跨 loop 重用 aiosqlite 連線)。
    engine, factory = _make_fresh_engine()
    monkeypatch.setattr(worker, "WorkerSessionLocal", factory)

    now = datetime.now(timezone.utc)
    forty_days_ago = now - timedelta(days=40)
    hundred_days_ago = now - timedelta(days=100)

    def _event(status: str, created_at: datetime | None, tag: str) -> SyncEvent:
        kwargs: dict = {}
        if created_at is not None:
            kwargs["created_at"] = created_at
        return SyncEvent(
            tenant_id=TENANT_TW,
            mapping_id=None,
            sync_type="SCHEDULE_PUSH",
            payload={"artificial": tag},
            status=status,
            retry_count=0,
            **kwargs,
        )

    def _outbox(status: str, created_at: datetime | None, tag: str) -> NotificationOutbox:
        kwargs: dict = {}
        if created_at is not None:
            kwargs["created_at"] = created_at
        return NotificationOutbox(
            tenant_id=TENANT_TW,
            region="TW",
            channel="LOG",
            message=f"artificial sweep row: {tag}",
            status=status,
            retry_count=0,
            **kwargs,
        )

    async def _scenario() -> None:
        try:
            # --- 安排 (arrange)：人工回填 created_at 的事件 / outbox 列 -------
            ev_old_success = _event("SUCCESS", forty_days_ago, "old-success-40d")
            ev_old_dead = _event("DEAD", hundred_days_ago, "old-dead-100d")
            ev_recent_dead = _event("DEAD", forty_days_ago, "recent-dead-40d")
            ev_fresh_pending = _event("PENDING", None, "fresh-pending")

            ob_old_success = _outbox("SUCCESS", forty_days_ago, "old-success-40d")
            ob_old_dead = _outbox("DEAD", hundred_days_ago, "old-dead-100d")
            ob_fresh_pending = _outbox("PENDING", None, "fresh-pending")

            async with factory() as session:
                async with session.begin():
                    session.add_all(
                        [
                            ev_old_success,
                            ev_old_dead,
                            ev_recent_dead,
                            ev_fresh_pending,
                            ob_old_success,
                            ob_old_dead,
                            ob_fresh_pending,
                        ]
                    )

            all_event_ids = [
                ev_old_success.event_id,
                ev_old_dead.event_id,
                ev_recent_dead.event_id,
                ev_fresh_pending.event_id,
            ]
            all_outbox_ids = [
                ob_old_success.id,
                ob_old_dead.id,
                ob_fresh_pending.id,
            ]

            # --- 執行 (act)：單次保留清理 ------------------------------------
            await worker.sweep_event_logs_once()

            # --- 驗證 (assert)：僅過期列被刪除 --------------------------------
            async with factory() as session:
                remaining_events = {
                    row.event_id
                    for row in (
                        await session.execute(
                            select(SyncEvent).where(
                                SyncEvent.event_id.in_(all_event_ids)
                            )
                        )
                    )
                    .scalars()
                    .all()
                }
                assert ev_old_success.event_id not in remaining_events, (
                    "SUCCESS 逾 30 天的事件列應被清除"
                )
                assert ev_old_dead.event_id not in remaining_events, (
                    "DEAD 逾 90 天的事件列應被清除"
                )
                assert ev_recent_dead.event_id in remaining_events, (
                    "DEAD 未滿 90 天 (40 天) 的事件列不應被清除"
                )
                assert ev_fresh_pending.event_id in remaining_events, (
                    "新鮮 PENDING 事件列不應被清除"
                )

                remaining_outbox = {
                    row.id
                    for row in (
                        await session.execute(
                            select(NotificationOutbox).where(
                                NotificationOutbox.id.in_(all_outbox_ids)
                            )
                        )
                    )
                    .scalars()
                    .all()
                }
                assert ob_old_success.id not in remaining_outbox, (
                    "SUCCESS 逾 30 天的 outbox 列應被清除"
                )
                assert ob_old_dead.id not in remaining_outbox, (
                    "DEAD 逾 90 天的 outbox 列應被清除"
                )
                assert ob_fresh_pending.id in remaining_outbox, (
                    "新鮮 PENDING outbox 列不應被清除"
                )
        finally:
            await engine.dispose()

    asyncio.run(_scenario())
