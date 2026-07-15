"""資源路由 (Resources router) —— Phase 8 資源限制 / 需求 / 撫平。

職責:
  GET  /projects/{pid}/resources  讀取資源設定 (限制 + 各任務需求)。
  PUT  /projects/{pid}/resources  upsert 資源限制 + 各任務 resource_demands。
  POST /projects/{pid}/level      執行資源撫平 (resource leveling), 回傳 LevelingResult;
                                  若撫平導致工期展延則觸發風險自動化 (risk_listener)。

設計重點:
  * 重用 projects._get_project_or_404 / _load_tasks / _load_dependencies /
    _build_task_definitions, 不重複實作載入與 DAG 組裝。
  * 撫平引擎 (app.core.resource_leveling.level_resources) 為純函式 (無 DB)。
  * 租戶隔離: PostgreSQL 由 RLS 強制; sqlite (dev) 由 _get_project_or_404 以 tenant_id
    過濾。寫入時 tenant_id 一律取自 ctx (絕不信任輸入)。
  * project_resource_limits / tasks.resource_demands 皆位於 public schema (受 RLS 保護)。
"""

from __future__ import annotations

import logging
from datetime import date as _date

from fastapi import APIRouter, Depends
from sqlalchemy import delete as sa_delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit, risk_listener, workcal
from app.core.resource_leveling import level_resources
from app.deps import TenantContext, get_db, require_role, verify_tenant
from app.models.orm import (
    ProjectResourceLimit,
    ResourceCalendar,
    ResourceCalendarHoliday,
    Task,
)
from app.routers.projects import (
    _build_task_definitions,
    _get_project_or_404,
    _load_dependencies,
    _load_holiday_dates,
    _load_tasks,
)
from app.schemas.analytics import (
    LevelingResult,
    ResourceCalendar as ResourceCalendarSchema,
    ResourceConfig,
    ResourceLimit,
)

logger = logging.getLogger("cpm.routers.resources")

router = APIRouter(prefix="/projects", tags=["resources"])


# ---------------------------------------------------------------------------
# 內部工具
# ---------------------------------------------------------------------------
async def _load_resource_limits(
    db: AsyncSession, project_id: str
) -> list[ProjectResourceLimit]:
    """載入專案的資源上限 (依 resource_type 排序, 輸出穩定)。"""
    result = await db.execute(
        select(ProjectResourceLimit)
        .where(ProjectResourceLimit.project_id == project_id)
        .order_by(ProjectResourceLimit.resource_type)
    )
    return list(result.scalars().all())


async def _load_resource_calendars(
    db: AsyncSession, project_id: str
) -> list[ResourceCalendar]:
    """載入專案的資源工作日曆 (依 resource_type 排序, 輸出穩定)。Pro Batch D FEATURE D3。"""
    result = await db.execute(
        select(ResourceCalendar)
        .where(ResourceCalendar.project_id == project_id)
        .order_by(ResourceCalendar.resource_type)
    )
    return list(result.scalars().all())


async def _load_resource_calendar_holidays(
    db: AsyncSession, project_id: str
) -> dict[str, list[str]]:
    """載入專案各資源的例外停工日 (Pro Batch E FEATURE E2)。

    回傳 {resource_type: [ISO 日期字串, ...]} (依日期排序，輸出穩定)；
    未設定任何例外假日的資源不在結果中 (呼叫端以空清單預設)。
    """
    result = await db.execute(
        select(ResourceCalendarHoliday)
        .where(ResourceCalendarHoliday.project_id == project_id)
        .order_by(ResourceCalendarHoliday.resource_type, ResourceCalendarHoliday.holiday_date)
    )
    out: dict[str, list[str]] = {}
    for row in result.scalars().all():
        out.setdefault(row.resource_type, []).append(row.holiday_date.isoformat())
    return out


