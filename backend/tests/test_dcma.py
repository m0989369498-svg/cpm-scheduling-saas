"""DCMA 14-point 排程品質檢核引擎單元測試。Pro Batch D FEATURE D2（旗艦功能）。

直接驗證 ``app.core.dcma.assess_dcma``（純函式，不接觸資料庫）。

逐項針對 14 個檢核，以最小、可人工驗算的情境涵蓋：
  合格 / 不合格 / None (資訊性，分母為 0 或缺乏基準線) 三種路徑，
  並驗證要徑連續性 (continuous vs broken) 與整體報告聚合 (score/passed_count)。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.core.dcma import assess_dcma


@dataclass
class _Task:
    """最小任務物件：具備 assess_dcma 所需的鴨子型別欄位。"""

    task_id: str
    duration: int = 0
    es: int = 0
    ef: int = 0
    ls: int = 0
    lf: int = 0
    float_time: int = 0
    is_critical: bool = False
    constraint_type: str | None = None
    status: str = "PENDING"
    resource_demands: dict | None = None


@dataclass
class _Dep:
    task_id: str
    predecessor_task_id: str
    dep_type: str = "FS"
    lag_days: int = 0


def _find(report, key: str):
    for c in report.checks:
        if c.key == key:
            return c
    raise AssertionError(f"check {key!r} not found in report")


# --------------------------------------------------------------------------- #
# 1. Logic —— 邏輯遺漏 (允許一個開放起點 + 一個開放終點)
# --------------------------------------------------------------------------- #
def test_logic_single_chain_within_tolerance():
    """單一線性鏈 A->B->C：僅 A 無前置、僅 C 無後繼 -> missing=max(0,1+1-2)=0。"""
    tasks = [_Task("A"), _Task("B"), _Task("C")]
    deps = [_Dep("B", "A"), _Dep("C", "B")]
    report = assess_dcma(tasks, deps, {}, [], data_date=0)
    check = _find(report, "logic")
    assert check.value == 0.0
    assert check.passed is True


def test_logic_disconnected_chains_exceeds_tolerance():
    """兩條互不相連的鏈 A->B、C->D：2 個無前置 + 2 個無後繼 -> missing=2。"""
    tasks = [_Task("A"), _Task("B"), _Task("C"), _Task("D")]
    deps = [_Dep("B", "A"), _Dep("D", "C")]
    report = assess_dcma(tasks, deps, {}, [], data_date=0)
    check = _find(report, "logic")
    assert check.value == 0.5  # 2/4
    assert check.passed is False


# --------------------------------------------------------------------------- #
# 2. Leads —— 提前 (負延時)
# --------------------------------------------------------------------------- #
def test_leads_negative_lag_fails():
    tasks = [_Task("A"), _Task("B")]
    deps = [_Dep("B", "A", lag_days=-2)]
    report = assess_dcma(tasks, deps, {}, [], data_date=0)
    check = _find(report, "leads")
    assert check.value == 1.0
    assert check.passed is False


def test_leads_no_negative_lag_passes():
    tasks = [_Task("A"), _Task("B")]
    deps = [_Dep("B", "A", lag_days=0)]
    report = assess_dcma(tasks, deps, {}, [], data_date=0)
    check = _find(report, "leads")
    assert check.value == 0.0
    assert check.passed is True


# --------------------------------------------------------------------------- #
# 3. Lags —— 延時
# --------------------------------------------------------------------------- #
def test_lags_ratio_exceeds_threshold():
    tasks = [_Task(f"T{i}") for i in range(5)]
    deps = [
        _Dep("T1", "T0", lag_days=3),
        _Dep("T2", "T1", lag_days=2),
        _Dep("T3", "T2", lag_days=0),
        _Dep("T4", "T3", lag_days=0),
    ]
    report = assess_dcma(tasks, deps, {}, [], data_date=0)
    check = _find(report, "lags")
    assert check.value == 0.5  # 2/4
    assert check.passed is False


def test_lags_no_deps_returns_none():
    tasks = [_Task("A")]
    report = assess_dcma(tasks, [], {}, [], data_date=0)
    check = _find(report, "lags")
    assert check.value is None
    assert check.passed is None


# --------------------------------------------------------------------------- #
# 4. Relationship Types —— 關係型態 (FS%)
# --------------------------------------------------------------------------- #
def test_relationship_types_below_threshold():
    tasks = [_Task(f"T{i}") for i in range(4)]
    deps = [
        _Dep("T1", "T0", dep_type="FS"),
        _Dep("T2", "T1", dep_type="SS"),
        _Dep("T3", "T2", dep_type="FF"),
    ]
    report = assess_dcma(tasks, deps, {}, [], data_date=0)
    check = _find(report, "relationship_types")
    assert round(check.value, 4) == round(1 / 3, 4)
    assert check.passed is False


def test_relationship_types_all_fs_passes():
    tasks = [_Task(f"T{i}") for i in range(3)]
    deps = [_Dep("T1", "T0", dep_type="FS"), _Dep("T2", "T1", dep_type="FS")]
    report = assess_dcma(tasks, deps, {}, [], data_date=0)
    check = _find(report, "relationship_types")
    assert check.value == 1.0
    assert check.passed is True


# --------------------------------------------------------------------------- #
# 5. Hard Constraints —— 硬性限制
# --------------------------------------------------------------------------- #
def test_hard_constraints_ratio_exceeds():
    tasks = [
        _Task("A", constraint_type="MSO"),
        _Task("B"),
        _Task("C"),
        _Task("D"),
    ]
    report = assess_dcma(tasks, [], {}, [], data_date=0)
    check = _find(report, "hard_constraints")
    assert check.value == 0.25
    assert check.passed is False


def test_hard_constraints_none_passes():
    tasks = [_Task(f"T{i}") for i in range(10)]
    report = assess_dcma(tasks, [], {}, [], data_date=0)
    check = _find(report, "hard_constraints")
    assert check.value == 0.0
    assert check.passed is True


# --------------------------------------------------------------------------- #
# 6. High Float —— 高寬裕 (>44d)
# --------------------------------------------------------------------------- #
def test_high_float_exceeds_threshold():
    tasks = [
        _Task("A", float_time=50),
        _Task("B", float_time=10),
    ]
    progress = {
        "A": {"percent_complete": 0}, "B": {"percent_complete": 0},
    }
    report = assess_dcma(tasks, [], progress, [], data_date=0)
    check = _find(report, "high_float")
    assert check.value == 0.5  # 1/2
    assert check.passed is False


def test_high_float_all_within_bound_passes():
    tasks = [_Task("A", float_time=10), _Task("B", float_time=20)]
    progress = {"A": {"percent_complete": 0}, "B": {"percent_complete": 0}}
    report = assess_dcma(tasks, [], progress, [], data_date=0)
    check = _find(report, "high_float")
    assert check.value == 0.0
    assert check.passed is True


def test_high_float_no_incomplete_tasks_returns_none():
    tasks = [_Task("A", float_time=100)]
    progress = {"A": {"percent_complete": 100}}
    report = assess_dcma(tasks, [], progress, [], data_date=0)
    check = _find(report, "high_float")
    assert check.value is None
    assert check.passed is None


# --------------------------------------------------------------------------- #
# 7. Negative Float —— 負寬裕
# --------------------------------------------------------------------------- #
def test_negative_float_detected():
    tasks = [_Task("A", float_time=-2), _Task("B", float_time=0), _Task("C", float_time=5)]
    report = assess_dcma(tasks, [], {}, [], data_date=0)
    check = _find(report, "negative_float")
    assert check.value == 1.0
    assert check.passed is False


def test_negative_float_none_passes():
    tasks = [_Task("A", float_time=0), _Task("B", float_time=3)]
    report = assess_dcma(tasks, [], {}, [], data_date=0)
    check = _find(report, "negative_float")
    assert check.value == 0.0
    assert check.passed is True


# --------------------------------------------------------------------------- #
# 8. High Duration —— 高工期 (>44d)；優先取基準線工期
# --------------------------------------------------------------------------- #
def test_high_duration_uses_baseline_duration_when_present():
    tasks = [_Task("A", duration=10), _Task("B", duration=5)]
    progress = {"A": {"percent_complete": 0}, "B": {"percent_complete": 0}}
    baseline_tasks = [
        {"task_id": "A", "duration": 60},  # 基準線工期覆蓋任務自身工期
        {"task_id": "B", "duration": 5},
    ]
    report = assess_dcma(tasks, [], progress, baseline_tasks, data_date=0)
    check = _find(report, "high_duration")
    assert check.value == 0.5  # 1/2 (僅 A 的基準線工期 60 > 44)
    assert check.passed is False


def test_high_duration_falls_back_to_task_duration_without_baseline():
    tasks = [_Task("A", duration=50)]
    progress = {"A": {"percent_complete": 0}}
    report = assess_dcma(tasks, [], progress, [], data_date=0)
    check = _find(report, "high_duration")
    assert check.value == 1.0
    assert check.passed is False


# --------------------------------------------------------------------------- #
# 9. Invalid Dates —— 無效日期
# --------------------------------------------------------------------------- #
def test_invalid_dates_actual_start_after_data_date():
    tasks = [_Task("A")]
    progress = {
        "A": {"percent_complete": 50, "actual_start_day": 15, "actual_finish_day": None}
    }
    report = assess_dcma(tasks, [], progress, [], data_date=10)
    check = _find(report, "invalid_dates")
    assert check.value == 1.0
    assert check.passed is False


def test_invalid_dates_within_range_passes():
    tasks = [_Task("A")]
    progress = {
        "A": {"percent_complete": 50, "actual_start_day": 5, "actual_finish_day": None}
    }
    report = assess_dcma(tasks, [], progress, [], data_date=10)
    check = _find(report, "invalid_dates")
    assert check.value == 0.0
    assert check.passed is True


# --------------------------------------------------------------------------- #
# 10. Resources —— 資源指派缺漏
# --------------------------------------------------------------------------- #
def test_resources_missing_demand_ratio():
    tasks = [
        _Task("A", duration=5, resource_demands={"crane": 1}),
        _Task("B", duration=5, resource_demands=None),
        _Task("C", duration=0, resource_demands=None),  # duration=0 -> 不計入分母
    ]
    report = assess_dcma(tasks, [], {}, [], data_date=0)
    check = _find(report, "resources")
    assert check.value == 0.5  # 1/2 (B 缺漏；C 不計)
    assert check.passed is False


def test_resources_all_assigned_passes():
    tasks = [
        _Task("A", duration=5, resource_demands={"crane": 1}),
        _Task("B", duration=5, resource_demands={"manpower": 3}),
    ]
    report = assess_dcma(tasks, [], {}, [], data_date=0)
    check = _find(report, "resources")
    assert check.value == 0.0
    assert check.passed is True


# --------------------------------------------------------------------------- #
# 11. Missed Tasks —— 逾期未完成 (需要基準線)
# --------------------------------------------------------------------------- #
def test_missed_tasks_without_baseline_returns_none():
    tasks = [_Task("A")]
    report = assess_dcma(tasks, [], {}, [], data_date=10)
    check = _find(report, "missed_tasks")
    assert check.value is None
    assert check.passed is None


def test_missed_tasks_detected_with_baseline():
    tasks = [_Task("A"), _Task("B")]
    progress = {"A": {"percent_complete": 50}, "B": {"percent_complete": 100}}
    baseline_tasks = [{"task_id": "A", "ef": 10}, {"task_id": "B", "ef": 30}]
    report = assess_dcma(tasks, [], progress, baseline_tasks, data_date=20)
    check = _find(report, "missed_tasks")
    # due = {A} (bl_ef 10 <= 20；B 的 30 > 20 不算到期)；missed = {A} (pct<100)。
    assert check.value == 1.0
    assert check.passed is False


def test_missed_tasks_all_on_track_passes():
    tasks = [_Task("A")]
    progress = {"A": {"percent_complete": 100}}
    baseline_tasks = [{"task_id": "A", "ef": 10}]
    report = assess_dcma(tasks, [], progress, baseline_tasks, data_date=20)
    check = _find(report, "missed_tasks")
    assert check.value == 0.0
    assert check.passed is True


# --------------------------------------------------------------------------- #
# 12. Critical Path Test —— 要徑連續性
# --------------------------------------------------------------------------- #
def test_critical_path_continuous():
    """Crit1(es0,ef5) -> Crit2(es5,ef10)，皆為要徑且相連 -> 連續。"""
    tasks = [
        _Task("Crit1", es=0, ef=5, is_critical=True),
        _Task("Crit2", es=5, ef=10, is_critical=True),
    ]
    deps = [_Dep("Crit2", "Crit1")]
    report = assess_dcma(tasks, deps, {}, [], data_date=0)
    check = _find(report, "critical_path_test")
    assert check.value == 1.0
    assert check.passed is True


def test_critical_path_broken_by_non_critical_link():
    """Crit1 -> Mid(非要徑) -> Crit2：要徑鏈被中間的非要徑任務打斷 -> 不連續。"""
    tasks = [
        _Task("Crit1", es=0, ef=5, is_critical=True),
        _Task("Mid", es=5, ef=7, is_critical=False, float_time=3),
        _Task("Crit2", es=7, ef=12, is_critical=True),
    ]
    deps = [_Dep("Mid", "Crit1"), _Dep("Crit2", "Mid")]
    report = assess_dcma(tasks, deps, {}, [], data_date=0)
    check = _find(report, "critical_path_test")
    assert check.value == 0.0
    assert check.passed is False


def test_critical_path_no_tasks_returns_none():
    report = assess_dcma([], [], {}, [], data_date=0)
    check = _find(report, "critical_path_test")
    assert check.value is None
    assert check.passed is None


# --------------------------------------------------------------------------- #
# 13. CPLI —— 要徑長度指數
# --------------------------------------------------------------------------- #
def test_cpli_positive_finish_float_passes():
    tasks = [_Task("A", ef=20, float_time=2), _Task("B", ef=15, float_time=0)]
    report = assess_dcma(tasks, [], {}, [], data_date=0)
    check = _find(report, "cpli")
    # project_duration = max(ef) = 20；finish_float = min(float of ef==20 tasks) = 2。
    assert check.value == (20 + 2) / 20
    assert check.passed is True


def test_cpli_negative_finish_float_fails():
    tasks = [_Task("A", ef=20, float_time=-3)]
    report = assess_dcma(tasks, [], {}, [], data_date=0)
    check = _find(report, "cpli")
    assert check.value == (20 - 3) / 20
    assert check.passed is False


def test_cpli_zero_project_duration_returns_none():
    tasks = [_Task("A", duration=0, es=0, ef=0)]
    report = assess_dcma(tasks, [], {}, [], data_date=0)
    check = _find(report, "cpli")
    assert check.value is None
    assert check.passed is None


# --------------------------------------------------------------------------- #
# 14. BEI —— 基準執行指數 (需要基準線)
# --------------------------------------------------------------------------- #
def test_bei_without_baseline_returns_none():
    tasks = [_Task("A")]
    progress = {"A": {"percent_complete": 100}}
    report = assess_dcma(tasks, [], progress, [], data_date=10)
    check = _find(report, "bei")
    assert check.value is None
    assert check.passed is None


def test_bei_meets_target():
    tasks = [_Task("A"), _Task("B")]
    progress = {"A": {"percent_complete": 100}, "B": {"percent_complete": 0}}
    baseline_tasks = [{"task_id": "A", "ef": 5}]  # 僅 A 已到期規劃 (ef<=data_date)
    report = assess_dcma(tasks, [], progress, baseline_tasks, data_date=10)
    check = _find(report, "bei")
    assert check.value == 1.0  # completed=1 / planned=1
    assert check.passed is True


def test_bei_below_target():
    tasks = [_Task("A")]
    progress = {"A": {"percent_complete": 0}}
    baseline_tasks = [{"task_id": "A", "ef": 5}]
    report = assess_dcma(tasks, [], progress, baseline_tasks, data_date=10)
    check = _find(report, "bei")
    assert check.value == 0.0
    assert check.passed is False


# --------------------------------------------------------------------------- #
# 報告層級聚合 (score / passed_count / applicable_count / total_count)
# --------------------------------------------------------------------------- #
def test_report_has_all_fourteen_checks_with_correct_keys():
    tasks = [_Task("A", es=0, ef=5, is_critical=True)]
    report = assess_dcma(tasks, [], {}, [], data_date=5)
    assert report.total_count == 14
    assert len(report.checks) == 14
    expected_keys = {
        "logic", "leads", "lags", "relationship_types", "hard_constraints",
        "high_float", "negative_float", "high_duration", "invalid_dates",
        "resources", "missed_tasks", "critical_path_test", "cpli", "bei",
    }
    assert {c.key for c in report.checks} == expected_keys


def test_report_score_matches_passed_over_applicable():
    tasks = [_Task("A", es=0, ef=5, is_critical=True)]
    report = assess_dcma(tasks, [], {}, [], data_date=5)
    applicable = [c for c in report.checks if c.passed is not None]
    passed = [c for c in applicable if c.passed]
    assert report.applicable_count == len(applicable)
    assert report.passed_count == len(passed)
    if applicable:
        assert report.score == len(passed) / len(applicable)
    else:
        assert report.score == 0.0
