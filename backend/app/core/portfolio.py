"""投資組合資源分配 (portfolio resource allocation)。Pro Batch E (FEATURE E1)。

純函式模組：不接觸資料庫、不依賴外部狀態，方便單元測試與重複呼叫。
重用 ``app.core.workcal.offset_to_date`` 將各專案的工作日 offset 換算為實際
日期，再以 ISO 週彙總「峰值 (peak) 日需求」——與資源撫平 (resource_leveling)
的逐日負載不同，此處刻意採「週峰值」而非「週加總」，避免長工期任務把單日
需求灌水成整週總和 (失真)。

演算法（FROZEN，見 SPEC）：
    1. per_date[(resource_type, date)] = 該資源當日跨所有「已排程」(有
       start_date) 專案的加總需求。
    2. 未設定 start_date 但確有資源需求的專案 -> 記入 unscheduled_projects
       (無法排入時間軸，故不貢獻 per_date)。
    3. 依 ISO 週彙總每個 (resource, date) -> 週峰值 (取當週逐日最大值，非加總)。
    4. 依 resource_type 排序組出各列；capacity/unit_cost/name/category 取自
       租戶資源池 (pool)，缺席時採預設值 (capacity=0 -> 有需求即超載)。
    5. 全程以排序鍵確保輸出決定性 (deterministic)。
"""

from __future__ import annotations

from datetime import date
from typing import Any

from app.core import workcal
from app.schemas.enterprise import (
    ResourceAllocationResult,
    ResourceAllocationRow,
    TenantResource,
)

__all__ = ["compute_resource_allocation"]


def _pool_lookup(
    pool: list[TenantResource] | dict[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    """將 pool (TenantResource 清單 或 dict) 正規化為
    {resource_type: {capacity, name, category, unit_cost}}。
    """
    lookup: dict[str, dict[str, Any]] = {}
    if not pool:
        return lookup
    if isinstance(pool, dict):
        for rtype, info in pool.items():
            info = info or {}
            lookup[str(rtype)] = {
                "capacity": int(info.get("capacity", 0) or 0),
                "name": str(info.get("name") or rtype),
                "category": str(info.get("category") or "labor"),
                "unit_cost": float(info.get("unit_cost", 0.0) or 0.0),
            }
        return lookup
    for item in pool:
        rtype = getattr(item, "resource_type")
        lookup[str(rtype)] = {
            "capacity": int(getattr(item, "capacity", 0) or 0),
            "name": str(getattr(item, "name", "") or rtype),
            "category": str(getattr(item, "category", "") or "labor"),
            "unit_cost": float(getattr(item, "unit_cost", 0.0) or 0.0),
        }
    return lookup


def _iso_week_label(d: date) -> str:
    iso = d.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def compute_resource_allocation(
    projects: list[dict[str, Any]],
    pool: list[TenantResource] | dict[str, Any] | None,
) -> ResourceAllocationResult:
    """計算跨專案的租戶層級資源分配剖面 (weekly peak demand profile)。

    projects 每筆形狀：
        {project_id, start_date: date|None, work_days: str, holidays: set[date],
         tasks: [{task_id, es: int, ef: int, demands: {resource_type: int}}]}
    pool：租戶資源池 (list[TenantResource] 或 dict[resource_type -> info])。
    """
    pool_lookup = _pool_lookup(pool)

    # per_date[(resource_type, date)] = 當日加總需求 (僅計入「已排程」專案)。
    per_date: dict[tuple[str, date], int] = {}
    unscheduled_projects: list[str] = []
    demanded_types: set[str] = set()

    for proj in projects or []:
        project_id = str(proj.get("project_id") or "")
        start_date = proj.get("start_date")
        tasks = proj.get("tasks") or []

        has_demand = any(
            (task.get("demands") or {}) and any(
                int(qty or 0) > 0 for qty in (task.get("demands") or {}).values()
            )
            for task in tasks
        )

        if start_date is None:
            if has_demand:
                unscheduled_projects.append(project_id)
            continue

        work_days = proj.get("work_days") or "1111110"
        holidays: set[date] = proj.get("holidays") or set()

        for task in tasks:
            es = int(task.get("es", 0) or 0)
            ef = int(task.get("ef", 0) or 0)
            demands = task.get("demands") or {}
            if ef <= es or not demands:
                continue
            for offset in range(es, ef):
                d = workcal.offset_to_date(start_date, offset, work_days, holidays)
                for rtype, qty in demands.items():
                    amount = int(qty or 0)
                    if not amount:
                        continue
                    demanded_types.add(str(rtype))
                    key = (str(rtype), d)
                    per_date[key] = per_date.get(key, 0) + amount

    # 週峰值彙總：dict[(resource_type, week_label)] = max(當週逐日總量)。
    by_week_peak: dict[tuple[str, str], int] = {}
    for (rtype, d), amount in per_date.items():
        week = _iso_week_label(d)
        key = (rtype, week)
        by_week_peak[key] = max(by_week_peak.get(key, 0), amount)

    all_resource_types = sorted(set(pool_lookup.keys()) | demanded_types)

    rows: list[ResourceAllocationRow] = []
    all_weeks: set[str] = set()
    for rtype in all_resource_types:
        info = pool_lookup.get(
            rtype, {"capacity": 0, "name": rtype, "category": "labor", "unit_cost": 0.0}
        )
        by_week: dict[str, int] = {}
        for (r, week), amount in by_week_peak.items():
            if r == rtype:
                by_week[week] = amount
        by_week = dict(sorted(by_week.items()))
        peak = max(by_week.values(), default=0)
        capacity = int(info["capacity"])
        over_weeks = sorted(week for week, v in by_week.items() if v > capacity)

        rows.append(
            ResourceAllocationRow(
                resource_type=rtype,
                name=str(info.get("name") or rtype),
                category=str(info.get("category") or "labor"),
                capacity=capacity,
                unit_cost=float(info.get("unit_cost", 0.0) or 0.0),
                by_week=by_week,
                peak=peak,
                over_weeks=over_weeks,
            )
        )
        all_weeks.update(by_week.keys())

    warnings: list[str] = []
    if unscheduled_projects:
        warnings.append(
            "以下專案有資源需求但未設定開工日期 (start_date)，未納入分配時間軸："
            f"{sorted(unscheduled_projects)}"
        )

    return ResourceAllocationResult(
        weeks=sorted(all_weeks),
        resources=rows,
        unscheduled_projects=sorted(unscheduled_projects),
        warnings=warnings,
    )