def _demands_from_tasks(tasks: list[Task]) -> dict[str, dict[str, int]]:
    """由各任務的 resource_demands 欄位組出 {task_id: {resource: qty}} 對映。

    僅納入有需求 (非空) 的任務; None / 空 dict 略過。
    """
    demands: dict[str, dict[str, int]] = {}
    for tk in tasks:
        rd = getattr(tk, "resource_demands", None)
        if rd:
            # 防禦性轉型: 確保 value 為 int
            demands[tk.task_id] = {str(k): int(v) for k, v in dict(rd).items()}
    return demands


def _limits_to_map(limits: list[ProjectResourceLimit]) -> dict[str, int]:
    """ORM 限制清單 -> {resource_type: max_capacity}。"""
    return {lim.resource_type: int(lim.max_capacity) for lim in limits}


def _build_resource_config(
    limits: list[ProjectResourceLimit],
    tasks: list[Task],
    calendars: list[ResourceCalendar] | None = None,
    holidays_by_type: dict[str, list[str]] | None = None,
) -> ResourceConfig:
    """組裝 ResourceConfig 回應 (限制 + 各任務需求 + 資源日曆 + 資源例外假日)。"""
    holidays_by_type = holidays_by_type or {}
    return ResourceConfig(
        limits=[
            ResourceLimit(
                resource_type=lim.resource_type,
                max_capacity=int(lim.max_capacity),
                unit_cost=float(lim.unit_cost or 0),
                category=lim.category or "labor",
            )
            for lim in limits
        ],
        demands=_demands_from_tasks(tasks),
        calendars=[
            ResourceCalendarSchema(
                resource_type=c.resource_type,
                work_days=c.work_days or "1111110",
                holidays=holidays_by_type.get(c.resource_type, []),
            )
            for c in (calendars or [])
        ],
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@router.get("/{project_id}/resources", response_model=ResourceConfig)
async def get_resources(
    project_id: str,
    ctx: TenantContext = Depends(verify_tenant),
    db: AsyncSession = Depends(get_db),
) -> ResourceConfig:
    """讀取專案資源設定: 限制取自 project_resource_limits, 需求取自 tasks.resource_demands,
    日曆取自 resource_calendars (Pro Batch D FEATURE D3；含 holidays，Pro Batch E FEATURE E2)。"""
    await _get_project_or_404(db, project_id, ctx.tenant_id)
    limits = await _load_resource_limits(db, project_id)
    tasks = await _load_tasks(db, project_id)
    calendars = await _load_resource_calendars(db, project_id)
    holidays_by_type = await _load_resource_calendar_holidays(db, project_id)
    return _build_resource_config(limits, tasks, calendars, holidays_by_type)


@router.put("/{project_id}/resources", response_model=ResourceConfig)
async def set_resources(
    project_id: str,
    payload: ResourceConfig,
    ctx: TenantContext = Depends(verify_tenant),
    db: AsyncSession = Depends(get_db),
    _role: None = Depends(require_role("editor")),
) -> ResourceConfig:
    """upsert 資源限制 (project_resource_limits)、各任務需求 (tasks.resource_demands)
    與資源日曆 (resource_calendars，Pro Batch D FEATURE D3)。

    - 限制: 依 (project_id, resource_type) upsert (含 unit_cost / category)；
      payload 未列出的既有限制保留不動。
    - 需求: 依 task_id upsert 至對應 Task.resource_demands; payload 未列出的任務不變動。
    - 日曆: 依 (project_id, resource_type) upsert；payload 未列出的既有日曆保留不動
      (與限制的 upsert 政策一致)。
    tenant_id 一律取自 ctx (寫入隔離)。
    """
    await _get_project_or_404(db, project_id, ctx.tenant_id)

    # --- upsert 資源限制 ---
    existing = await _load_resource_limits(db, project_id)
    by_type = {lim.resource_type: lim for lim in existing}
    for lim_in in payload.limits:
        row = by_type.get(lim_in.resource_type)
        if row is not None:
            row.max_capacity = int(lim_in.max_capacity)
            row.unit_cost = float(lim_in.unit_cost or 0)
            row.category = lim_in.category or "labor"
        else:
            db.add(
                ProjectResourceLimit(
                    project_id=project_id,
                    tenant_id=ctx.tenant_id,
                    resource_type=lim_in.resource_type,
                    max_capacity=int(lim_in.max_capacity),
                    unit_cost=float(lim_in.unit_cost or 0),
                    category=lim_in.category or "labor",
                )
            )

    # --- upsert 資源日曆 (Pro Batch D FEATURE D3) + 例外假日 (Pro Batch E FEATURE E2) ---
    if payload.calendars:
        existing_cal = await _load_resource_calendars(db, project_id)
        by_cal_type = {c.resource_type: c for c in existing_cal}
        for cal_in in payload.calendars:
            row = by_cal_type.get(cal_in.resource_type)
            if row is not None:
                row.work_days = cal_in.work_days
            else:
                db.add(
                    ResourceCalendar(
                        project_id=project_id,
                        tenant_id=ctx.tenant_id,
                        resource_type=cal_in.resource_type,
                        work_days=cal_in.work_days,
                    )
                )

            # 例外假日：以「替換式 upsert」覆寫該 (project_id, resource_type) 的
            # 既有假日清單 (與 PUT /projects/{pid}/holidays 的替換式語意一致)。
            await db.execute(
                sa_delete(ResourceCalendarHoliday).where(
                    ResourceCalendarHoliday.project_id == project_id,
                    ResourceCalendarHoliday.resource_type == cal_in.resource_type,
                )
            )
            seen_dates: set[str] = set()
            for raw_date in cal_in.holidays or []:
                if raw_date in seen_dates:
                    continue
                seen_dates.add(raw_date)
                db.add(
                    ResourceCalendarHoliday(
                        project_id=project_id,
                        tenant_id=ctx.tenant_id,
                        resource_type=cal_in.resource_type,
                        holiday_date=_date.fromisoformat(raw_date),
                    )
                )

    # --- upsert 各任務需求 ---
    if payload.demands:
        tasks = await _load_tasks(db, project_id)
        by_task = {tk.task_id: tk for tk in tasks}
        for task_id, demand in payload.demands.items():
            tk = by_task.get(task_id)
            if tk is None:
                # 略過未知 task_id (避免為不存在任務建立孤兒資料)
                logger.warning(
                    "set_resources: project=%s 未知 task_id=%s (略過需求 upsert)",
                    project_id,
                    task_id,
                )
                continue
            tk.resource_demands = {str(k): int(v) for k, v in dict(demand).items()}

    await db.flush()

    # 稽核 (best-effort): 失敗僅記錄, 絕不中斷主要操作。
    try:
        await audit.log_action(
            db,
            ctx,
            "RESOURCES_UPDATE",
            {
                "project_id": project_id,
                "limits": {
                    lim.resource_type: int(lim.max_capacity)
                    for lim in payload.limits
                },
                "demand_task_ids": sorted(payload.demands.keys())
                if payload.demands
                else [],
            },
        )
    except Exception as exc:  # noqa: BLE001 - 稽核失敗不可中斷主要操作
        logger.warning("audit RESOURCES_UPDATE failed (ignored): %s", exc)

    # 回傳更新後的完整設定
    limits = await _load_resource_limits(db, project_id)
    tasks = await _load_tasks(db, project_id)
    calendars = await _load_resource_calendars(db, project_id)
    holidays_by_type = await _load_resource_calendar_holidays(db, project_id)
    return _build_resource_config(limits, tasks, calendars, holidays_by_type)


async def _build_availability(
    db: AsyncSession,
    project,
    project_id: str,
    limits: dict[str, int],
    definitions: list,
) -> dict[str, list[int]] | None:
    """組出資源撫平用的逐日可用產能 (Pro Batch D FEATURE D3 + Pro Batch E FEATURE E2)。

    僅當「專案已設定 start_date」且「至少一筆 resource_calendars」時才建立；
    否則回傳 None (退回批次前的純量上限行為，regression-critical)。
    有日曆的資源：每個 offset 天，經 workcal.offset_to_date 換算為實際日期，
    當「該資源自己的 work_days 判定為非工作日」或「落在專案假日」或「落在
    該資源自己的例外停工日 (resource_calendar_holidays，FEATURE E2)」之一時，
    當日產能視為 0；否則產能=limits[res]。無日曆的資源不列入 availability
    (退回純量上限)。空的資源專屬假日集合 == Batch D 行為 (向下相容)。
    """
    if project.start_date is None:
        return None

    calendars = await _load_resource_calendars(db, project_id)
    if not calendars:
        return None

    holidays = await _load_holiday_dates(db, project_id)
    resource_holidays_by_type = await _load_resource_calendar_holidays(db, project_id)
    resource_holiday_dates: dict[str, set] = {
        rtype: {_date.fromisoformat(d) for d in dates}
        for rtype, dates in resource_holidays_by_type.items()
    }

    from app.core.cpm_engine import calculate_cpm, project_duration as _project_duration

    if definitions:
        try:
            base_results = calculate_cpm(definitions)
            project_duration = _project_duration(base_results)
        except ValueError:
            project_duration = 0
    else:
        project_duration = 0

    availability: dict[str, list[int]] = {}
    for cal in calendars:
        cap = int(limits.get(cal.resource_type, 0))
        own_holidays = resource_holiday_dates.get(cal.resource_type, set())
        day_list: list[int] = []
        for day in range(project_duration):
            the_date = workcal.offset_to_date(
                project.start_date, day, project.work_days or "1111110", holidays
            )
            is_resource_workday = (cal.work_days or "1111110")[the_date.weekday()] == "1"
            is_own_holiday = the_date in own_holidays
            day_list.append(cap if (is_resource_workday and not is_own_holiday) else 0)
        availability[cal.resource_type] = day_list

    return availability


@router.post("/{project_id}/level", response_model=LevelingResult)
async def level_project(
    project_id: str,
    ctx: TenantContext = Depends(verify_tenant),
    db: AsyncSession = Depends(get_db),
) -> LevelingResult:
    """執行資源撫平: 載入任務/DAG/需求/限制 -> level_resources -> 回傳 LevelingResult。

    若撫平導致工期展延 (result.extended), 觸發 risk_listener.evaluate_and_dispatch
    (reason="LEVELING_EXTENSION"), 入列 RISK_PROVISION 事件並 best-effort 通知。

    Pro Batch D (FEATURE D3)：當專案已設定 start_date 且至少一筆
    resource_calendars 存在時，建立逐日可用產能 (availability) 傳入
    level_resources，使有專屬日曆的資源在非其工作日時產能視為 0；
    否則 availability=None，行為與批次前完全一致。
    """
    project = await _get_project_or_404(db, project_id, ctx.tenant_id)

    tasks = await _load_tasks(db, project_id)
    deps = await _load_dependencies(db, project_id)
    definitions = _build_task_definitions(tasks, deps)

    demands = _demands_from_tasks(tasks)
    limits = _limits_to_map(await _load_resource_limits(db, project_id))

    availability = await _build_availability(
        db, project, project_id, limits, definitions
    )

    result: LevelingResult = level_resources(definitions, demands, limits, availability)

    if result.extended:
        try:
            await risk_listener.evaluate_and_dispatch(
                db,
                ctx,
                project.project_id,
                reason="LEVELING_EXTENSION",
                detail={
                    "project_id": project.project_id,
                    "original_duration": result.original_duration,
                    "leveled_duration": result.leveled_duration,
                    "extended_by": result.leveled_duration - result.original_duration,
                    "over_capacity_days": result.over_capacity_days,
                    "unresolved": result.unresolved,
                },
            )
        except Exception as exc:  # noqa: BLE001 - 風險派工失敗不可中斷撫平結果回傳
            logger.warning(
                "level_project: risk dispatch failed (ignored) project=%s: %s",
                project_id,
                exc,
            )

    return result
