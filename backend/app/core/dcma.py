"""DCMA 14-point 排程品質檢核引擎。Pro Batch D（FEATURE D2，旗艦功能）。

純函式模組：不接觸資料庫、不依賴外部狀態，方便單元測試與重複呼叫。

公開 API：
    assess_dcma(tasks, deps, progress, baseline_tasks, data_date) -> DcmaReport

輸入契約：
    tasks           list，具備 .task_id .duration .es .ef .ls .lf .float_time
                    .is_critical .constraint_type .status（及選填的
                    .resource_demands，供第 10 項 Resources 檢核使用）。
    deps            list，具備 .task_id .predecessor_task_id .dep_type .lag_days。
    progress        {task_id: {percent_complete, actual_cost, actual_start_day,
                                actual_finish_day}}。
    baseline_tasks  [{task_id, es, ef, duration, budget}, ...]（可為空清單）。
    data_date       working-day offset（呼叫端預設為 project_duration）。

通則：當某檢核的分母為 0，或需要基準線但基準線缺席時 -> value=None、
      passed=None（僅供資訊參考，不計入 applicable_count / score）。
"""

from __future__ import annotations

from typing import Any, Iterable

from app.schemas.dcma import DcmaCheck, DcmaReport

_HARD_CONSTRAINTS = {"MSO", "MFO", "SNLT", "FNLT"}
_DETAIL_CAP = 25


def _capped(items: Iterable[str]) -> list[str]:
    """detail 欄位統一以「排序後」取前 _DETAIL_CAP 筆，輸出穩定。"""
    return sorted(set(items))[:_DETAIL_CAP]


def _check(
    key: str,
    name: str,
    name_cn: str,
    value: float | None,
    threshold: float | None,
    comparison: str,
    count: int,
    total: int,
    passed: bool | None,
    detail: Iterable[str],
) -> DcmaCheck:
    return DcmaCheck(
        key=key,
        name=name,
        name_cn=name_cn,
        value=value,
        threshold=threshold,
        comparison=comparison,
        count=int(count),
        total=int(total),
        passed=passed,
        detail=_capped(detail),
    )


def _reachable_es0(
    start_id: str, crit_map: dict[str, Any], preds_of: dict[str, list[str]]
) -> bool:
    """由 start_id（要徑任務）沿「要徑前置」是否可到達某個 es==0 的要徑任務。"""
    stack = [start_id]
    visited: set[str] = set()
    while stack:
        cur = stack.pop()
        if cur in visited:
            continue
        visited.add(cur)
        task = crit_map.get(cur)
        if task is None:
            continue
        if int(getattr(task, "es", 0) or 0) == 0:
            return True
        for pred in preds_of.get(cur, []):
            if pred in crit_map and pred not in visited:
                stack.append(pred)
    return False


