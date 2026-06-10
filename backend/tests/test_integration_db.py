"""DB 整合測試 —— 對「真實 PostgreSQL」執行。

執行條件 (見 conftest.py)：RUN_DB_TESTS=1 且 DATABASE_URL=postgresql+asyncpg://cpm_app:...
否則所有測試被乾淨跳過 (skip)，不影響開發機既有單元套件。

這些測試刻意透過 FastAPI TestClient + X-Tenant-Id 標頭驅動，藉此完整行經：
    verify_tenant -> get_db (AsyncSession + 設定 app.current_tenant RLS GUC)
    -> ORM -> PostgreSQL (含 RLS 政策)。
因此它們同時驗證 CPM 持久化、CRUD 重算、RLS 租戶隔離與 ERP 拋轉 + worker 落地。

重要：RLS 隔離測試 (test_rls_isolation) 之所以能通過，前提是 app/worker 以
「非 superuser 角色 cpm_app」連線 —— superuser 會 BYPASS RLS。若仍以 cpm
(superuser) 連線，跨租戶讀取將「看得到」其他租戶資料而使該測試失敗。

各測試使用「相異 project_id」避免互相碰撞；CI 每次以全新 DB 執行。
"""
from __future__ import annotations

import uuid

import pytest

from tests.conftest import make_project_payload, run_async

pytestmark = pytest.mark.integration

# 種子租戶 (db/init.sql)：TENT-9981 (TW)，且已設定 DINGXIN_TW ERP (空 api_endpoint => 模擬成功)。
TENANT = "TENT-9981"
HEADERS = {"X-Tenant-Id": TENANT, "X-Region": "TW"}

# 一個「不存在 / 不同」的租戶，用於 RLS 讀取隔離 (verify_tenant 不檢查租戶是否存在)。
OUTSIDER = "TENT-OUTSIDER"
OUTSIDER_HEADERS = {"X-Tenant-Id": OUTSIDER, "X-Region": "TW"}

# 建立 / 新增任務端點回傳 201；GET/PUT/DELETE/list 回傳 200；ERP 入列回傳 202。
CREATED_OK = (200, 201)


def _by_id(tasks: list[dict]) -> dict[str, dict]:
    """以 task_id 為鍵索引 ProjectOut.tasks，方便逐任務斷言。"""
    return {t["task_id"]: t for t in tasks}


# --------------------------------------------------------------------------- #
# 1) 建立 + 讀取 (含 list)
# --------------------------------------------------------------------------- #
def test_create_and_get_project(client):
    """POST 建立線性鏈 (5,3,2) -> 總工期 10、全要徑；GET 單筆 / list 一致。"""
    project_id = "PRJ-IT-CRUD"
    payload = make_project_payload(project_id)

    resp = client.post("/api/v1/projects", json=payload, headers=HEADERS)
    assert resp.status_code in CREATED_OK, resp.text
    created = resp.json()
    assert created["project_id"] == project_id
    assert created["tenant_id"] == TENANT
    assert created["project_duration"] == 10, created
    assert len(created["tasks"]) == 3
    for t in created["tasks"]:
        assert t["is_critical"] is True, t

    # GET 單筆：應與建立結果一致 (工期 10、全要徑)。
    resp = client.get(f"/api/v1/projects/{project_id}", headers=HEADERS)
    assert resp.status_code == 200, resp.text
    fetched = resp.json()
    assert fetched["project_id"] == project_id
    assert fetched["project_duration"] == 10
    assert {t["task_id"] for t in fetched["tasks"]} == {"T-01", "T-02", "T-03"}
    for t in fetched["tasks"]:
        assert t["is_critical"] is True, t

    # list：應包含剛建立的專案。
    resp = client.get("/api/v1/projects", headers=HEADERS)
    assert resp.status_code == 200, resp.text
    summaries = resp.json()
    assert any(s["project_id"] == project_id for s in summaries), summaries


# --------------------------------------------------------------------------- #
# 2) 改工期 -> 重算
# --------------------------------------------------------------------------- #
def test_update_duration_recalc(client):
    """PUT .../tasks/T-02/duration {10} -> 總工期 5+10+2=17，仍全要徑。"""
    project_id = "PRJ-IT-DUR"
    resp = client.post("/api/v1/projects", json=make_project_payload(project_id), headers=HEADERS)
    assert resp.status_code in CREATED_OK, resp.text

    resp = client.put(
        f"/api/v1/projects/{project_id}/tasks/T-02/duration",
        json={"duration": 10},
        headers=HEADERS,
    )
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["project_duration"] == 17, out
    for t in out["tasks"]:
        assert t["is_critical"] is True, t
    assert _by_id(out["tasks"])["T-02"]["duration"] == 10


# --------------------------------------------------------------------------- #
# 3) 新增任務 -> 重算 (任務數增加，總工期延長)
# --------------------------------------------------------------------------- #
def test_add_task_recalc(client):
    """POST 新增 T-04(4, 接在 T-03 後) -> 任務數 4，總工期 10+4=14。"""
    project_id = "PRJ-IT-ADD"
    resp = client.post("/api/v1/projects", json=make_project_payload(project_id), headers=HEADERS)
    assert resp.status_code in CREATED_OK, resp.text
    assert len(resp.json()["tasks"]) == 3

    resp = client.post(
        f"/api/v1/projects/{project_id}/tasks",
        json={
            "task_id": "T-04",
            "task_name": "二樓鋼筋綁紮",
            "duration": 4,
            "predecessors": ["T-03"],
            "status": "PENDING",
        },
        headers=HEADERS,
    )
    assert resp.status_code in CREATED_OK, resp.text
    out = resp.json()
    assert len(out["tasks"]) == 4, out
    assert out["project_duration"] == 14, out
    assert _by_id(out["tasks"])["T-04"]["is_critical"] is True


