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

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import risk_listener
from app.core.resource_leveling import level_resources
from app.deps import TenantContext, get_db, verify_tenant
from app.models.orm import ProjectResourceLimit, Task
from app.routers.projects import (
    _build_task_definitions,
    _get_project_or_404,
    _load_dependencies,
    _load_tasks,
)
from app.schemas.analytics import (
    LevelingResult,
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
    limits: list[ProjectResourceLimit], tasks: list[Task]
) -> ResourceConfig:
    """組裝 ResourceConfig 回應 (限制 + 各任務需求)。"""
    return ResourceConfig(
        limits=[
            ResourceLimit(resource_type=lim.resource_type, max_capacity=int(lim.max_capacity))
            for lim in limits
        ],
        demands=_demands_from_tasks(tasks),
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
    """讀取專案資源設定: 限制取自 project_resource_limits, 需求取自 tasks.resource_demands。"""
    await _get_project_or_404(db, project_id, ctx.tenant_id)
    limits = await _load_resource_limits(db, project_id)
    tasks = await _load_tasks(db, project_id)
    return _build_resource_config(limits, tasks)


@router.put("/{project_id}/resources", response_model=ResourceConfig)
async def set_resources(
    project_id: str,
    payload: ResourceConfig,
    ctx: TenantContext = Depends(verify_tenant),
    db: AsyncSession = Depends(get_db),
) -> ResourceConfig:
    """upsert 資源限制 (project_resource_limits) 與各任務需求 (tasks.resource_demands)。

    - 限制: 依 (project_id, resource_type) upsert; payload 未列出的既有限制保留不動。
    - 需求: 依 task_id upsert 至對應 Task.resource_demands; payload 未列出的任務不變動。
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
        else:
            db.add(
                ProjectResourceLimit(
                    project_id=project_id,
                    tenant_id=ctx.tenant_id,
                    resource_type=lim_in.resource_type,
                    max_capacity=int(lim_in.max_capacity),
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

    # 回傳更新後的完整設定
    limits = await _load_resource_limits(db, project_id)
    tasks = await _load_tasks(db, project_id)
    return _build_resource_config(limits, tasks)


@router.post("/{project_id}/level", response_model=LevelingResult)
async def level_project(
    project_id: str,
    ctx: TenantContext = Depends(verify_tenant),
    db: AsyncSession = Depends(get_db),
) -> LevelingResult:
    """執行資源撫平: 載入任務/DAG/需求/限制 -> level_resources -> 回傳 LevelingResult。

    若撫平導致工期展延 (result.extended), 觸發 risk_listener.evaluate_and_dispatch
    (reason="LEVELING_EXTENSION"), 入列 RISK_PROVISION 事件並 best-effort 通知。
    """
    project = await _get_project_or_404(db, project_id, ctx.tenant_id)

    tasks = await _load_tasks(db, project_id)
    deps = await _load_dependencies(db, project_id)
    definitions = _build_task_definitions(tasks, deps)

    demands = _demands_from_tasks(tasks)
    limits = _limits_to_map(await _load_resource_limits(db, project_id))

    result: LevelingResult = level_resources(definitions, demands, limits)

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
