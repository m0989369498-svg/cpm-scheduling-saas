"""儀表板路由 (Dashboard router) —— Phase 10 投資組合彙總。

職責:
  GET /dashboard  -> {projects:[ProjectKpi], totals:{...}}
                     對「當前租戶」可見的所有專案做「便宜的」彙總。

設計重點 (Batch 4 PERF-1):
  * 「固定查詢數」(constant query count, <=5)，與專案數無關:
      Q1 本租戶專案 (deleted_at IS NULL，選配 limit/offset 分頁)。
      Q2 任務彙總 GROUP BY project_id: COUNT(*) / MAX(ef) /
         SUM(CASE WHEN is_critical THEN 1 ELSE 0 END)。
      Q3 每專案最新基準線: ROW_NUMBER() OVER (PARTITION BY project_id
         ORDER BY created_at DESC, id DESC) == 1 (sqlite>=3.25 / postgres 皆可)。
      Q4 task_progress 列 (僅查有基準線的專案; IN 子句)。
      Q5 待處理風險預警計數 GROUP BY sync_event_log.project_id
         (PERF-3 新增欄位 + 複合索引, 不再掃描 payload JSON)。
  * 「不」執行 calculate_cpm: 所有寫入路徑 (recompute_project) 都已持久化
    es/ef/ls/lf/float_time/is_critical, 彙總端直接讀 DB 欄位 (PERF-4 同理)。
  * EVM 仍重用 compute_evm (純函式) —— 以 Q3 快照 + Q4 進度於 Python 端計算,
    data_date 採 MAX(ef) (即 CPM 工期), 不可得時退回基準線快照工期 (語義同前)。
  * 「不」執行蒙地卡羅 (Monte Carlo) —— 過重, 不適合彙總端點 (見 SPEC)。
  * 唯讀 (read-only): 不寫入、不派工; viewer 角色亦可存取 (不加 require_role)。
  * 租戶隔離: PostgreSQL 由 RLS 強制; sqlite (dev) 由查詢條件
    (Project.tenant_id == ctx.tenant_id) 過濾。sync_event_log 無 RLS, 故
    一律於程式以 tenant_id 過濾。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.evm import compute_evm
from app.core.risk_listener import RISK_PROVISION_SYNC_TYPE
from app.deps import TenantContext, get_db, verify_tenant
from app.models.orm import Project, ProjectBaseline, SyncEvent, Task, TaskProgress
from app.schemas.dashboard import DashboardOut, DashboardTotals, ProjectKpi

logger = logging.getLogger("cpm.routers.dashboard")

router = APIRouter(tags=["dashboard"])


async def _pending_risk_counts(
    db: AsyncSession, tenant_id: str
) -> dict[str, int]:
    """統計本租戶各專案待處理的風險預警事件數 (RISK_PROVISION / PENDING)。

    PERF-3: sync_event_log 已有 project_id 欄位 + (tenant_id, sync_type, status)
    複合索引, 故以單一 GROUP BY 查詢取代「抓取全部 payload JSON 於 Python 端
    聚合」。sync_event_log 無 RLS, 故明確以 tenant_id 過濾。
    回傳 {project_id: count}。
    """
    result = await db.execute(
        select(SyncEvent.project_id, func.count())
        .where(
            SyncEvent.tenant_id == tenant_id,
            SyncEvent.sync_type == RISK_PROVISION_SYNC_TYPE,
            SyncEvent.status == "PENDING",
            SyncEvent.project_id.is_not(None),
        )
        .group_by(SyncEvent.project_id)
    )
    return {str(pid): int(cnt) for pid, cnt in result.all()}


def _snapshot_tasks_for_evm(snapshot: dict) -> list[dict]:
    """由基準線快照 dict 取出 EVM 引擎所需的任務清單。

    形狀同 progress._baseline_tasks_for_evm (該助手吃 ORM 列; 此處 Q3 已直接
    取回 snapshot JSON, 故以 dict 版本重作, 語義一致):
    [{task_id, es, duration, budget}]。
    """
    out: list[dict] = []
    for t in (snapshot or {}).get("tasks", []):
        out.append(
            {
                "task_id": t.get("task_id"),
                "es": int(t.get("es", 0)),
                "duration": int(t.get("duration", 0)),
                "budget": float(t.get("budget", 0)),
            }
        )
    return out


@router.get("/dashboard", response_model=DashboardOut)
async def get_dashboard(
    limit: int | None = Query(
        default=None, ge=1, description="選配分頁: 回傳專案數上限 (預設全部)。"
    ),
    offset: int | None = Query(
        default=None, ge=0, description="選配分頁: 起始偏移 (預設 0)。"
    ),
    ctx: TenantContext = Depends(verify_tenant),
    db: AsyncSession = Depends(get_db),
) -> DashboardOut:
    """投資組合儀表板: 當前租戶所有專案的便宜 KPI 彙總 (唯讀)。

    PERF-1: 固定 <=5 個查詢, 與專案數無關 (見模組 docstring)。每個專案:
    以「已持久化」的 CPM 結果欄位取工期 / 要徑數; 有基準線時以 EVM 取 spi/cpi;
    並附上待處理風險預警事件數。「不」執行蒙地卡羅模擬。
    viewer 角色亦可存取 (純讀取, 不加 require_role)。
    """
    # ---- Q1: 本租戶專案 (排除軟刪除), 選配 limit/offset 分頁 ----
    stmt = (
        select(Project)
        .where(
            Project.tenant_id == ctx.tenant_id,
            # FEAT-4 軟刪除: 已進回收桶的專案不納入儀表板彙總。
            Project.deleted_at.is_(None),
        )
        .order_by(Project.created_at)
    )
    if offset is not None:
        stmt = stmt.offset(offset)
    if limit is not None:
        stmt = stmt.limit(limit)
    result = await db.execute(stmt)
    projects = list(result.scalars().all())

    if not projects:
        return DashboardOut(projects=[], totals=DashboardTotals())

    project_ids = [p.project_id for p in projects]

    # ---- Q2: 任務彙總 (COUNT / MAX(ef) / 要徑數) GROUP BY project_id ----
    agg_result = await db.execute(
        select(
            Task.project_id,
            func.count().label("task_count"),
            func.max(Task.ef).label("max_ef"),
            func.sum(case((Task.is_critical.is_(True), 1), else_=0)).label(
                "critical_count"
            ),
        )
        .where(Task.project_id.in_(project_ids))
        .group_by(Task.project_id)
    )
    task_agg: dict[str, tuple[int, int, int]] = {
        row.project_id: (
            int(row.task_count or 0),
            int(row.max_ef or 0),
            int(row.critical_count or 0),
        )
        for row in agg_result.all()
    }

    # ---- Q3: 每專案最新基準線 (ROW_NUMBER 視窗函式; sqlite>=3.25 / postgres) ----
    rn = (
        func.row_number()
        .over(
            partition_by=ProjectBaseline.project_id,
            order_by=(
                ProjectBaseline.created_at.desc(),
                ProjectBaseline.id.desc(),
            ),
        )
        .label("rn")
    )
    baseline_subq = (
        select(ProjectBaseline.project_id, ProjectBaseline.snapshot, rn)
        .where(ProjectBaseline.project_id.in_(project_ids))
        .subquery()
    )
    baseline_result = await db.execute(
        select(baseline_subq.c.project_id, baseline_subq.c.snapshot).where(
            baseline_subq.c.rn == 1
        )
    )
    snapshot_by_project: dict[str, dict] = {
        row.project_id: (row.snapshot or {}) for row in baseline_result.all()
    }

    # ---- Q4: 進度列 (僅有基準線的專案才需要 EVM; 無則跳過此查詢) ----
    progress_by_project: dict[str, dict[str, dict]] = {}
    baseline_pids = list(snapshot_by_project.keys())
    if baseline_pids:
        progress_result = await db.execute(
            select(
                TaskProgress.project_id,
                TaskProgress.task_id,
                TaskProgress.percent_complete,
                TaskProgress.actual_cost,
            ).where(TaskProgress.project_id.in_(baseline_pids))
        )
        for pid, task_id, pct, cost in progress_result.all():
            progress_by_project.setdefault(pid, {})[task_id] = {
                "percent_complete": int(pct or 0),
                "actual_cost": float(cost or 0),
            }

    # ---- Q5: 待處理風險預警事件數 (PERF-3 索引化 GROUP BY) ----
    pending_by_project = await _pending_risk_counts(db, ctx.tenant_id)

    # ---- 純 Python 組裝 KPI: 無逐專案查詢、無 calculate_cpm ----
    kpis: list[ProjectKpi] = []
    for project in projects:
        task_count, duration, critical_count = task_agg.get(
            project.project_id, (0, 0, 0)
        )

        snapshot = snapshot_by_project.get(project.project_id)
        has_baseline = snapshot is not None
        spi: float | None = None
        cpi: float | None = None
        if snapshot is not None:
            baseline_tasks = _snapshot_tasks_for_evm(snapshot)
            progress = progress_by_project.get(project.project_id, {})
            # data_date 採持久化 CPM 工期 (MAX(ef)); 不可得 (未算過 / 空專案)
            # 則退回基準線快照工期 —— 語義同 Batch 3 版本。
            data_date = (
                duration if duration > 0 else int(snapshot.get("project_duration", 0))
            )
            evm = compute_evm(baseline_tasks, progress, int(data_date))
            spi = evm.spi
            cpi = evm.cpi

        kpis.append(
            ProjectKpi(
                project_id=project.project_id,
                project_name=project.project_name,
                region=project.region,
                task_count=task_count,
                project_duration=int(duration),
                critical_count=int(critical_count),
                has_baseline=has_baseline,
                spi=spi,
                cpi=cpi,
                pending_risk_events=int(
                    pending_by_project.get(project.project_id, 0)
                ),
            )
        )

    totals = DashboardTotals(
        project_count=len(kpis),
        task_count=sum(k.task_count for k in kpis),
        critical_count=sum(k.critical_count for k in kpis),
        baseline_count=sum(1 for k in kpis if k.has_baseline),
        pending_risk_events=sum(k.pending_risk_events for k in kpis),
        at_risk_count=sum(
            1
            for k in kpis
            if (k.spi is not None and k.spi < 1.0)
            or (k.cpi is not None and k.cpi < 1.0)
        ),
    )

    return DashboardOut(projects=kpis, totals=totals)