# --------------------------------------------------------------------------- #
# 4) 刪除任務 -> 重算 (任務數減少，總工期縮短)
# --------------------------------------------------------------------------- #
def test_delete_task_recalc(client):
    """DELETE T-03 -> 任務數 2，剩 T-01(5)->T-02(3)，總工期 8。"""
    project_id = "PRJ-IT-DEL"
    resp = client.post("/api/v1/projects", json=make_project_payload(project_id), headers=HEADERS)
    assert resp.status_code in CREATED_OK, resp.text
    assert len(resp.json()["tasks"]) == 3

    resp = client.delete(f"/api/v1/projects/{project_id}/tasks/T-03", headers=HEADERS)
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert len(out["tasks"]) == 2, out
    assert {t["task_id"] for t in out["tasks"]} == {"T-01", "T-02"}
    assert out["project_duration"] == 8, out

    # 確認刪除已落地：GET 也只剩 2 筆。
    resp = client.get(f"/api/v1/projects/{project_id}", headers=HEADERS)
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["tasks"]) == 2


# --------------------------------------------------------------------------- #
# 5) RLS 租戶隔離 (核心安全屬性)
# --------------------------------------------------------------------------- #
def test_rls_isolation(client):
    """跨租戶讀取必須被 RLS 阻擋 —— 唯有 app 以非 superuser (cpm_app) 連線時才成立。

    以 TENT-9981 建立 PRJ-IT-RLS 後：
      - 以 TENT-OUTSIDER 查 list：不得出現 PRJ-IT-RLS。
      - 以 TENT-OUTSIDER 查單筆：必須 404 (RLS 過濾後查無此列)。
    若 app 仍以 superuser (cpm) 連線，RLS 會被 BYPASS，跨租戶會看得到資料而失敗。
    """
    project_id = "PRJ-IT-RLS"
    resp = client.post("/api/v1/projects", json=make_project_payload(project_id), headers=HEADERS)
    assert resp.status_code in CREATED_OK, resp.text

    # 擁有者租戶仍看得到 (健全性檢查)。
    resp = client.get(f"/api/v1/projects/{project_id}", headers=HEADERS)
    assert resp.status_code == 200, resp.text

    # 外部租戶的 list：不得包含此專案。
    resp = client.get("/api/v1/projects", headers=OUTSIDER_HEADERS)
    assert resp.status_code == 200, resp.text
    outsider_ids = {s["project_id"] for s in resp.json()}
    assert project_id not in outsider_ids, (
        f"RLS 隔離失敗：TENT-OUTSIDER 看見了 {project_id}；"
        f"app 可能仍以 superuser 連線 (RLS 被 BYPASS)。list={outsider_ids}"
    )

    # 外部租戶查單筆：RLS 過濾後查無此列 -> 404。
    resp = client.get(f"/api/v1/projects/{project_id}", headers=OUTSIDER_HEADERS)
    assert resp.status_code == 404, (
        f"RLS 隔離失敗：TENT-OUTSIDER 取得 {project_id} (status={resp.status_code})；"
        f"預期 404。app 可能仍以 superuser 連線。body={resp.text}"
    )


# --------------------------------------------------------------------------- #
# 6) ERP 拋轉入列 + worker 落地
# --------------------------------------------------------------------------- #
def test_erp_enqueue_and_worker(client):
    """POST .../erp/sync 入列 >=1 筆 PENDING；scan_once() 後至少一筆 SUCCESS。

    種子 TENT-9981 已設定 DINGXIN_TW 且 api_endpoint 為空 => worker 走模擬推送成功。
    erp_integration.* 無 RLS，故 worker 與這裡的 SyncEvent 查詢皆不需設定 GUC。
    """
    project_id = "PRJ-IT-ERP"
    resp = client.post("/api/v1/projects", json=make_project_payload(project_id), headers=HEADERS)
    assert resp.status_code in CREATED_OK, resp.text

    # 入列：每個任務一筆 PENDING 事件 (回傳 202 ACCEPTED)。
    resp = client.post(
        f"/api/v1/projects/{project_id}/erp/sync",
        json={"sync_type": "SCHEDULE_PUSH"},
        headers=HEADERS,
    )
    assert resp.status_code == 202, resp.text
    enqueue = resp.json()
    assert enqueue["enqueued"] >= 1, enqueue
    event_ids = set(enqueue.get("event_ids", []))
    assert event_ids, enqueue

    # 延後匯入需要真實 DB 的模組 (worker 在匯入時即建立 asyncpg engine；
    # 開發機無 asyncpg/apscheduler，故僅在整合測試執行時於函式內匯入)。
    from app.erp.worker import scan_once
    from app.database import SessionLocal
    from app.models.orm import SyncEvent
    from sqlalchemy import select

    # 跑一次掃描：worker 以 cpm_app 自有 session 跨租戶掃描 erp_integration。
    stats = run_async(scan_once())
    assert stats["processed"] >= 1, stats
    assert stats["success"] >= 1, stats

    # 直接查 sync_event_log：本租戶 + 本次入列的事件，至少一筆 status=SUCCESS。
    async def _count_success() -> int:
        async with SessionLocal() as session:
            result = await session.execute(
                select(SyncEvent).where(
                    SyncEvent.tenant_id == TENANT,
                    SyncEvent.event_id.in_([uuid.UUID(e) for e in event_ids]),
                    SyncEvent.status == "SUCCESS",
                )
            )
            return len(list(result.scalars().all()))

    success_count = run_async(_count_success())
    assert success_count >= 1, (
        f"預期本次入列事件至少一筆 SUCCESS，實得 {success_count}；event_ids={event_ids}"
    )
