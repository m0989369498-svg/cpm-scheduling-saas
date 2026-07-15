"""投資組合資源分配 (portfolio resource allocation) 純函式測試。Pro Batch E (FEATURE E1)。

app.core.portfolio.compute_resource_allocation(projects, pool) -> ResourceAllocationResult

涵蓋:
  * 週別 ISO bucketing 是否正確 (isocalendar 週標籤)。
  * PEAK-not-sum：同一週內若逐日需求有高有低，取「峰值」而非「加總」。
  * over_weeks：週峰值需求 > capacity 才觸發。
  * 未設定 start_date 但有資源需求的專案 -> unscheduled_projects + warning。
  * pool 未列出的資源 (capacity 預設 0) -> 有需求即超載。
  * 空輸入 -> 空結果 (不例外)。
  * 兩專案任務在同一 ISO 週重疊：手動驗算峰值 bucketing (frozen adversarial check)。
"""

from __future__ import annotations

from datetime import date

from app.core.portfolio import compute_resource_allocation
from app.schemas.enterprise import TenantResource


def _pool(*rows: tuple[str, int, float, str]) -> list[TenantResource]:
    """(resource_type, capacity, unit_cost, category) -> list[TenantResource]。"""
    return [
        TenantResource(
            resource_type=rtype, name=rtype, category=category,
            capacity=capacity, unit_cost=unit_cost,
        )
        for rtype, capacity, unit_cost, category in rows
    ]


# --------------------------------------------------------------------------- #
# 基本 ISO 週 bucketing + peak-not-sum
# --------------------------------------------------------------------------- #
def test_weekly_iso_bucketing_and_peak_not_sum():
    """單一專案、單一資源，5 個工作日 (同一 ISO 週) 需求量遞增 1..5 ->
    週峰值應為 5 (最後一天)，而非加總 (15)。"""
    # 2026-07-06 為週一 (ISO week 2026-W28)。
    project = {
        "project_id": "P1",
        "start_date": date(2026, 7, 6),
        "work_days": "1111100",
        "holidays": set(),
        "tasks": [
            {"task_id": "T1", "es": 0, "ef": 1, "demands": {"crane": 1}},
            {"task_id": "T2", "es": 1, "ef": 2, "demands": {"crane": 2}},
            {"task_id": "T3", "es": 2, "ef": 3, "demands": {"crane": 3}},
            {"task_id": "T4", "es": 3, "ef": 4, "demands": {"crane": 4}},
            {"task_id": "T5", "es": 4, "ef": 5, "demands": {"crane": 5}},
        ],
    }
    pool = _pool(("crane", 10, 3000.0, "equipment"))
    result = compute_resource_allocation([project], pool)

    assert result.weeks == ["2026-W28"]
    row = result.resources[0]
    assert row.resource_type == "crane"
    assert row.by_week == {"2026-W28": 5}  # peak, 不是 1+2+3+4+5=15
    assert row.peak == 5
    assert row.over_weeks == []  # 5 <= capacity 10


def test_over_weeks_when_peak_exceeds_capacity():
    project = {
        "project_id": "P1",
        "start_date": date(2026, 7, 6),
        "work_days": "1111100",
        "holidays": set(),
        "tasks": [
            {"task_id": "T1", "es": 0, "ef": 1, "demands": {"crane": 2}},
        ],
    }
    pool = _pool(("crane", 1, 3000.0, "equipment"))
    result = compute_resource_allocation([project], pool)
    row = result.resources[0]
    assert row.peak == 2
    assert row.over_weeks == ["2026-W28"]


# --------------------------------------------------------------------------- #
# 兩專案任務重疊於同一 ISO 週：手動驗算峰值 bucketing (frozen adversarial check)
# --------------------------------------------------------------------------- #
def test_two_projects_overlap_same_iso_week_peak_bucketing():
    """兩專案的任務在同一 ISO 週重疊：
      P1: 2026-07-06(Mon) 需求 crane=1, 2026-07-07(Tue) 需求 crane=1
      P2: 2026-07-07(Tue) 需求 crane=2, 2026-07-08(Wed) 需求 crane=1
    逐日加總：Mon=1, Tue=1+2=3, Wed=1。
    capacity=2 -> 週峰值=3 (Tue) > capacity -> over_weeks 該週應出現 (peak 觸發，
    而非任一單日單獨判定)；capacity=3 -> 恰好打平，不應觸發 over_weeks。
    """
    p1 = {
        "project_id": "P1",
        "start_date": date(2026, 7, 6),  # Mon
        "work_days": "1111100",
        "holidays": set(),
        "tasks": [
            {"task_id": "T1", "es": 0, "ef": 2, "demands": {"crane": 1}},  # Mon,Tue
        ],
    }
    p2 = {
        "project_id": "P2",
        "start_date": date(2026, 7, 7),  # Tue
        "work_days": "1111100",
        "holidays": set(),
        "tasks": [
            {"task_id": "T2", "es": 0, "ef": 2, "demands": {"crane": 2}},  # Tue,Wed
        ],
    }
    pool_low = _pool(("crane", 2, 3000.0, "equipment"))
    result_low = compute_resource_allocation([p1, p2], pool_low)
    row_low = result_low.resources[0]
    assert row_low.by_week == {"2026-W28": 3}  # peak day = Tue: 1(P1) + 2(P2) = 3
    assert row_low.peak == 3
    assert row_low.over_weeks == ["2026-W28"]  # 3 > capacity(2)

    pool_ok = _pool(("crane", 3, 3000.0, "equipment"))
    result_ok = compute_resource_allocation([p1, p2], pool_ok)
    row_ok = result_ok.resources[0]
    assert row_ok.peak == 3
    assert row_ok.over_weeks == []  # 3 <= capacity(3) -> 不觸發


