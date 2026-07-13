"""WBS 階層 + 多重命名基準線測試 (sqlite TestClient pattern) —— 不需 Postgres。

Pro Batch B / FEATURE 1（WBS 階層）+ FEATURE 3（多重命名基準線）：

WBS
---
  GET  /projects/{pid}/wbs   回傳扁平（flat）WBS 節點清單
                              [{wbs_code, name, parent_code, sort_order}]。
  PUT  /projects/{pid}/wbs   （editor+）以扁平清單整批取代（replace-all upsert）；
                              驗證：代碼唯一、parent_code 需指向清單中的代碼或為
                              null、不可有循環（cycle）—— 違反者 422。
  任務（TaskCreate/TaskUpdate/ProjectCreate.schedule_data）可帶選填
  wbs_code；即使該代碼不在專案 WBS 清單中仍允許（import-friendly，允許懸空
  / dangling）。ProjectOut 額外回傳 wbs（扁平清單）供前端建樹。

多重命名基準線（multiple named baselines）
-------------------------------------------
  POST /projects/{pid}/baseline                建立新基準線（可選 name），
                                                 自動設為作用中（is_active=true）
                                                 並清除其餘基準線的 is_active。
  GET  /projects/{pid}/baseline                 回傳「作用中」基準線
                                                 （無旗標時退回最新者，向下相容）。
  GET  /projects/{pid}/baselines                列出所有基準線摘要
                                                 [{id,name,created_at,is_active,
                                                 project_duration}]。
  GET  /projects/{pid}/baselines/{bid}          單一基準線完整內容（BaselineOut）。
  POST /projects/{pid}/baselines/{bid}/activate 將指定基準線設為作用中
                                                 （並清除其餘）。
  DELETE /projects/{pid}/baselines/{bid}        刪除指定基準線；若刪除的是作用中
                                                 基準線，自動將剩餘中最新者設為
                                                 作用中（若有）；不存在 -> 404。

關鍵設計 (與 test_auth.py / test_product_batch3.py 完全一致)
------------------------------------------------------------
1. 「import app 之前」即設定 DATABASE_URL=sqlite 檔案 + DEV_BOOTSTRAP=1。
2. client fixture 以 _rebind_sqlite_engine() 將整個 DB 層重綁至本檔 sqlite 檔，
   teardown 時 dispose engine、還原全域綁定、再刪暫存檔 (Windows 句柄安全)。
"""
from __future__ import annotations

import os
import tempfile

# --------------------------------------------------------------------------- #
# 於匯入 app 之前先設定環境變數 (env at top before importing app)。
# 使用具名暫存「檔案」作為 sqlite DB，確保跨連線共享資料且測試可重現。
# --------------------------------------------------------------------------- #
_DB_FD, _DB_PATH = tempfile.mkstemp(prefix="cpm_wbs_baseline_test_", suffix=".db")
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

# openpyxl 產生的 xlsx 正式 MIME 型別 (與 test_dashboard_export.py 一致)。
XLSX_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)


