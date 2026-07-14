"""成本負載路由 (Cost router) —— Pro Batch D FEATURE D1。

職責:
  GET  /projects/{pid}/cost   計算並回傳成本負載 (CostResult)；唯讀。

設計重點:
  * 重用 projects._get_project_or_404 / _load_tasks，不重複實作載入。
  * 成本負載引擎 (app.core.cost.compute_cost_loading) 為純函式 (無 DB)。
  * 費率 / 資源類別取自 project_resource_limits (unit_cost / category)；
    資源需求取自 tasks.resource_demands；WBS 歸屬取自 tasks.wbs_code；
    project_duration = max(ef)（與既有 ProjectSummary 彙總邏輯一致）。
  * 唯讀端點：不需 require_role，任何已通過租戶驗證的使用者皆可讀取。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cost import compute_cost_loading
from app.deps import TenantContext, get_db, verify_tenant
from app.models.orm import ProjectResourceLimit
from app.routers.projects import _get_project_or_404, _load_tasks
from app.schemas.cost import CostResult

router = APIRouter(prefix="/projects", tags=["cost"])


@router.get("/{project_id}/cost", response_model=CostResult)
async def get_cost(
    project_id: str,
    ctx: TenantContext = Depends(verify_tenant),
    db: AsyncSession = Depends(get_db),
) -> CostResult:
    """計算專案成本負載 (唯讀，無副作用)。"""
    await _get_project_or_404(db, project_id, ctx.tenant_id)
    tasks = await _load_tasks(db, project_id)

    result = await db.execute(
        select(ProjectResourceLimit).where(
            ProjectResourceLimit.project_id == project_id
        )
    )
    limits = list(result.scalars().all())
    rates = {lim.resource_type: float(lim.unit_cost or 0) for lim in limits}
    categories = {lim.resource_type: (lim.category or "labor") for lim in limits}

    demands: dict[str, dict[str, int]] = {}
    for t in tasks:
        rd = getattr(t, "resource_demands", None)
        if rd:
            demands[t.task_id] = {str(k): int(v) for k, v in dict(rd).items()}

    wbs_of = {t.task_id: t.wbs_code for t in tasks}
    project_duration = max((int(t.ef or 0) for t in tasks), default=0)

    return compute_cost_loading(
        tasks, demands, rates, categories, wbs_of, project_duration
    )
