"""儀表板路由 (Dashboard router) —— Phase 10 投資組合彙總。

職責:
  GET /dashboard  -> {projects:[ProjectKpi], totals:{...}}
                     對「當前租戶」可見的所有專案做「便宜的」彙總:
                       * 以 _load_tasks + calculate_cpm 取得工期 / 要徑數 (CPU 輕量)。
                       * 若有基準線, 以 compute_evm 於 data_date=project_duration
                         取 spi / cpi (唯讀)。無基準線則 spi/cpi=None。
                       * pending_risk_events: 統計 erp_integration.sync_event_log
                         中 sync_type=RISK_PROVISION 且 status=PENDING、且 payload
                         project_id 對應該專案的事件數 (本租戶範圍)。

設計重點:
  * 「不」執行蒙地卡羅 (Monte Carlo) —— 過重, 不適合彙總端點 (見 SPEC)。
  * 重用 projects._load_tasks / _build_task_definitions、cpm_engine、
    evm.compute_evm 與 progress router 既有的基準線 / 進度載入助手, 不重複實作。
  * 唯讀 (read-only): 不寫入、不派工; viewer 角色亦可存取 (不加 require_role)。
  * 租戶隔離: PostgreSQL 由 RLS 強制; sqlite (dev) 由查詢條件
    (Project.tenant_id == ctx.tenant_id) 過濾。sync_event_log 無 RLS, 故
    一律於程式以 tenant_id 過濾。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cpm_engine import calculate_cpm, project_duration
from app.core.evm import compute_evm
from app.core.risk_listener import RISK_PROVISION_SYNC_TYPE
from app.deps import TenantContext, get_db, verify_tenant
from app.models.orm import Project, SyncEvent
from app.routers.progress import (
    _baseline_tasks_for_evm,
    _load_latest_baseline,
    _progress_map_for_evm,
)
from app.routers.projects import _build_task_definitions, _load_dependencies, _load_tasks
from app.schemas.dashboard import DashboardOut, DashboardTotals, ProjectKpi

logger = logging.getLogger("cpm.routers.dashboard")

router = APIRouter(tags=["dashboard"])


async def _pending_risk_counts(
    db: AsyncSession, tenant_id: str
) -> dict[str, int]:
    """統計本租戶各專案待處理的風險預警事件數 (RISK_PROVISION / PENDING)。

    sync_event_log 無 RLS, 故明確以 tenant_id 過濾; project_id 編碼於 payload
    (見 risk_listener: payload={"reason","project_id","detail"}), 因 JSON 取值
    在 sqlite / PostgreSQL 行為不一, 此處於 Python 端聚合 (事件量小, 可接受)。
    回傳 {project_id: count}。
    """
    result = await db.execute(
        select(SyncEvent.payload).where(
            SyncEvent.tenant_id == tenant_id,
            SyncEvent.sync_type == RISK_PROVISION_SYNC_TYPE,
            SyncEvent.status == "PENDING",
        )
    )
    counts: dict[str, int] = {}
    for (payload,) in result.all():
        pid = None
        if isinstance(payload, dict):
            pid = payload.get("project_id")
        if pid:
            counts[str(pid)] = counts.get(str(pid), 0) + 1
    return counts


async def _project_kpi(
    db: AsyncSession,
    project: Project,
    pending_by_project: dict[str, int],
) -> ProjectKpi:
    """計算單一專案的便宜 KPI 摘要 (CPM 工期/要徑 + 選配 EVM spi/cpi)。"""
    tasks = await _load_tasks(db, project.project_id)
    deps = await _load_dependencies(db, project.project_id)
    definitions = _build_task_definitions(tasks, deps)

    duration = 0
    critical_count = 0
    if definitions:
        try:
            task_results = calculate_cpm(definitions)
            duration = project_duration(task_results)
            critical_count = sum(1 for r in task_results.values() if r.is_critical)
        except ValueError:
            # 資料無法構成有效 DAG (環路 / 未知前置): 退回 DB 快取值, 不阻斷彙總。
            duration = max((t.ef or 0 for t in tasks), default=0)
            critical_count = sum(1 for t in tasks if bool(t.is_critical))

    # ---- 選配 EVM: 僅在有基準線時計算 spi/cpi (data_date = 專案總工期) ----
    has_baseline = False
    spi: float | None = None
    cpi: float | None = None
    baseline = await _load_latest_baseline(db, project.project_id)
    if baseline is not None:
        has_baseline = True
        baseline_tasks = _baseline_tasks_for_evm(baseline)
        progress = await _progress_map_for_evm(db, project.project_id)
        # data_date 採 CPM 工期; 若 CPM 不可得 (空 / 環路) 則退回基準線快照工期。
        snapshot = baseline.snapshot or {}
        data_date = duration if duration > 0 else int(snapshot.get("project_duration", 0))
        evm = compute_evm(baseline_tasks, progress, int(data_date))
        spi = evm.spi
        cpi = evm.cpi

    return ProjectKpi(
        project_id=project.project_id,
        project_name=project.project_name,
        region=project.region,
        task_count=len(tasks),
        project_duration=int(duration),
        critical_count=int(critical_count),
        has_baseline=has_baseline,
        spi=spi,
        cpi=cpi,
        pending_risk_events=int(pending_by_project.get(project.project_id, 0)),
    )


@router.get("/dashboard", response_model=DashboardOut)
async def get_dashboard(
    ctx: TenantContext = Depends(verify_tenant),
    db: AsyncSession = Depends(get_db),
) -> DashboardOut:
    """投資組合儀表板: 當前租戶所有專案的便宜 KPI 彙總 (唯讀)。

    每個專案: 以 CPM 取工期 / 要徑數; 有基準線時以 EVM 取 spi/cpi (data_date=
    project_duration); 並附上待處理風險預警事件數。「不」執行蒙地卡羅模擬。
    viewer 角色亦可存取 (純讀取, 不加 require_role)。
    """
    result = await db.execute(
        select(Project)
        .where(Project.tenant_id == ctx.tenant_id)
        .order_by(Project.created_at)
    )
    projects = list(result.scalars().all())

    pending_by_project = await _pending_risk_counts(db, ctx.tenant_id)

    kpis: list[ProjectKpi] = []
    for project in projects:
        kpis.append(await _project_kpi(db, project, pending_by_project))

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
