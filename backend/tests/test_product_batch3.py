"""Batch 3 產品能力測試 (product capability, sqlite TestClient pattern) —— 不需 Postgres。

本檔「不」標記 @pytest.mark.integration，於開發機 / CI backend-tests 皆執行
(與 test_auth.py / test_security_batch1.py 一致)。

涵蓋契約 (BATCH 3 — 後端可由 API / worker 驗證之行為)
------------------------------------------------------
(a) FEAT-2 真實日期 + 工作日曆：建立專案帶 start_date=2026-07-01 +
    work_days='1111100' (週一~週五) -> ProjectOut.day_dates[0]=='2026-07-01'，
    且 day_dates 跳過週末 (已知對應：offset3=7/06 週一、offset8=7/13 下週一)。
(b) FEAT-3 樂觀並行 (optimistic concurrency)：PUT duration 帶錯誤
    expected_version -> 409 {"detail":"版本衝突：專案已被其他人修改",
    "current_version":N}；帶正確值 -> 200 且 version 遞增。
(c) FEAT-4 軟刪除 / 回收桶：DELETE -> 從 GET /projects 消失 ->
    GET /projects/trash (admin) 可見 -> restore 還原 -> purge 永久刪除。
(d) FEAT-1 links 往返 (round-trip)：新增任務帶 links [{pred, SS, 2}] ->
    GET 專案回傳該 link 且 es 符合 SS 語義。
(e) FEAT-6 租戶開通 CLI：以 import + call 執行 app.provision_tenant main()
    建立租戶 T-PROV + admin，隨後以該 admin 登入 -> 200。
(f) FEAT-5 ERP 成本回拉：monkeypatch FakeAdapter.fetch_actuals 回傳已知
    wbs->cost；插入 task_mapping；執行 pull_actuals_once()；驗證
    task_progress.actual_cost 已更新且存在 COST_PULL 之 sync_event_log 列。

關鍵設計 (與 test_auth.py / test_security_batch1.py 完全一致)
------------------------------------------------------------
1. 「import app 之前」即設定 DATABASE_URL=sqlite 檔案 + DEV_BOOTSTRAP=1。
2. client fixture 以 _rebind_sqlite_engine() 將整個 DB 層重綁至本檔 sqlite 檔，
   teardown 時 dispose engine、還原全域綁定、再刪暫存檔 (Windows 句柄安全)。
3. (f) 之 worker 與 DB 直查皆於「單一 asyncio.run 事件圈」內以「本測試自建的
   fresh engine/sessionmaker」執行，避免跨 event loop 重用 aiosqlite 連線。
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from datetime import date

# --------------------------------------------------------------------------- #
# 於匯入 app 之前先設定環境變數 (env at top before importing app)。
# 使用具名暫存「檔案」作為 sqlite DB，確保跨連線共享資料且測試可重現。
# --------------------------------------------------------------------------- #
_DB_FD, _DB_PATH = tempfile.mkstemp(prefix="cpm_batch3_test_", suffix=".db")
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

DEMO_PASSWORD = "demo1234"
TENANT_TW = "TENT-9981"
SEED_PROJECT_TW = "PRJ-2026-TW-001"

# FEAT-3 契約之 409 訊息 (frozen contract)。
VERSION_CONFLICT_DETAIL = "版本衝突：專案已被其他人修改"


# --------------------------------------------------------------------------- #
# sqlite rebind / teardown 工具 (與 test_auth.py / test_security_batch1.py 同法)
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


def _chain_schedule() -> list[dict]:
    """標準線性鏈 T-01(5) -> T-02(3) -> T-03(2)，專案總工期 10。"""
    return [
        {"task_id": "T-01", "task_name": "基地開挖", "duration": 5,
         "predecessors": [], "status": "PENDING"},
        {"task_id": "T-02", "task_name": "一樓鋼筋綁紮", "duration": 3,
         "predecessors": ["T-01"], "status": "PENDING"},
        {"task_id": "T-03", "task_name": "一樓混凝土澆置", "duration": 2,
         "predecessors": ["T-02"], "status": "PENDING"},
    ]


def _create_project(
    client: TestClient,
    headers: dict[str, str],
    project_id: str,
    *,
    schedule_data: list[dict] | None = None,
    **extra,
) -> dict:
    """建立專案並回傳 ProjectOut body (預設帶標準 3 任務鏈)。"""
    payload = {
        "project_id": project_id,
        "project_name": f"Batch3 測試專案 {project_id}",
        "region": "TW",
        "schedule_data": _chain_schedule() if schedule_data is None else schedule_data,
    }
    payload.update(extra)
    resp = client.post(PROJECTS_URL, headers=headers, json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


# --------------------------------------------------------------------------- #
# (a) FEAT-2 真實日期 + 工作日曆：start_date + work_days -> day_dates
#     2026-07-01 為週三；work_days='1111100' (週一~週五，不含週末)：
#       offset 0 = 2026-07-01 (三)   offset 3 = 2026-07-06 (一，跳過 7/4 7/5 週末)
#       offset 8 = 2026-07-13 (一)   offset 10 = 2026-07-15 (三)
#     「第一個週一 (offset 3) + 5 個工作天 = offset 8」落在下週一 —— 已知對應。
# --------------------------------------------------------------------------- #
def test_project_start_date_and_work_calendar(client):
    headers = _admin_headers(client)
    body = _create_project(
        client,
        headers,
        "PRJ-B3-DATES",
        start_date="2026-07-01",
        work_days="1111100",
    )

    assert body["start_date"] == "2026-07-01"
    assert body["work_days"] == "1111100"
    assert body["project_duration"] == 10

    day_dates = body["day_dates"]
    assert isinstance(day_dates, list)
    # offsets 0..project_duration -> 長度 = 工期 + 1
    assert len(day_dates) == 11

    # 已知對應 (known mapping)
    assert day_dates[0] == "2026-07-01"
    assert day_dates[3] == "2026-07-06"   # 跳過週末 (7/4 六、7/5 日)
    assert day_dates[8] == "2026-07-13"   # 第一個週一 + 5 個工作天 = 下週一
    assert day_dates[10] == "2026-07-15"

    # 全部落在工作日 (週一=0 .. 週五=4)，絕無週末。
    for iso in day_dates:
        assert date.fromisoformat(iso).weekday() < 5, f"{iso} 不應為週末"

    # GET 專案亦回傳相同 day_dates (持久化 + 重讀一致)。
    got = client.get(f"{PROJECTS_URL}/PRJ-B3-DATES", headers=headers)
    assert got.status_code == 200, got.text
    got_body = got.json()
    assert got_body["start_date"] == "2026-07-01"
    assert got_body["day_dates"] == day_dates


# --------------------------------------------------------------------------- #
# (b) FEAT-3 樂觀並行：expected_version 不符 -> 409；相符 -> 200 且 version 遞增。
# --------------------------------------------------------------------------- #
def test_version_conflict_optimistic_concurrency(client):
    headers = _admin_headers(client)
    _create_project(client, headers, "PRJ-B3-VERSION")

    got = client.get(f"{PROJECTS_URL}/PRJ-B3-VERSION", headers=headers)
    assert got.status_code == 200, got.text
    version = got.json()["version"]
    assert isinstance(version, int)

    duration_url = f"{PROJECTS_URL}/PRJ-B3-VERSION/tasks/T-01/duration"

    # 錯誤的 expected_version -> 409 + 契約 body 形狀。
    conflict = client.put(
        duration_url,
        headers=headers,
        json={"duration": 7, "expected_version": version - 999},
    )
    assert conflict.status_code == 409, conflict.text
    conflict_body = conflict.json()
    assert conflict_body["detail"] == VERSION_CONFLICT_DETAIL
    assert conflict_body["current_version"] == version

    # 正確的 expected_version -> 200，且 version 遞增。
    ok = client.put(
        duration_url,
        headers=headers,
        json={"duration": 7, "expected_version": version},
    )
    assert ok.status_code == 200, ok.text
    ok_body = ok.json()
    assert ok_body["version"] == version + 1

    # 工期確實已更新。
    t01 = next(t for t in ok_body["tasks"] if t["task_id"] == "T-01")
    assert t01["duration"] == 7

    # 不帶 expected_version -> 不檢查 (向後相容，200)。
    legacy = client.put(duration_url, headers=headers, json={"duration": 5})
    assert legacy.status_code == 200, legacy.text


# --------------------------------------------------------------------------- #
# (c) FEAT-4 軟刪除 / 回收桶：DELETE -> 排除於清單 -> trash 可見 -> restore ->
#     回到清單 -> 再刪 -> purge -> 自 trash 消失且 404。
# --------------------------------------------------------------------------- #
def _listed_project_ids(client: TestClient, headers: dict[str, str]) -> set[str]:
    resp = client.get(PROJECTS_URL, headers=headers)
    assert resp.status_code == 200, resp.text
    return {p["project_id"] for p in resp.json()}


def _trash_project_ids(client: TestClient, headers: dict[str, str]) -> set[str]:
    resp = client.get(f"{PROJECTS_URL}/trash", headers=headers)
    assert resp.status_code == 200, resp.text
    return {p["project_id"] for p in resp.json()}


def test_soft_delete_trash_restore_purge(client):
    headers = _admin_headers(client)
    pid = "PRJ-B3-TRASH"
    _create_project(client, headers, pid)
    assert pid in _listed_project_ids(client, headers)

    # 軟刪除：回應契約維持 {ok: true}。
    deleted = client.delete(f"{PROJECTS_URL}/{pid}", headers=headers)
    assert deleted.status_code == 200, deleted.text
    assert deleted.json() == {"ok": True}

    # 讀取路徑全面排除：清單不見、單查 404。
    assert pid not in _listed_project_ids(client, headers)
    assert client.get(f"{PROJECTS_URL}/{pid}", headers=headers).status_code == 404

    # 回收桶 (admin) 可見 —— 同時驗證 /projects/trash 路由順序正確
    # (未被 /projects/{project_id} 吞掉)。
    assert pid in _trash_project_ids(client, headers)

    # 還原 -> 回到清單、自回收桶移除。
    restored = client.post(f"{PROJECTS_URL}/{pid}/restore", headers=headers)
    assert restored.status_code < 300, restored.text
    assert pid in _listed_project_ids(client, headers)
    assert pid not in _trash_project_ids(client, headers)

    # 再次軟刪除後永久刪除 (purge) -> 自回收桶消失、單查 404、清單不見。
    deleted_again = client.delete(f"{PROJECTS_URL}/{pid}", headers=headers)
    assert deleted_again.status_code == 200, deleted_again.text
    assert pid in _trash_project_ids(client, headers)

    purged = client.delete(f"{PROJECTS_URL}/{pid}/purge", headers=headers)
    assert purged.status_code < 300, purged.text
    assert pid not in _trash_project_ids(client, headers)
    assert pid not in _listed_project_ids(client, headers)
    assert client.get(f"{PROJECTS_URL}/{pid}", headers=headers).status_code == 404


# --------------------------------------------------------------------------- #
# (d) FEAT-1 links 往返 (round-trip)：新增任務帶 links [{A, SS, 2}] ->
#     回應與 GET 皆回傳該 link，且 es 符合 SS 語義 (B.es = A.es + 2 = 2)。
# --------------------------------------------------------------------------- #
def test_links_round_trip_ss_lag(client):
    headers = _admin_headers(client)
    pid = "PRJ-B3-LINKS"
    _create_project(
        client,
        headers,
        pid,
        schedule_data=[
            {"task_id": "A", "task_name": "開挖", "duration": 5,
             "predecessors": [], "status": "PENDING"},
        ],
    )

    created = client.post(
        f"{PROJECTS_URL}/{pid}/tasks",
        headers=headers,
        json={
            "task_id": "B",
            "task_name": "排水",
            "duration": 3,
            "links": [
                {"predecessor_task_id": "A", "dep_type": "SS", "lag_days": 2}
            ],
        },
    )
    assert created.status_code == 201, created.text

    def _assert_link_shape(body: dict) -> None:
        tasks = {t["task_id"]: t for t in body["tasks"]}
        task_b = tasks["B"]

        # SS+2 語義：B.es = A.es + 2 = 2、B.ef = 5；總工期 5 (見引擎測試 (b))。
        assert task_b["es"] == 2
        assert task_b["ef"] == 5
        assert body["project_duration"] == 5

        # links 持久化往返；predecessors 由 links 重新推導。
        links = task_b["links"]
        assert isinstance(links, list) and len(links) == 1
        link = links[0]
        assert link["predecessor_task_id"] == "A"
        assert link["dep_type"] == "SS"
        assert link["lag_days"] == 2
        assert task_b["predecessors"] == ["A"]

        # SS+2 下兩者皆要徑 (hand-computed，見 test_dependency_types)。
        assert tasks["A"]["is_critical"] is True
        assert task_b["is_critical"] is True

    _assert_link_shape(created.json())

    # GET 重讀：link 自 task_dependencies (dep_type/lag_days) 還原。
    got = client.get(f"{PROJECTS_URL}/{pid}", headers=headers)
    assert got.status_code == 200, got.text
    _assert_link_shape(got.json())


# --------------------------------------------------------------------------- #
# (e) FEAT-6 租戶開通 CLI：import + call main() 建立 T-PROV + admin -> 登入 200。
# --------------------------------------------------------------------------- #
def _dispose_module_engines(mod) -> None:
    """best-effort 釋放模組層級 async engine (避免持有本檔 sqlite 暫存檔句柄)。"""
    for name in ("_engine", "engine"):
        eng = getattr(mod, name, None)
        if eng is None or not hasattr(eng, "dispose"):
            continue
        try:
            asyncio.run(eng.dispose())
        except Exception:
            pass


def _run_provision_main(argv: list[str]) -> None:
    """執行 app.provision_tenant 的 main()，相容 sync/async 與 argv/sys.argv 兩種形狀。

    - main(argv) 為慣用形狀；若簽章不收參數則回退 sys.argv 模式。
    - async main -> 以 asyncio.run 執行 (契約：run main() via asyncio)。
    - 成功時容忍 SystemExit(0/None) 或回傳 0/None；其餘視為失敗。
    """
    import inspect

    from app import provision_tenant

    old_argv = sys.argv
    sys.argv = ["app.provision_tenant", *argv]
    try:
        try:
            result = provision_tenant.main(argv)
        except TypeError:
            result = provision_tenant.main()
        if inspect.iscoroutine(result):
            result = asyncio.run(result)
    except SystemExit as exc:
        assert exc.code in (None, 0), f"provision_tenant 應成功結束 (exit={exc.code})"
        result = None
    finally:
        sys.argv = old_argv
        _dispose_module_engines(provision_tenant)

    assert result in (None, 0), f"provision_tenant 應成功結束 (return={result!r})"


def test_provision_tenant_cli_then_login(client):
    _run_provision_main(
        [
            "--tenant-id", "T-PROV",
            "--name", "開通測試租戶",
            "--region", "TW",
            "--admin-username", "prov-admin@test",
            "--admin-password", "Prov-12345",
        ]
    )

    body = _login(client, "prov-admin@test", "Prov-12345")
    assert body["access_token"]
    assert body["token_type"] == "bearer"
    assert body["tenant_id"] == "T-PROV"
    assert body["region"] == "TW"


# --------------------------------------------------------------------------- #
# (f) FEAT-5 ERP 成本回拉 (inbound cost pull)：FakeAdapter.fetch_actuals 回傳
#     已知 wbs->cost；task_mapping 對應 T-01；pull_actuals_once() 後
#     task_progress.actual_cost 更新且存在 COST_PULL 之 sync_event_log 列。
# --------------------------------------------------------------------------- #
def test_erp_pull_actuals_updates_progress(client, monkeypatch):
    pytest.importorskip("apscheduler")  # worker 模組依賴 (dev 機缺套件時乾淨跳過)

    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import (
        AsyncSession,
        async_sessionmaker,
        create_async_engine,
    )

    import app.erp.adapters as adapters_mod
    import app.erp.worker as worker
    from app.erp.acl import ErpAdapter
    from app.models.orm import ErpConfig, SyncEvent, TaskMapping, TaskProgress

    fake_endpoint = "http://fake-erp.example/api/actuals"
    known_cost = 98765.0
    known_pct = 60
    captured_codes: list[list[str]] = []

    class _FakeActualsAdapter(ErpAdapter):
        """模擬 ERP adapter：fetch_actuals 回傳已知 wbs->cost (不打網路)。"""

        erp_type = "FAKE_ACTUALS"

        async def fetch_actuals(self, wbs_codes):  # noqa: ANN001 - 契約簽章
            captured_codes.append(list(wbs_codes))
            return [
                {
                    "wbs_code": "WBS-T-01",
                    "actual_cost": known_cost,
                    "percent_complete": known_pct,
                }
            ]

    def _fake_get_adapter(erp_type=None, api_endpoint=None, *args, **kwargs):
        return _FakeActualsAdapter(api_endpoint or fake_endpoint)

    # worker 於模組層 `from app.erp.adapters import get_adapter` —— 兩處皆 patch
    # (函式內延遲 import 的實作亦被涵蓋)。
    monkeypatch.setattr(worker, "get_adapter", _fake_get_adapter, raising=False)
    monkeypatch.setattr(adapters_mod, "get_adapter", _fake_get_adapter)

    # 本測試專用 fresh engine/sessionmaker：worker 與 DB 直查全部跑在「同一個」
    # asyncio.run 事件圈，避免跨 loop 重用 aiosqlite 連線。
    engine = create_async_engine(
        _DB_URL,
        future=True,
        connect_args={"check_same_thread": False},
        execution_options={"schema_translate_map": {"erp_integration": None}},
    )
    session_factory = async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )
    monkeypatch.setattr(worker, "WorkerSessionLocal", session_factory)

    async def _scenario() -> None:
        try:
            # --- 安排 (arrange)：啟用帶端點的 ERP 設定 + task_mapping ----------
            async with session_factory() as session:
                async with session.begin():
                    cfg = await session.get(ErpConfig, TENANT_TW)
                    if cfg is None:
                        session.add(
                            ErpConfig(
                                tenant_id=TENANT_TW,
                                erp_type="DINGXIN_TW",
                                api_endpoint=fake_endpoint,
                                is_active=True,
                            )
                        )
                    else:
                        cfg.api_endpoint = fake_endpoint
                        cfg.is_active = True

                    found = (
                        await session.execute(
                            select(TaskMapping).where(
                                TaskMapping.tenant_id == TENANT_TW,
                                TaskMapping.schedule_task_id == "T-01",
                            )
                        )
                    ).scalar_one_or_none()
                    if found is None:
                        session.add(
                            TaskMapping(
                                tenant_id=TENANT_TW,
                                schedule_task_id="T-01",
                                erp_wbs_code="WBS-T-01",
                            )
                        )
                    else:
                        found.erp_wbs_code = "WBS-T-01"

            # --- 執行 (act)：單次成本回拉 -------------------------------------
            await worker.pull_actuals_once()

            # --- 驗證 (assert)：task_progress 已更新 ---------------------------
            async with session_factory() as session:
                rows = (
                    (
                        await session.execute(
                            select(TaskProgress).where(
                                TaskProgress.tenant_id == TENANT_TW,
                                TaskProgress.task_id == "T-01",
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                assert rows, "T-01 應存在 task_progress 列 (種子或回拉 upsert)"
                for row in rows:
                    assert row.actual_cost == pytest.approx(known_cost)
                    assert row.percent_complete == known_pct

                # COST_PULL 之 sync_event_log 列 (每租戶一筆，status=SUCCESS)。
                logs = (
                    (
                        await session.execute(
                            select(SyncEvent).where(
                                SyncEvent.sync_type == "COST_PULL",
                                SyncEvent.tenant_id == TENANT_TW,
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                assert logs, "應寫入 sync_type='COST_PULL' 之 sync_event_log 列"
                success = [log for log in logs if log.status == "SUCCESS"]
                assert success, "COST_PULL 列應為 SUCCESS"
                payload = worker._to_jsonable(success[-1].payload)
                assert int(payload.get("updated", 0)) >= 1
        finally:
            await engine.dispose()

    asyncio.run(_scenario())

    # adapter 確實被以該租戶的 mapping wbs codes 呼叫。
    assert captured_codes, "fetch_actuals 應被呼叫"
    assert any("WBS-T-01" in codes for codes in captured_codes)
