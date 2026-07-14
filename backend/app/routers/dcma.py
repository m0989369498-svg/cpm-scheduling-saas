"""DCMA 14-point 排程健康評估路由 (DCMA health router) —— Pro Batch D FEATURE D2。

職責:
  GET  /projects/{pid}/health?data_date=  計算並回傳 DCMA 14 點排程品質報告
                                            (DcmaReport)；唯讀。

設計重點:
  * 重用 projects._get_project_or_404 / _load_tasks / _load_dependencies，
    以及 progress 路由的 _load_active_baseline / _load_progress，不重複
    實作載入邏輯。
  * DCMA 評估引擎 (app.core.dcma.assess_dcma) 為純函式 (無 DB)。
  * tasks / deps 直接以「已持久化」的 ORM 列傳入 (與 SPEC「持久化 CPM 欄位」
    要求一致，不重跑 CPM)。
  * data_date 省略時預設為專案總工期 (max ef)。
  * 唯讀端點：不需 require_role。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dcma import assess_dcma
from app.deps import TenantContext, get_db, verify_tenant
from app.routers.progress import _load_active_baseline, _load_progress
from app.routers.projects import _get_project_or_404, _load_dependencies, _load_tasks
from app.schemas.dcma import DcmaReport

router = APIRouter(prefix="/projects", tags=["dcma"])


@router.get("/{project_id}/health", response_model=DcmaReport)
async def get_health(
    project_id: str,
    data_date: int | None = Query(
        default=None,
        description="資料截止日 (data date)；省略時預設為專案總工期。",
    ),
    ctx: TenantContext = Depends(verify_tenant),
    db: AsyncSession = Depends(get_db),
) -> DcmaReport:
    """計算 DCMA 14-point 排程健康評估 (唯讀，無副作用)。"""
    await _get_project_or_404(db, project_id, ctx.tenant_id)

    tasks = await _load_tasks(db, project_id)
    deps = await _load_dependencies(db, project_id)

    progress_rows = await _load_progress(db, project_id)
    progress = {
        r.task_id: {
            "percent_complete": int(r.percent_complete or 0),
            "actual_cost": float(r.actual_cost or 0),
            "actual_start_day": r.actual_start_day,
            "actual_finish_day": r.actual_finish_day,
        }
        for r in progress_rows
    }

    baseline = await _load_active_baseline(db, project_id)
    baseline_tasks = list((baseline.snapshot or {}).get("tasks", [])) if baseline else []

    project_duration = max((int(t.ef or 0) for t in tasks), default=0)
    if data_date is None:
        data_date = project_duration

    return assess_dcma(tasks, deps, progress, baseline_tasks, int(data_date))
