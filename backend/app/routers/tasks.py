"""任務路由（Tasks router）。

涵蓋專案內任務的 CRUD，所有寫入操作完成後都會呼叫共用的
recompute_project() 重算整個專案 CPM 並回傳新的 ProjectOut。
其中 PUT .../duration 為前端「拖曳改工期即時重算」的主要路徑。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select, delete as sa_delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.deps import verify_tenant, get_db, TenantContext, require_role
from app.models.orm import Task, TaskDependency
from app.schemas.schedule import (
    DependencyLink,
    TaskCreate,
    TaskUpdate,
    TaskDurationUpdate,
    TaskResult,
    ProjectOut,
)
from app.routers.projects import (
    recompute_project,
    version_conflict_response,
    _dependency_maps,
    _get_project_or_404,
    _load_tasks,
    _load_dependencies,
)

logger = logging.getLogger("cpm.routers.tasks")

router = APIRouter(prefix="/projects", tags=["tasks"])


# ---------------------------------------------------------------------------
# 內部工具
# ---------------------------------------------------------------------------
async def _get_task_or_404(db: AsyncSession, project_id: str, task_id: str) -> Task:
    """取得專案內指定任務，找不到回 404。"""
    result = await db.execute(
        select(Task).where(
            Task.project_id == project_id,
            Task.task_id == task_id,
        )
    )
    task = result.scalar_one_or_none()
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task '{task_id}' not found in project '{project_id}'",
        )
    return task


async def _replace_predecessors(
    db: AsyncSession,
    project_id: str,
    tenant_id: str,
    task_id: str,
    predecessors: list[str],
    links: list[DependencyLink] | None = None,
) -> None:
    """以新的相依關係覆寫指定任務的前置設定。

    FEAT-1：links 提供時以 links 為準（含 dep_type / lag_days；predecessors
    參數被忽略）；links 為 None 時沿用 predecessors（視為傳統 FS + lag 0）。
    """
    await db.execute(
        sa_delete(TaskDependency).where(
            TaskDependency.project_id == project_id,
            TaskDependency.task_id == task_id,
        )
    )
    if links is not None:
        # 去重（以 predecessor_task_id），避免違反 UNIQUE 約束。
        seen: set[str] = set()
        for link in links:
            if link.predecessor_task_id in seen:
                continue
            seen.add(link.predecessor_task_id)
            db.add(
                TaskDependency(
                    project_id=project_id,
                    tenant_id=tenant_id,
                    task_id=task_id,
                    predecessor_task_id=link.predecessor_task_id,
                    dep_type=link.dep_type or "FS",
                    lag_days=int(link.lag_days or 0),
                )
            )
    else:
        # 去重，避免違反 UNIQUE(project_id, task_id, predecessor_task_id)
        for pred in dict.fromkeys(predecessors):
            db.add(
                TaskDependency(
                    project_id=project_id,
                    tenant_id=tenant_id,
                    task_id=task_id,
                    predecessor_task_id=pred,
                    dep_type="FS",
                    lag_days=0,
                )
            )
    await db.flush()


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@router.get("/{project_id}/tasks", response_model=list[TaskResult])
async def list_tasks(
    project_id: str,
    ctx: TenantContext = Depends(verify_tenant),
    db: AsyncSession = Depends(get_db),
) -> list[TaskResult]:
    """列出專案任務（含「已持久化」的 CPM 結果欄位；PERF-4）。

    recompute_project() 於所有寫入路徑都會持久化 es/ef/ls/lf/float_time/
    is_critical，故此讀取端點直接以 Task 列組裝回應，「不」重跑 calculate_cpm。
    僅當結果「看似未算過」（每個任務 es==0 且 ef==0、且存在 duration>0 者）
    時重算一次後回應（兼容歷史資料 / 外部直寫）。
    """
    project = await _get_project_or_404(db, project_id, ctx.tenant_id)
    tasks = await _load_tasks(db, project_id)
    deps = await _load_dependencies(db, project_id)

    needs_recompute = (
        bool(tasks)
        and all((t.es or 0) == 0 and (t.ef or 0) == 0 for t in tasks)
        and any((t.duration or 0) > 0 for t in tasks)
    )
    if needs_recompute:
        project_out = await recompute_project(db, project, notify=False)
        return list(project_out.tasks)

    # FEAT-1：predecessors（向下相容）+ links（dep_type/lag_days）雙視圖。
    pred_map, links_map = _dependency_maps(deps)

    out: list[TaskResult] = []
    for t in tasks:
        out.append(
            TaskResult(
                task_id=t.task_id,
                task_name=t.task_name or "",
                duration=t.duration or 0,
                predecessors=pred_map.get(t.task_id, []),
                links=links_map.get(t.task_id, []),
                status=t.status or "PENDING",
                es=t.es or 0,
                ef=t.ef or 0,
                ls=t.ls or 0,
                lf=t.lf or 0,
                float_time=t.float_time or 0,
                is_critical=bool(t.is_critical),
            )
        )
    return out


@router.post(
    "/{project_id}/tasks",
    response_model=ProjectOut,
    status_code=status.HTTP_201_CREATED,
)
async def add_task(
    project_id: str,
    payload: TaskCreate,
    ctx: TenantContext = Depends(verify_tenant),
    db: AsyncSession = Depends(get_db),
    _role: None = Depends(require_role("editor")),
) -> ProjectOut:
    """新增任務後重算整個專案 CPM。

    FEAT-1：payload.links 提供時以 links 為準（predecessors 被忽略並重新推導）。
    FEAT-3：payload.expected_version 提供且不符當前版本 -> 409 版本衝突。
    """
    project = await _get_project_or_404(db, project_id, ctx.tenant_id)

    # FEAT-3 樂觀併發：先檢查版本，衝突即不做任何寫入。
    conflict = version_conflict_response(project, payload.expected_version)
    if conflict is not None:
        return conflict

    # 同專案內 task_id 不可重複
    existing = await db.execute(
        select(Task).where(
            Task.project_id == project_id,
            Task.task_id == payload.task_id,
        )
    )
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Task '{payload.task_id}' already exists in project '{project_id}'",
        )

    db.add(
        Task(
            project_id=project_id,
            tenant_id=ctx.tenant_id,
            task_id=payload.task_id,
            task_name=payload.task_name or "",
            duration=payload.duration,
            status=payload.status or "PENDING",
        )
    )
    await db.flush()

    if payload.links is not None:
        # links 提供時為準（predecessors 被忽略）。
        await _replace_predecessors(
            db, project_id, ctx.tenant_id, payload.task_id, [], links=payload.links
        )
    elif payload.predecessors:
        await _replace_predecessors(
            db, project_id, ctx.tenant_id, payload.task_id, payload.predecessors
        )

    project_out = await recompute_project(db, project)

    # 稽核 (best-effort): 失敗僅記錄, 絕不中斷主要操作。
    try:
        await audit.log_action(
            db,
            ctx,
            "TASK_CREATE",
            {
                "project_id": project_id,
                "task_id": payload.task_id,
                "duration": payload.duration,
                "predecessors": list(payload.predecessors or []),
                "links": (
                    [link.model_dump() for link in payload.links]
                    if payload.links is not None
                    else None
                ),
            },
        )
    except Exception as exc:  # noqa: BLE001 - 稽核失敗不可中斷主要操作
        logger.warning("audit TASK_CREATE failed (ignored): %s", exc)

    return project_out


@router.put("/{project_id}/tasks/{task_id}", response_model=ProjectOut)
async def update_task(
    project_id: str,
    task_id: str,
    payload: TaskUpdate,
    ctx: TenantContext = Depends(verify_tenant),
    db: AsyncSession = Depends(get_db),
    _role: None = Depends(require_role("editor")),
) -> ProjectOut:
    """部分更新任務（名稱 / 工期 / 狀態 / 前置任務 / 相依連結）後重算 CPM。

    FEAT-1：payload.links 提供時以 links 為準（predecessors 被忽略並重新推導）。
    FEAT-3：payload.expected_version 提供且不符當前版本 -> 409 版本衝突。
    """
    project = await _get_project_or_404(db, project_id, ctx.tenant_id)

    # FEAT-3 樂觀併發：先檢查版本，衝突即不做任何寫入。
    conflict = version_conflict_response(project, payload.expected_version)
    if conflict is not None:
        return conflict

    task = await _get_task_or_404(db, project_id, task_id)

    changed: dict[str, object] = {}
    if payload.task_name is not None:
        task.task_name = payload.task_name
        changed["task_name"] = payload.task_name
    if payload.duration is not None:
        task.duration = payload.duration
        changed["duration"] = payload.duration
    if payload.status is not None:
        task.status = payload.status
        changed["status"] = payload.status
    await db.flush()

    if payload.links is not None:
        # links 提供時為準（predecessors 被忽略並由 links 重新推導）。
        await _replace_predecessors(
            db, project_id, ctx.tenant_id, task_id, [], links=payload.links
        )
        changed["links"] = [link.model_dump() for link in payload.links]
    elif payload.predecessors is not None:
        await _replace_predecessors(
            db, project_id, ctx.tenant_id, task_id, payload.predecessors
        )
        changed["predecessors"] = list(payload.predecessors)

    project_out = await recompute_project(db, project)

    # 稽核 (best-effort): 失敗僅記錄, 絕不中斷主要操作。
    try:
        await audit.log_action(
            db,
            ctx,
            "TASK_UPDATE",
            {"project_id": project_id, "task_id": task_id, "changed": changed},
        )
    except Exception as exc:  # noqa: BLE001 - 稽核失敗不可中斷主要操作
        logger.warning("audit TASK_UPDATE failed (ignored): %s", exc)

    return project_out


@router.put("/{project_id}/tasks/{task_id}/duration", response_model=ProjectOut)
async def update_task_duration(
    project_id: str,
    task_id: str,
    payload: TaskDurationUpdate,
    ctx: TenantContext = Depends(verify_tenant),
    db: AsyncSession = Depends(get_db),
    _role: None = Depends(require_role("editor")),
) -> ProjectOut:
    """拖曳改工期路徑：更新工期後重算整個專案 CPM 並回傳最新 ProjectOut。

    FEAT-3：payload.expected_version 提供且不符當前版本 -> 409 版本衝突。
    """
    project = await _get_project_or_404(db, project_id, ctx.tenant_id)

    # FEAT-3 樂觀併發：先檢查版本，衝突即不做任何寫入。
    conflict = version_conflict_response(project, payload.expected_version)
    if conflict is not None:
        return conflict

    task = await _get_task_or_404(db, project_id, task_id)

    old_duration = task.duration
    task.duration = payload.duration
    await db.flush()

    project_out = await recompute_project(db, project)

    # 稽核 (best-effort): 失敗僅記錄, 絕不中斷主要操作。
    try:
        await audit.log_action(
            db,
            ctx,
            "TASK_DURATION_UPDATE",
            {
                "project_id": project_id,
                "task_id": task_id,
                "before": old_duration,
                "after": payload.duration,
            },
        )
    except Exception as exc:  # noqa: BLE001 - 稽核失敗不可中斷主要操作
        logger.warning("audit TASK_DURATION_UPDATE failed (ignored): %s", exc)

    return project_out


@router.delete("/{project_id}/tasks/{task_id}", response_model=ProjectOut)
async def delete_task(
    project_id: str,
    task_id: str,
    ctx: TenantContext = Depends(verify_tenant),
    db: AsyncSession = Depends(get_db),
    _role: None = Depends(require_role("editor")),
) -> ProjectOut:
    """刪除任務及其相依（含其他任務以它為前置者）後重算 CPM。"""
    project = await _get_project_or_404(db, project_id, ctx.tenant_id)
    task = await _get_task_or_404(db, project_id, task_id)

    # 刪除以此任務為「自身」或「前置」的相依關係
    await db.execute(
        sa_delete(TaskDependency).where(
            TaskDependency.project_id == project_id,
            (TaskDependency.task_id == task_id)
            | (TaskDependency.predecessor_task_id == task_id),
        )
    )
    await db.delete(task)
    await db.flush()

    project_out = await recompute_project(db, project)

    # 稽核 (best-effort): 失敗僅記錄, 絕不中斷主要操作。
    try:
        await audit.log_action(
            db,
            ctx,
            "TASK_DELETE",
            {"project_id": project_id, "task_id": task_id},
        )
    except Exception as exc:  # noqa: BLE001 - 稽核失敗不可中斷主要操作
        logger.warning("audit TASK_DELETE failed (ignored): %s", exc)

    return project_out