# --------------------------------------------------------------------------- #
# sqlite rebind / teardown 工具 (與 test_auth.py / test_product_batch3.py 同法)
# --------------------------------------------------------------------------- #
def _rebind_sqlite_engine() -> None:
    """把 app 的 DB 層強制指向本檔的 sqlite 暫存檔。"""
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
        "project_name": f"WBS/基準線測試專案 {project_id}",
        "region": "TW",
        "schedule_data": _chain_schedule() if schedule_data is None else schedule_data,
    }
    payload.update(extra)
    resp = client.post(PROJECTS_URL, headers=headers, json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _assert_same_wbs_set(actual: list[dict], expected: list[dict]) -> None:
    """比對兩份 WBS 扁平清單內容相同 (忽略順序)。"""
    assert len(actual) == len(expected), actual
    by_code = {row["wbs_code"]: row for row in actual}
    assert set(by_code) == {row["wbs_code"] for row in expected}
    for row in expected:
        got_row = by_code[row["wbs_code"]]
        assert got_row["name"] == row["name"]
        assert got_row["parent_code"] == row["parent_code"]
        assert got_row["sort_order"] == row["sort_order"]


# =============================================================================
# WBS 階層 (FEATURE 1)
# =============================================================================

# --------------------------------------------------------------------------- #
# PUT 2 父節點 + 1 子節點 -> GET 往返 (round-trip) 內容相同。
# --------------------------------------------------------------------------- #
def test_wbs_put_get_round_trip(client):
    headers = _admin_headers(client)
    pid = "PRJ-WBS-ROUNDTRIP"
    _create_project(client, headers, pid, schedule_data=[])

    payload = [
        {"wbs_code": "1", "name": "土建工程", "parent_code": None, "sort_order": 0},
        {"wbs_code": "2", "name": "機電工程", "parent_code": None, "sort_order": 1},
        {"wbs_code": "1.1", "name": "基礎開挖", "parent_code": "1", "sort_order": 0},
    ]

    put_resp = client.put(f"{PROJECTS_URL}/{pid}/wbs", headers=headers, json=payload)
    assert put_resp.status_code == 200, put_resp.text
    _assert_same_wbs_set(put_resp.json(), payload)

    got = client.get(f"{PROJECTS_URL}/{pid}/wbs", headers=headers)
    assert got.status_code == 200, got.text
    _assert_same_wbs_set(got.json(), payload)


# --------------------------------------------------------------------------- #
# parent_code 形成循環 (cycle) -> 422；WBS 清單保持不變 (未寫入)。
# --------------------------------------------------------------------------- #
def test_wbs_cycle_in_parent_code_rejected_422(client):
    headers = _admin_headers(client)
    pid = "PRJ-WBS-CYCLE"
    _create_project(client, headers, pid, schedule_data=[])

    payload = [
        {"wbs_code": "X", "name": "X 項", "parent_code": "Y", "sort_order": 0},
        {"wbs_code": "Y", "name": "Y 項", "parent_code": "X", "sort_order": 1},
    ]
    resp = client.put(f"{PROJECTS_URL}/{pid}/wbs", headers=headers, json=payload)
    assert resp.status_code == 422, resp.text

    got = client.get(f"{PROJECTS_URL}/{pid}/wbs", headers=headers)
    assert got.status_code == 200, got.text
    assert got.json() == []


# --------------------------------------------------------------------------- #
# parent_code 未指向清單中任一代碼 (亦非 null) -> 422。
# --------------------------------------------------------------------------- #
def test_wbs_dangling_parent_code_rejected_422(client):
    headers = _admin_headers(client)
    pid = "PRJ-WBS-DANGLE"
    _create_project(client, headers, pid, schedule_data=[])

    payload = [
        {"wbs_code": "1", "name": "土建工程", "parent_code": "NO-SUCH-CODE",
         "sort_order": 0},
    ]
    resp = client.put(f"{PROJECTS_URL}/{pid}/wbs", headers=headers, json=payload)
    assert resp.status_code == 422, resp.text


# --------------------------------------------------------------------------- #
# 任務帶 wbs_code -> ProjectOut.tasks[].wbs_code 往返 + ProjectOut.wbs 清單；
# 未在 WBS 清單中的 wbs_code (懸空 / dangling) 仍允許 (import-friendly)。
# --------------------------------------------------------------------------- #
def test_task_wbs_code_round_trip_and_project_out_wbs_list(client):
    headers = _admin_headers(client)
    pid = "PRJ-WBS-TASK"
    _create_project(client, headers, pid, schedule_data=[])

    wbs_payload = [
        {"wbs_code": "1", "name": "土建工程", "parent_code": None, "sort_order": 0},
        {"wbs_code": "1.1", "name": "基礎開挖", "parent_code": "1", "sort_order": 0},
    ]
    put_resp = client.put(
        f"{PROJECTS_URL}/{pid}/wbs", headers=headers, json=wbs_payload
    )
    assert put_resp.status_code == 200, put_resp.text

    created = client.post(
        f"{PROJECTS_URL}/{pid}/tasks",
        headers=headers,
        json={
            "task_id": "T-01",
            "task_name": "基礎開挖工作",
            "duration": 5,
            "wbs_code": "1.1",
        },
    )
    assert created.status_code == 201, created.text
    body = created.json()
    tasks_by_id = {t["task_id"]: t for t in body["tasks"]}
    assert tasks_by_id["T-01"]["wbs_code"] == "1.1"
    _assert_same_wbs_set(body["wbs"], wbs_payload)

    # 懸空 wbs_code (未在 WBS 清單中) 仍允許建立 (import-friendly；前端歸類「未分類」)。
    created2 = client.post(
        f"{PROJECTS_URL}/{pid}/tasks",
        headers=headers,
        json={
            "task_id": "T-02",
            "task_name": "未分類任務",
            "duration": 2,
            "wbs_code": "9.9",
        },
    )
    assert created2.status_code == 201, created2.text
    tasks_by_id2 = {t["task_id"]: t for t in created2.json()["tasks"]}
    assert tasks_by_id2["T-02"]["wbs_code"] == "9.9"

    # GET 重讀：wbs_code 持久化往返 + wbs 清單一致。
    got = client.get(f"{PROJECTS_URL}/{pid}", headers=headers)
    assert got.status_code == 200, got.text
    got_body = got.json()
    got_tasks = {t["task_id"]: t for t in got_body["tasks"]}
    assert got_tasks["T-01"]["wbs_code"] == "1.1"
    assert got_tasks["T-02"]["wbs_code"] == "9.9"
    _assert_same_wbs_set(got_body["wbs"], wbs_payload)


# --------------------------------------------------------------------------- #
# WBS 設定後 export.xlsx 仍 200 (ZIP magic "PK")。
# --------------------------------------------------------------------------- #
def test_export_xlsx_200_after_wbs_set(client):
    headers = _admin_headers(client)
    pid = "PRJ-WBS-XLSX"
    _create_project(
        client,
        headers,
        pid,
        schedule_data=[
            {"task_id": "T-01", "task_name": "基礎開挖", "duration": 5,
             "predecessors": [], "status": "PENDING", "wbs_code": "1.1"},
        ],
    )

    wbs_payload = [
        {"wbs_code": "1", "name": "土建工程", "parent_code": None, "sort_order": 0},
        {"wbs_code": "1.1", "name": "基礎開挖", "parent_code": "1", "sort_order": 0},
    ]
    put_resp = client.put(
        f"{PROJECTS_URL}/{pid}/wbs", headers=headers, json=wbs_payload
    )
    assert put_resp.status_code == 200, put_resp.text

    resp = client.get(f"{PROJECTS_URL}/{pid}/export.xlsx", headers=headers)
    assert resp.status_code == 200, resp.text
    content_type = resp.headers.get("content-type", "")
    assert XLSX_MEDIA_TYPE in content_type, f"非預期 content-type: {content_type}"
    body = resp.content
    assert body, "xlsx body 不應為空"
    assert body[:2] == b"PK", f"xlsx body 應以 ZIP magic PK 起始: {body[:8]!r}"


# =============================================================================
# 多重命名基準線 (FEATURE 3)
# =============================================================================

# --------------------------------------------------------------------------- #
# 建立兩條具名基準線 -> GET /baselines 顯示 2 條、第二條為作用中 ->
# 啟用第一條 -> GET /baseline 回傳第一條名稱 -> EVM 對其執行 ->
# 刪除作用中基準線 -> 剩餘基準線自動成為作用中。
# --------------------------------------------------------------------------- #
def test_baselines_list_activate_get_evm_and_delete_auto_activates_remaining(client):
    headers = _admin_headers(client)
    pid = "PRJ-BASELINES"
    _create_project(client, headers, pid)  # 預設 5/3/2 鏈, 總工期 10

    first = client.post(
        f"{PROJECTS_URL}/{pid}/baseline", headers=headers, json={"name": "first"}
    )
    assert first.status_code == 200, first.text
    first_id = first.json()["id"]

    second = client.post(
        f"{PROJECTS_URL}/{pid}/baseline", headers=headers, json={"name": "second"}
    )
    assert second.status_code == 200, second.text
    second_id = second.json()["id"]
    assert second_id != first_id

    # 建立第二條後 -> GET /baselines 顯示 2 條，第二條 (最新建立) 為作用中。
    listing = client.get(f"{PROJECTS_URL}/{pid}/baselines", headers=headers)
    assert listing.status_code == 200, listing.text
    rows = listing.json()
    assert len(rows) == 2
    by_id = {r["id"]: r for r in rows}
    assert by_id[first_id]["name"] == "first"
    assert by_id[second_id]["name"] == "second"
    assert by_id[first_id]["is_active"] is False
    assert by_id[second_id]["is_active"] is True

    # GET /baseline (單數，作用中) -> "second"。
    active = client.get(f"{PROJECTS_URL}/{pid}/baseline", headers=headers)
    assert active.status_code == 200, active.text
    assert active.json()["name"] == "second"
    assert active.json()["id"] == second_id

    # 啟用 first -> 取代作用中基準線。
    activate = client.post(
        f"{PROJECTS_URL}/{pid}/baselines/{first_id}/activate", headers=headers
    )
    assert activate.status_code < 300, activate.text

    active_after = client.get(f"{PROJECTS_URL}/{pid}/baseline", headers=headers)
    assert active_after.status_code == 200, active_after.text
    assert active_after.json()["name"] == "first"
    assert active_after.json()["id"] == first_id

    listing2 = client.get(f"{PROJECTS_URL}/{pid}/baselines", headers=headers)
    assert listing2.status_code == 200, listing2.text
    rows2 = {r["id"]: r for r in listing2.json()}
    assert rows2[first_id]["is_active"] is True
    assert rows2[second_id]["is_active"] is False

    # GET 單一基準線 -> 完整 BaselineOut (含 tasks 快照)。
    single = client.get(
        f"{PROJECTS_URL}/{pid}/baselines/{second_id}", headers=headers
    )
    assert single.status_code == 200, single.text
    single_body = single.json()
    assert single_body["id"] == second_id
    assert single_body["name"] == "second"
    assert isinstance(single_body["tasks"], list)
    assert single_body["project_duration"] == 10

    # EVM 對目前作用中基準線 (first) 執行 -> 200 (唯讀, 不需先前寫入進度)。
    evm = client.get(f"{PROJECTS_URL}/{pid}/evm", headers=headers)
    assert evm.status_code == 200, evm.text
    assert evm.json()["data_date"] == 10

    # 刪除目前作用中基準線 (first) -> 剩餘 (second) 自動成為作用中。
    deleted = client.delete(
        f"{PROJECTS_URL}/{pid}/baselines/{first_id}", headers=headers
    )
    assert deleted.status_code < 300, deleted.text

    listing3 = client.get(f"{PROJECTS_URL}/{pid}/baselines", headers=headers)
    assert listing3.status_code == 200, listing3.text
    rows3 = listing3.json()
    assert len(rows3) == 1
    assert rows3[0]["id"] == second_id
    assert rows3[0]["is_active"] is True

    active_final = client.get(f"{PROJECTS_URL}/{pid}/baseline", headers=headers)
    assert active_final.status_code == 200, active_final.text
    assert active_final.json()["name"] == "second"

    # 刪除不存在的基準線 -> 404。
    missing = client.delete(
        f"{PROJECTS_URL}/{pid}/baselines/9999999", headers=headers
    )
    assert missing.status_code == 404, missing.text
