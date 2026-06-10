"""API 整合測試 (FastAPI endpoint tests).

聚焦於無狀態 (stateless) 的要徑計算端點：
    POST {API_V1_PREFIX}/schedule/calculate
該端點不需資料庫，只需 X-Tenant-Id 標頭即可運作，
回傳 list[TaskResult]，並標記要徑 (is_critical) 與寬裕時間 (float_time)。

以樣本 T-01..T-03 線性鏈為輸入，預期三個任務全部位於要徑。
"""

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app

# 端點前綴 (預設 /api/v1)，由設定檔提供
PREFIX = settings.api_v1_prefix
CALC_URL = f"{PREFIX}/schedule/calculate"

# 所有業務端點必備的多租戶標頭 (multi-tenant header)
TENANT_HEADERS = {"X-Tenant-Id": "TENT-9981", "X-Region": "TW"}

# 樣本相依鏈：T-01 -> T-02 -> T-03 (3 + 5 + 2 = 10 天，全要徑)
SAMPLE_PAYLOAD = [
    {"task_id": "T-01", "task_name": "基礎開挖", "duration": 3, "predecessors": []},
    {"task_id": "T-02", "task_name": "結構施作", "duration": 5, "predecessors": ["T-01"]},
    {"task_id": "T-03", "task_name": "機電安裝", "duration": 2, "predecessors": ["T-02"]},
]


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def test_health_ok(client):
    """/health 應回傳 status ok。"""
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_calculate_returns_results_for_all_tasks(client):
    resp = client.post(CALC_URL, json=SAMPLE_PAYLOAD, headers=TENANT_HEADERS)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) == 3
    assert {t["task_id"] for t in data} == {"T-01", "T-02", "T-03"}


def test_calculate_marks_all_tasks_critical(client):
    """線性鏈上三個任務皆應 is_critical=True 且 float_time=0。"""
    resp = client.post(CALC_URL, json=SAMPLE_PAYLOAD, headers=TENANT_HEADERS)
    assert resp.status_code == 200, resp.text
    by_id = {t["task_id"]: t for t in resp.json()}

    for task_id in ("T-01", "T-02", "T-03"):
        assert by_id[task_id]["is_critical"] is True, f"{task_id} 應為要徑"
        assert by_id[task_id]["float_time"] == 0, f"{task_id} 寬裕時間應為 0"


def test_calculate_es_ef_values(client):
    """驗證 forward pass 的 ES/EF；末任務 EF=10 即專案總工期。"""
    resp = client.post(CALC_URL, json=SAMPLE_PAYLOAD, headers=TENANT_HEADERS)
    assert resp.status_code == 200, resp.text
    by_id = {t["task_id"]: t for t in resp.json()}

    assert by_id["T-01"]["es"] == 0 and by_id["T-01"]["ef"] == 3
    assert by_id["T-02"]["es"] == 3 and by_id["T-02"]["ef"] == 8
    assert by_id["T-03"]["es"] == 8 and by_id["T-03"]["ef"] == 10


def test_calculate_parallel_branch_float(client):
    """並行分支：較短分支任務應為非要徑且具正 float。"""
    payload = [
        {"task_id": "T-A", "duration": 5, "predecessors": []},
        {"task_id": "T-B", "duration": 2, "predecessors": []},
        {"task_id": "T-C", "duration": 1, "predecessors": ["T-B"]},
        {"task_id": "T-D", "duration": 2, "predecessors": ["T-A", "T-C"]},
    ]
    resp = client.post(CALC_URL, json=payload, headers=TENANT_HEADERS)
    assert resp.status_code == 200, resp.text
    by_id = {t["task_id"]: t for t in resp.json()}

    assert by_id["T-A"]["is_critical"] is True
    assert by_id["T-D"]["is_critical"] is True
    assert by_id["T-C"]["is_critical"] is False
    assert by_id["T-C"]["float_time"] > 0


def test_calculate_empty_list_returns_empty(client):
    """空任務清單應回傳空陣列。"""
    resp = client.post(CALC_URL, json=[], headers=TENANT_HEADERS)
    assert resp.status_code == 200, resp.text
    assert resp.json() == []


def test_calculate_requires_tenant_header(client):
    """缺少 X-Tenant-Id 標頭應被拒絕 (4xx)。"""
    resp = client.post(CALC_URL, json=SAMPLE_PAYLOAD)
    assert resp.status_code >= 400


def test_calculate_cycle_rejected(client):
    """相依環應回傳客戶端錯誤 (422 Unprocessable Entity)，而非 500。"""
    payload = [
        {"task_id": "T-01", "duration": 3, "predecessors": ["T-03"]},
        {"task_id": "T-02", "duration": 5, "predecessors": ["T-01"]},
        {"task_id": "T-03", "duration": 2, "predecessors": ["T-02"]},
    ]
    resp = client.post(CALC_URL, json=payload, headers=TENANT_HEADERS)
    assert resp.status_code == 422, resp.text
