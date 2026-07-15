"""企業級 (enterprise / tenant-level) 資源路由。Pro Batch E (FEATURE E1)。

職責:
  GET  /resources/pool        讀取租戶層級資源池 (tenant_resources)。
  PUT  /resources/pool        upsert 租戶資源池 (editor+)。
  GET  /resources/allocation  投資組合資源分配剖面 (跨專案週別峰值需求 vs 產能)；
                               唯讀 (無 require_role)。

設計重點:
  * 與 routers/resources.py（單一專案 project_resource_limits）不同：本路由
    以「租戶」為範圍，前綴為 "/resources"（不在 /projects/{pid} 之下）。
  * GET /resources/allocation 為「固定查詢數」(constant query count)，與專案數
    無關 (mirror dashboard.py PERF-1 模式)：
      Q1 租戶資源池 (tenant_resources)。
      Q2 本租戶專案 (deleted_at IS NULL；取 project_id/start_date/work_days)。
      Q3 這些專案的所有任務 (project_id/task_id/es/ef/resource_demands；IN 子句)。
      Q4 這些專案的所有假日 (project_id/holiday_date；IN 子句)。
    「不」執行 calculate_cpm：直接使用已持久化的 es/ef (與 dashboard.py PERF-4 同理)。
  * 分配計算引擎 (app.core.portfolio.compute_resource_allocation) 為純函式 (無 DB)。
  * 租戶隔離: PostgreSQL 由 RLS 強制; sqlite (dev) 由查詢條件
    (tenant_id == ctx.tenant_id) 過濾。寫入時 tenant_id 一律取自 ctx。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.portfolio import compute_resource_allocation
from app.deps import TenantContext, get_db, require_role, verify_tenant
from app.models.orm import Project, ProjectHoliday, Task, TenantResource
from app.schemas.enterprise import (
    ResourceAllocationResult,
    TenantResource as TenantResourceSchema,
)

logger = logging.getLogger("cpm.routers.enterprise")

router = APIRouter(prefix="/resources", tags=["enterprise"])


# ---------------------------------------------------------------------------
# 內部工具
# ---------------------------------------------------------------------------
async def _load_tenant_pool(db: AsyncSession, tenant_id: str) -> list[TenantResource]:
    """載入租戶資源池 (依 resource_type 排序, 輸出穩定)。"""
    result = await db.execute(
        select(TenantResource)
        .where(TenantResource.tenant_id == tenant_id)
        .order_by(TenantResource.resource_type)
    )
    return list(result.scalars().all())


def _pool_to_schema(rows: list[TenantResource]) -> list[TenantResourceSchema]:
    return [
        TenantResourceSchema(
            resource_type=r.resource_type,
            name=r.name or "",
            category=r.category or "labor",
            capacity=int(r.capacity or 0),
            unit_cost=float(r.unit_cost or 0),
            work_days=r.work_days or "1111100",
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Endpoints —— 租戶資源池 (pool)
# ---------------------------------------------------------------------------
@router.get("/pool", response_model=list[TenantResourceSchema])
async def get_pool(
    ctx: TenantContext = Depends(verify_tenant),
    db: AsyncSession = Depends(get_db),
) -> list[TenantResourceSchema]:
    """讀取租戶層級資源池。唯讀 (viewer 亦可)。"""
    rows = await _load_tenant_pool(db, ctx.tenant_id)
    return _pool_to_schema(rows)


@router.put("/pool", response_model=list[TenantResourceSchema])
async def set_pool(
    payload: list[TenantResourceSchema],
    ctx: TenantContext = Depends(verify_tenant),
    db: AsyncSession = Depends(get_db),
    _role: None = Depends(require_role("editor")),
) -> list[TenantResourceSchema]:
    """upsert 租戶資源池 (依 resource_type；payload 未列出的既有列保留不動)。

    tenant_id 一律取自 ctx (寫入隔離，絕不信任輸入)。
    """
    existing = await _load_tenant_pool(db, ctx.tenant_id)
    by_type = {r.resource_type: r for r in existing}

    for item in payload:
        row = by_type.get(item.resource_type)
        if row is not None:
            row.name = item.name or ""
            row.category = item.category or "labor"
            row.capacity = int(item.capacity)
            row.unit_cost = float(item.unit_cost)
            row.work_days = item.work_days
        else:
            db.add(
                TenantResource(
                    tenant_id=ctx.tenant_id,
                    resource_type=item.resource_type,
                    name=item.name or "",
                    category=item.category or "labor",
                    capacity=int(item.capacity),
                    unit_cost=float(item.unit_cost),
                    work_days=item.work_days,
                )
            )

    await db.flush()

    rows = await _load_tenant_pool(db, ctx.tenant_id)
    return _pool_to_schema(rows)


# ---------------------------------------------------------------------------
# Endpoints —— 投資組合資源分配 (allocation)
# ---------------------------------------------------------------------------
@router.get("/allocation", response_model=ResourceAllocationResult)
async def get_allocation(
    ctx: TenantContext = Depends(verify_tenant),
    db: AsyncSession = Depends(get_db),
) -> ResourceAllocationResult:
    """投資組合資源分配剖面 (跨租戶所有專案的週別峰值需求 vs 資源池產能)。

    唯讀 (不寫入、不派工); viewer 角色亦可存取 (不加 require_role)。
    固定 4 個查詢，與專案數無關 (見模組 docstring)。
    """
    # ---- Q1: 租戶資源池 ----
    pool = await _load_tenant_pool(db, ctx.tenant_id)

    # ---- Q2: 本租戶專案 (排除軟刪除) ----
    proj_result = await db.execute(
        select(Project).where(
            Project.tenant_id == ctx.tenant_id,
            Project.deleted_at.is_(None),
        )
    )
    projects = list(proj_result.scalars().all())

    if not projects:
        return compute_resource_allocation([], pool)

    project_ids = [p.project_id for p in projects]

    # ---- Q3: 這些專案的所有任務 (es/ef/resource_demands) ----
    task_result = await db.execute(
        select(
            Task.project_id, Task.task_id, Task.es, Task.ef, Task.resource_demands
        ).where(Task.project_id.in_(project_ids))
    )
    tasks_by_project: dict[str, list[dict]] = {}
    for pid, task_id, es, ef, demands in task_result.all():
        tasks_by_project.setdefault(pid, []).append(
            {
                "task_id": task_id,
                "es": int(es or 0),
                "ef": int(ef or 0),
                "demands": {
                    str(k): int(v) for k, v in dict(demands or {}).items()
                },
            }
        )

    # ---- Q4: 這些專案的所有假日 ----
    holiday_result = await db.execute(
        select(ProjectHoliday.project_id, ProjectHoliday.holiday_date).where(
            ProjectHoliday.project_id.in_(project_ids)
        )
    )
    holidays_by_project: dict[str, set] = {}
    for pid, hdate in holiday_result.all():
        holidays_by_project.setdefault(pid, set()).add(hdate)

    portfolio_projects = [
        {
            "project_id": p.project_id,
            "start_date": p.start_date,
            "work_days": p.work_days or "1111110",
            "holidays": holidays_by_project.get(p.project_id, set()),
            "tasks": tasks_by_project.get(p.project_id, []),
        }
        for p in projects
    ]

    return compute_resource_allocation(portfolio_projects, pool)