# --------------------------------------------------------------------------- #
# unscheduled_projects
# --------------------------------------------------------------------------- #
def test_unscheduled_project_without_start_date_flagged():
    scheduled = {
        "project_id": "P-SCHEDULED",
        "start_date": date(2026, 7, 6),
        "work_days": "1111100",
        "holidays": set(),
        "tasks": [{"task_id": "T1", "es": 0, "ef": 1, "demands": {"crane": 1}}],
    }
    unscheduled = {
        "project_id": "P-NO-START",
        "start_date": None,
        "work_days": "1111100",
        "holidays": set(),
        "tasks": [{"task_id": "T2", "es": 0, "ef": 3, "demands": {"manpower": 5}}],
    }
    pool = _pool(("crane", 5, 1000.0, "equipment"), ("manpower", 20, 200.0, "labor"))
    result = compute_resource_allocation([scheduled, unscheduled], pool)

    assert result.unscheduled_projects == ["P-NO-START"]
    assert any("P-NO-START" in w for w in result.warnings)
    # 未排程專案不貢獻 manpower 的 by_week 需求。
    manpower_row = next(r for r in result.resources if r.resource_type == "manpower")
    assert manpower_row.by_week == {}
    assert manpower_row.peak == 0


def test_project_without_start_date_and_no_demand_not_flagged():
    """未設定 start_date 但「無任何資源需求」的專案 -> 不應被列為 unscheduled
    (無需求即無法排入時間軸的意義，不構成警告噪音)。"""
    project = {
        "project_id": "P-EMPTY",
        "start_date": None,
        "work_days": "1111100",
        "holidays": set(),
        "tasks": [{"task_id": "T1", "es": 0, "ef": 2, "demands": {}}],
    }
    result = compute_resource_allocation([project], [])
    assert result.unscheduled_projects == []
    assert result.warnings == []


# --------------------------------------------------------------------------- #
# 需求資源不在資源池 (capacity 預設 0 -> 有需求即超載)
# --------------------------------------------------------------------------- #
def test_demanded_resource_not_in_pool_defaults_capacity_zero_and_over():
    project = {
        "project_id": "P1",
        "start_date": date(2026, 7, 6),
        "work_days": "1111100",
        "holidays": set(),
        "tasks": [
            {"task_id": "T1", "es": 0, "ef": 1, "demands": {"welder": 1}},
        ],
    }
    result = compute_resource_allocation([project], [])  # 空資源池
    row = next(r for r in result.resources if r.resource_type == "welder")
    assert row.capacity == 0
    assert row.name == "welder"  # 預設 name = resource_type
    assert row.category == "labor"  # 預設 category
    assert row.peak == 1
    assert row.over_weeks == ["2026-W28"]  # 1 > capacity(0)


def test_pool_resource_with_no_demand_appears_with_empty_by_week():
    """資源池內有資源但當前無任何任務需求 -> 仍應出現在 resources 清單
    (by_week 為空、peak=0、over_weeks 為空)。"""
    pool = _pool(("idle_crane", 3, 1500.0, "equipment"))
    result = compute_resource_allocation([], pool)
    assert len(result.resources) == 1
    row = result.resources[0]
    assert row.resource_type == "idle_crane"
    assert row.capacity == 3
    assert row.by_week == {}
    assert row.peak == 0
    assert row.over_weeks == []
    assert result.weeks == []


# --------------------------------------------------------------------------- #
# duration 0 / ef<=es 不貢獻需求
# --------------------------------------------------------------------------- #
def test_zero_duration_task_contributes_nothing():
    project = {
        "project_id": "P1",
        "start_date": date(2026, 7, 6),
        "work_days": "1111100",
        "holidays": set(),
        "tasks": [
            {"task_id": "MILESTONE", "es": 3, "ef": 3, "demands": {"crane": 5}},
        ],
    }
    pool = _pool(("crane", 1, 1000.0, "equipment"))
    result = compute_resource_allocation([project], pool)
    row = result.resources[0]
    assert row.by_week == {}
    assert row.peak == 0
    assert row.over_weeks == []


# --------------------------------------------------------------------------- #
# 空輸入
# --------------------------------------------------------------------------- #
def test_empty_projects_and_pool_returns_empty_result():
    result = compute_resource_allocation([], [])
    assert result.weeks == []
    assert result.resources == []
    assert result.unscheduled_projects == []
    assert result.warnings == []


def test_pool_accepts_plain_dict_form():
    """pool 亦可為 dict[resource_type -> info] (非 list[TenantResource])。"""
    project = {
        "project_id": "P1",
        "start_date": date(2026, 7, 6),
        "work_days": "1111100",
        "holidays": set(),
        "tasks": [{"task_id": "T1", "es": 0, "ef": 1, "demands": {"crane": 1}}],
    }
    pool = {"crane": {"capacity": 2, "name": "吊車", "category": "equipment", "unit_cost": 3000.0}}
    result = compute_resource_allocation([project], pool)
    row = result.resources[0]
    assert row.name == "吊車"
    assert row.capacity == 2
    assert row.unit_cost == 3000.0


# --------------------------------------------------------------------------- #
# Deterministic 排序
# --------------------------------------------------------------------------- #
def test_resources_sorted_by_resource_type():
    project = {
        "project_id": "P1",
        "start_date": date(2026, 7, 6),
        "work_days": "1111100",
        "holidays": set(),
        "tasks": [
            {"task_id": "T1", "es": 0, "ef": 1, "demands": {"zeta": 1, "alpha": 1}},
        ],
    }
    result = compute_resource_allocation([project], [])
    assert [r.resource_type for r in result.resources] == ["alpha", "zeta"]