def assess_dcma(
    tasks: list[Any],
    deps: list[Any],
    progress: dict[str, dict[str, Any]] | None,
    baseline_tasks: list[dict[str, Any]] | None,
    data_date: int,
) -> DcmaReport:
    """執行 DCMA 14-point 排程品質檢核，回傳 DcmaReport。"""
    progress = progress or {}
    baseline_tasks = baseline_tasks or []
    total_tasks = len(tasks)
    project_duration = max((int(getattr(t, "ef", 0) or 0) for t in tasks), default=0)

    # ---- preds_of / succs_of ---------------------------------------------
    preds_of: dict[str, list[str]] = {t.task_id: [] for t in tasks}
    succs_of: dict[str, list[str]] = {t.task_id: [] for t in tasks}
    for d in deps:
        preds_of.setdefault(d.task_id, []).append(d.predecessor_task_id)
        succs_of.setdefault(d.predecessor_task_id, []).append(d.task_id)

    checks: list[DcmaCheck] = []

    # ------------------------------------------------------------------ #
    # 1. Logic —— 邏輯遺漏（允許一個開放起點 + 一個開放終點）
    # ------------------------------------------------------------------ #
    no_pred = [t.task_id for t in tasks if not preds_of.get(t.task_id)]
    no_succ = [t.task_id for t in tasks if not succs_of.get(t.task_id)]
    missing = max(0, len(no_pred) + len(no_succ) - 2)
    value = (missing / total_tasks) if total_tasks else None
    passed = (value <= 0.05) if value is not None else None
    checks.append(
        _check(
            "logic", "Logic", "邏輯遺漏", value, 0.05, "lte",
            missing, total_tasks, passed, no_pred + no_succ,
        )
    )

    # ------------------------------------------------------------------ #
    # 2. Leads —— 提前 (負延時)
    # ------------------------------------------------------------------ #
    total_deps = len(deps)
    lead_links = [d for d in deps if int(d.lag_days or 0) < 0]
    lead_count = len(lead_links)
    checks.append(
        _check(
            "leads", "Leads", "提前 (負延時)", float(lead_count), 0.0, "eq",
            lead_count, total_deps, lead_count == 0,
            [f"{d.predecessor_task_id}->{d.task_id}" for d in lead_links],
        )
    )

    # ------------------------------------------------------------------ #
    # 3. Lags —— 延時
    # ------------------------------------------------------------------ #
    lag_links = [d for d in deps if int(d.lag_days or 0) > 0]
    lag_count = len(lag_links)
    value = (lag_count / total_deps) if total_deps else None
    passed = (value <= 0.05) if value is not None else None
    checks.append(
        _check(
            "lags", "Lags", "延時", value, 0.05, "lte",
            lag_count, total_deps, passed,
            [f"{d.predecessor_task_id}->{d.task_id}" for d in lag_links],
        )
    )

    # ------------------------------------------------------------------ #
    # 4. Relationship Types —— 關係型態 (FS%)
    # ------------------------------------------------------------------ #
    fs_links = [d for d in deps if (d.dep_type or "FS") == "FS"]
    non_fs_links = [d for d in deps if (d.dep_type or "FS") != "FS"]
    fs_count = len(fs_links)
    value = (fs_count / total_deps) if total_deps else None
    passed = (value >= 0.90) if value is not None else None
    checks.append(
        _check(
            "relationship_types", "Relationship Types", "關係型態 (FS%)",
            value, 0.90, "gte",
            fs_count, total_deps, passed,
            [f"{d.predecessor_task_id}->{d.task_id}({d.dep_type})" for d in non_fs_links],
        )
    )

    # ------------------------------------------------------------------ #
    # 5. Hard Constraints —— 硬性限制
    # ------------------------------------------------------------------ #
    hard_ids = [
        t.task_id for t in tasks if (t.constraint_type or "") in _HARD_CONSTRAINTS
    ]
    value = (len(hard_ids) / total_tasks) if total_tasks else None
    passed = (value <= 0.05) if value is not None else None
    checks.append(
        _check(
            "hard_constraints", "Hard Constraints", "硬性限制", value, 0.05, "lte",
            len(hard_ids), total_tasks, passed, hard_ids,
        )
    )

    # ------------------------------------------------------------------ #
    # 6. High Float —— 高寬裕 (>44d)
    # ------------------------------------------------------------------ #
    incomplete = [
        t for t in tasks
        if int(progress.get(t.task_id, {}).get("percent_complete", 0) or 0) < 100
    ]
    high_float_ids = [
        t.task_id for t in incomplete if int(getattr(t, "float_time", 0) or 0) > 44
    ]
    value = (len(high_float_ids) / len(incomplete)) if incomplete else None
    passed = (value <= 0.05) if value is not None else None
    checks.append(
        _check(
            "high_float", "High Float", "高寬裕 (>44d)", value, 0.05, "lte",
            len(high_float_ids), len(incomplete), passed, high_float_ids,
        )
    )

    # ------------------------------------------------------------------ #
    # 7. Negative Float —— 負寬裕
    # ------------------------------------------------------------------ #
    neg_float_ids = [
        t.task_id for t in tasks if int(getattr(t, "float_time", 0) or 0) < 0
    ]
    checks.append(
        _check(
            "negative_float", "Negative Float", "負寬裕", float(len(neg_float_ids)),
            0.0, "eq", len(neg_float_ids), total_tasks, len(neg_float_ids) == 0,
            neg_float_ids,
        )
    )

    # ------------------------------------------------------------------ #
    # 8. High Duration —— 高工期 (>44d)
    # ------------------------------------------------------------------ #
    bl_duration_by_task = {
        bt.get("task_id"): bt.get("duration") for bt in baseline_tasks
    }
    high_duration_ids = []
    for t in incomplete:
        dur = bl_duration_by_task.get(t.task_id)
        if dur is None:
            dur = getattr(t, "duration", 0)
        if int(dur or 0) > 44:
            high_duration_ids.append(t.task_id)
    value = (len(high_duration_ids) / len(incomplete)) if incomplete else None
    passed = (value <= 0.05) if value is not None else None
    checks.append(
        _check(
            "high_duration", "High Duration", "高工期 (>44d)", value, 0.05, "lte",
            len(high_duration_ids), len(incomplete), passed, high_duration_ids,
        )
    )

    # ------------------------------------------------------------------ #
    # 9. Invalid Dates —— 無效日期
    # ------------------------------------------------------------------ #
    invalid_ids = []
    for t in tasks:
        p = progress.get(t.task_id, {})
        a_start = p.get("actual_start_day")
        a_finish = p.get("actual_finish_day")
        if (a_start is not None and a_start > data_date) or (
            a_finish is not None and a_finish > data_date
        ):
            invalid_ids.append(t.task_id)
    checks.append(
        _check(
            "invalid_dates", "Invalid Dates", "無效日期", float(len(invalid_ids)),
            0.0, "eq", len(invalid_ids), total_tasks, len(invalid_ids) == 0,
            invalid_ids,
        )
    )

    # ------------------------------------------------------------------ #
    # 10. Resources —— 資源指派缺漏
    # ------------------------------------------------------------------ #
    with_duration = [t for t in tasks if int(getattr(t, "duration", 0) or 0) > 0]
    missing_resource_ids = [
        t.task_id for t in with_duration if not getattr(t, "resource_demands", None)
    ]
    value = (
        (len(missing_resource_ids) / len(with_duration)) if with_duration else None
    )
    passed = (value <= 0.05) if value is not None else None
    checks.append(
        _check(
            "resources", "Resources", "資源指派缺漏", value, 0.05, "lte",
            len(missing_resource_ids), len(with_duration), passed,
            missing_resource_ids,
        )
    )

    # ------------------------------------------------------------------ #
    # 11. Missed Tasks —— 逾期未完成 (需要基準線)
    # ------------------------------------------------------------------ #
    if not baseline_tasks:
        checks.append(
            _check(
                "missed_tasks", "Missed Tasks", "逾期未完成", None, 0.05, "lte",
                0, 0, None, [],
            )
        )
    else:
        bl_ef_by_task = {bt.get("task_id"): bt.get("ef") for bt in baseline_tasks}
        due = [
            t for t in tasks
            if bl_ef_by_task.get(t.task_id) is not None
            and bl_ef_by_task[t.task_id] <= data_date
        ]
        missed_ids = [
            t.task_id for t in due
            if int(progress.get(t.task_id, {}).get("percent_complete", 0) or 0) < 100
        ]
        value = (len(missed_ids) / len(due)) if due else None
        passed = (value <= 0.05) if value is not None else None
        checks.append(
            _check(
                "missed_tasks", "Missed Tasks", "逾期未完成", value, 0.05, "lte",
                len(missed_ids), len(due), passed, missed_ids,
            )
        )

    # ------------------------------------------------------------------ #
    # 12. Critical Path Test —— 要徑連續性
    # ------------------------------------------------------------------ #
    if total_tasks == 0:
        checks.append(
            _check(
                "critical_path_test", "Critical Path Test", "要徑連續性", None,
                1.0, "eq", 0, 0, None, [],
            )
        )
    else:
        crit_map = {t.task_id: t for t in tasks if getattr(t, "is_critical", False)}
        end_candidates = [
            t for t in crit_map.values()
            if int(getattr(t, "ef", 0) or 0) == project_duration
        ]
        continuous = any(
            _reachable_es0(t.task_id, crit_map, preds_of) for t in end_candidates
        )
        checks.append(
            _check(
                "critical_path_test", "Critical Path Test", "要徑連續性",
                1.0 if continuous else 0.0, 1.0, "eq",
                1 if continuous else 0, 1, continuous,
                [] if continuous else [t.task_id for t in end_candidates],
            )
        )

    # ------------------------------------------------------------------ #
    # 13. CPLI —— 要徑長度指數
    # ------------------------------------------------------------------ #
    end_tasks = [
        t for t in tasks if int(getattr(t, "ef", 0) or 0) == project_duration
    ]
    finish_float = min(
        (int(getattr(t, "float_time", 0) or 0) for t in end_tasks), default=0
    )
    value = (
        (project_duration + finish_float) / project_duration
        if project_duration > 0
        else None
    )
    passed = (value >= 0.95) if value is not None else None
    checks.append(
        _check(
            "cpli", "CPLI", "要徑長度指數", value, 0.95, "gte",
            finish_float, project_duration, passed,
            [t.task_id for t in end_tasks],
        )
    )

    # ------------------------------------------------------------------ #
    # 14. BEI —— 基準執行指數 (需要基準線)
    # ------------------------------------------------------------------ #
    completed_ids = [
        t.task_id for t in tasks
        if int(progress.get(t.task_id, {}).get("percent_complete", 0) or 0) == 100
    ]
    if not baseline_tasks:
        checks.append(
            _check(
                "bei", "BEI", "基準執行指數", None, 0.95, "gte", len(completed_ids),
                0, None, [],
            )
        )
    else:
        planned = sum(
            1 for bt in baseline_tasks
            if bt.get("ef") is not None and bt.get("ef") <= data_date
        )
        value = (len(completed_ids) / planned) if planned else None
        passed = (value >= 0.95) if value is not None else None
        checks.append(
            _check(
                "bei", "BEI", "基準執行指數", value, 0.95, "gte",
                len(completed_ids), planned, passed, completed_ids,
            )
        )

    applicable = [c for c in checks if c.passed is not None]
    passed_count = sum(1 for c in applicable if c.passed)
    applicable_count = len(applicable)
    score = (passed_count / applicable_count) if applicable_count else 0.0

    return DcmaReport(
        data_date=int(data_date),
        checks=checks,
        score=score,
        passed_count=passed_count,
        applicable_count=applicable_count,
        total_count=14,
    )
