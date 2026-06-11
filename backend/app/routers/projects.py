"""專案路由（Projects router）。

職責：
  - 專案 CRUD（建立 / 查詢 / 更新中繼資料 / 刪除）
  - 共用的 recompute_project() 助手：載入 DAG -> 跑 CPM -> 回寫 es/ef/ls/lf/float_time/is_critical -> 回傳 ProjectOut
  - PDF 報表串流（/report）

recompute_project() 為全系統共用的重算入口，create / add-task / update / duration / delete
等路徑都會呼叫它，確保 CPM 結果與 DB 一致，避免重複實作。
"""

from __future__ import annotations

import functools
import io
import uuid
import logging

import anyio
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select, delete as sa_delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import verify_tenant, get_db, TenantContext, require_role
from app.models.orm import Project, Task, TaskDependency
from app.schemas.schedule import (
    ProjectCreate,
    ProjectBase,
    ProjectOut,
    ProjectSummary,
    TaskDefinition,
    TaskResult,
)
from app.core.cpm_engine import calculate_cpm, project_duration, critical_path
from app.core import audit
from app.automation import reports

logger = logging.getLogger("cpm.routers.projects")

router = APIRouter(prefix="/projects", tags=["projects"])


# ---------------------------------------------------------------------------
# 內部工具：載入 / 組裝 / 回寫
# ---------------------------------------------------------------------------
async def _get_project_or_404(
    db: AsyncSession, project_id: str, tenant_id: str | None = None
) -> Project:
    """取得專案 ORM 物件，找不到回 404。

    租戶範圍：在 PostgreSQL 由 RLS (app.current_tenant) 強制隔離；在 sqlite
    (dev 原生模式，無 RLS) 則改以「應用層查詢條件」隔離 —— 故當呼叫端提供
    tenant_id 時，明確加上 ``Project.tenant_id == tenant_id``，確保兩種後端
    皆只回傳當前租戶的專案 (避免 sqlite 下跨租戶讀取)。
    """
    stmt = select(Project).where(Project.project_id == project_id)
    if tenant_id is not None:
        stmt = stmt.where(Project.tenant_id == tenant_id)
    result = await db.execute(stmt)
    project = result.scalar_one_or_none()
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Project '{project_id}' not found",
        )
    return project


async def _load_tasks(db: AsyncSession, project_id: str) -> list[Task]:
    """載入專案所有任務（依 task_id 排序，輸出穩定）。"""
    result = await db.execute(
        select(Task).where(Task.project_id == project_id).order_by(Task.task_id)
    )
    return list(result.scalars().all())


async def _load_dependencies(db: AsyncSession, project_id: str) -> list[TaskDependency]:
    """載入專案所有相依關係。"""
    result = await db.execute(
        select(TaskDependency).where(TaskDependency.project_id == project_id)
    )
    return list(result.scalars().all())


def _build_task_definitions(
    tasks: list[Task], deps: list[TaskDependency]
) -> list[TaskDefinition]:
    """將 ORM 任務 + 相依 組成 CPM 引擎用的 TaskDefinition 清單。"""
    pred_map: dict[str, list[str]] = {}
    for d in deps:
        pred_map.setdefault(d.task_id, []).append(d.predecessor_task_id)

    definitions: list[TaskDefinition] = []
    for t in tasks:
        definitions.append(
            TaskDefinition(
                task_id=t.task_id,
                task_name=t.task_name or "",
                duration=t.duration or 0,
                predecessors=pred_map.get(t.task_id, []),
                status=t.status or "PENDING",
            )
        )
    return definitions


def _to_project_out(
    project: Project,
    tasks: list[Task],
    deps: list[TaskDependency],
    task_results: dict[str, TaskResult],
    duration: int,
) -> ProjectOut:
    """組裝 ProjectOut（合併 CPM 計算結果）。"""
    pred_map: dict[str, list[str]] = {}
    for d in deps:
        pred_map.setdefault(d.task_id, []).append(d.predecessor_task_id)

    out_tasks: list[TaskResult] = []
    for t in tasks:
        res = task_results.get(t.task_id)
        if res is not None:
            # 以引擎結果為準，但補回相依與名稱/狀態
            res.task_name = t.task_name or ""
            res.predecessors = pred_map.get(t.task_id, [])
            res.status = t.status or "PENDING"
            out_tasks.append(res)
        else:
            out_tasks.append(
                TaskResult(
                    task_id=t.task_id,
                    task_name=t.task_name or "",
                    duration=t.duration or 0,
                    predecessors=pred_map.get(t.task_id, []),
                    status=t.status or "PENDING",
                    es=t.es or 0,
                    ef=t.ef or 0,
                    ls=t.ls or 0,
                    lf=t.lf or 0,
                    float_time=t.float_time or 0,
                    is_critical=bool(t.is_critical),
                )
            )

    return ProjectOut(
        project_id=project.project_id,
        tenant_id=project.tenant_id,
        project_name=project.project_name,
        region=project.region,
        project_duration=duration,
        tasks=out_tasks,
    )


# ---------------------------------------------------------------------------
# 共用重算助手（全系統共用）
# ---------------------------------------------------------------------------
async def recompute_project(
    db: AsyncSession,
    project: Project,
    *,
    notify: bool = True,
) -> ProjectOut:
    """載入專案 DAG -> 跑 CPM -> 回寫每個任務的 es/ef/ls/lf/float_time/is_critical -> 回傳 ProjectOut。

    這是 create / add-task / update / duration / delete 等路徑共用的重算入口。
    CPM 為純函式（無 DB），此處負責持久化結果。

    notify: 已保留作呼叫端相容；FIX-4 後不再於請求交易內阻斷式發送通知 (no-op)。
    """
    tasks = await _load_tasks(db, project.project_id)
    deps = await _load_dependencies(db, project.project_id)

    definitions = _build_task_definitions(tasks, deps)

    # 空專案：不需計算
    if not definitions:
        await db.flush()
        return _to_project_out(project, tasks, deps, {}, 0)

    try:
        task_results = calculate_cpm(definitions)
    except ValueError as exc:
        # 環路或未知前置任務 -> 422（資料無法構成有效 DAG）
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"CPM calculation failed: {exc}",
        )

    duration = project_duration(task_results)

    # 回寫 CPM 結果到每筆任務
    for t in tasks:
        res = task_results.get(t.task_id)
        if res is None:
            continue
        t.es = res.es
        t.ef = res.ef
        t.ls = res.ls
        t.lf = res.lf
        t.float_time = res.float_time
        t.is_critical = res.is_critical

    await db.flush()

    project_out = _to_project_out(project, tasks, deps, task_results, duration)

    # 安全修正 (FIX-4): 移除每次編輯都阻斷請求交易的 best-effort 通知。
    # 原本 await notify (LINE / 釘釘 httpx, 10s timeout) 是在 get_db 的
    # session.begin() 交易內進行, 會延長交易並把外部 I/O 失敗風險帶進主流程,
    # 且逐編輯通知為雜訊。有意義的風險通知改由 risk_listener 以背景任務發送。
    # 保留 notify 參數僅為呼叫端相容 (目前為 no-op)。
    return project_out


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@router.get("", response_model=list[ProjectSummary])
async def list_projects(
    ctx: TenantContext = Depends(verify_tenant),
    db: AsyncSession = Depends(get_db),
) -> list[ProjectSummary]:
    """列出（當前 tenant 可見的）所有專案摘要。

    租戶隔離：PostgreSQL 由 RLS 處理；sqlite (無 RLS) 改以查詢條件
    ``Project.tenant_id == ctx.tenant_id`` 隔離，確保兩種後端皆只列出當前
    租戶的專案 (避免 sqlite dev 模式下跨租戶外洩)。明確過濾於 PostgreSQL 為
    冗餘但無害 (與 RLS 結果一致)。
    """
    result = await db.execute(
        select(Project)
        .where(Project.tenant_id == ctx.tenant_id)
        .order_by(Project.created_at)
    )
    projects = list(result.scalars().all())

    summaries: list[ProjectSummary] = []
    for p in projects:
        tasks = await _load_tasks(db, p.project_id)
        max_ef = max((t.ef or 0 for t in tasks), default=0)
        summaries.append(
            ProjectSummary(
                project_id=p.project_id,
                project_name=p.project_name,
                region=p.region,
                tenant_id=p.tenant_id,
                task_count=len(tasks),
                project_duration=max_ef,
            )
        )
    return summaries


@router.post("", response_model=ProjectOut, status_code=status.HTTP_201_CREATED)
async def create_project(
    payload: ProjectCreate,
    ctx: TenantContext = Depends(verify_tenant),
    db: AsyncSession = Depends(get_db),
    _role: None = Depends(require_role("editor")),
) -> ProjectOut:
    """建立專案：持久化任務 + 相依，跑 CPM，回寫結果。"""
    project_id = payload.project_id or f"PRJ-{uuid.uuid4().hex[:12].upper()}"

    # 重複檢查（同 tenant 下 project_id 唯一）
    existing = await db.execute(select(Project).where(Project.project_id == project_id))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Project '{project_id}' already exists",
        )

    project = Project(
        project_id=project_id,
        tenant_id=ctx.tenant_id,
        project_name=payload.project_name,
        region=payload.region or ctx.region,
    )
    db.add(project)
    await db.flush()

    # 寫入任務與相依
    for td in payload.schedule_data:
        db.add(
            Task(
                project_id=project_id,
                tenant_id=ctx.tenant_id,
                task_id=td.task_id,
                task_name=td.task_name or "",
                duration=td.duration,
                status=td.status or "PENDING",
            )
        )
    await db.flush()

    for td in payload.schedule_data:
        for pred in td.predecessors:
            db.add(
                TaskDependency(
                    project_id=project_id,
                    tenant_id=ctx.tenant_id,
                    task_id=td.task_id,
                    predecessor_task_id=pred,
                )
            )
    await db.flush()

    project_out = await recompute_project(db, project)

    # 稽核 (best-effort): 失敗僅記錄, 絕不中斷主要操作。
    try:
        await audit.log_action(
            db,
            ctx,
            "PROJECT_CREATE",
            {
                "project_id": project_id,
                "project_name": payload.project_name,
                "region": project.region,
                "task_count": len(payload.schedule_data),
            },
        )
    except Exception as exc:  # noqa: BLE001 - 稽核失敗不可中斷主要操作
        logger.warning("audit PROJECT_CREATE failed (ignored): %s", exc)

    return project_out


@router.get("/{project_id}", response_model=ProjectOut)
async def get_project(
    project_id: str,
    ctx: TenantContext = Depends(verify_tenant),
    db: AsyncSession = Depends(get_db),
) -> ProjectOut:
    """載入專案 DAG 並回傳快取的 CPM 結果；若缺值則重算。"""
    project = await _get_project_or_404(db, project_id, ctx.tenant_id)
    tasks = await _load_tasks(db, project_id)
    deps = await _load_dependencies(db, project_id)

    # 判斷是否需要重算：有任務但 ef 全為 0（從未算過）
    needs_recompute = bool(tasks) and all((t.ef or 0) == 0 for t in tasks)
    if needs_recompute:
        return await recompute_project(db, project, notify=False)

    definitions = _build_task_definitions(tasks, deps)
    if definitions:
        try:
            task_results = calculate_cpm(definitions)
            duration = project_duration(task_results)
        except ValueError:
            # 資料異常時退回 DB 內既有快取值
            task_results = {}
            duration = max((t.ef or 0 for t in tasks), default=0)
    else:
        task_results = {}
        duration = 0

    return _to_project_out(project, tasks, deps, task_results, duration)


@router.put("/{project_id}", response_model=ProjectOut)
async def update_project(
    project_id: str,
    payload: ProjectBase,
    ctx: TenantContext = Depends(verify_tenant),
    db: AsyncSession = Depends(get_db),
    _role: None = Depends(require_role("editor")),
) -> ProjectOut:
    """更新專案中繼資料（名稱 / 區域）。"""
    project = await _get_project_or_404(db, project_id, ctx.tenant_id)
    before = {"project_name": project.project_name, "region": project.region}
    project.project_name = payload.project_name
    project.region = payload.region or project.region
    await db.flush()

    # 稽核 (best-effort): 失敗僅記錄, 絕不中斷主要操作。
    try:
        await audit.log_action(
            db,
            ctx,
            "PROJECT_UPDATE",
            {
                "project_id": project_id,
                "before": before,
                "after": {
                    "project_name": project.project_name,
                    "region": project.region,
                },
            },
        )
    except Exception as exc:  # noqa: BLE001 - 稽核失敗不可中斷主要操作
        logger.warning("audit PROJECT_UPDATE failed (ignored): %s", exc)

    # 不需重算 CPM，但回傳完整 ProjectOut（含既有結果）
    return await get_project(project_id, ctx=ctx, db=db)


@router.delete("/{project_id}")
async def delete_project(
    project_id: str,
    ctx: TenantContext = Depends(verify_tenant),
    db: AsyncSession = Depends(get_db),
    _role: None = Depends(require_role("editor")),
) -> dict:
    """刪除專案（任務經 ON DELETE CASCADE 連帶刪除；相依手動清理）。"""
    project = await _get_project_or_404(db, project_id, ctx.tenant_id)
    project_name = project.project_name

    await db.execute(
        sa_delete(TaskDependency).where(TaskDependency.project_id == project_id)
    )
    await db.delete(project)
    await db.flush()

    # 稽核 (best-effort): 失敗僅記錄, 絕不中斷主要操作。
    try:
        await audit.log_action(
            db,
            ctx,
            "PROJECT_DELETE",
            {"project_id": project_id, "project_name": project_name},
        )
    except Exception as exc:  # noqa: BLE001 - 稽核失敗不可中斷主要操作
        logger.warning("audit PROJECT_DELETE failed (ignored): %s", exc)

    return {"ok": True}


@router.get("/{project_id}/report")
async def project_report(
    project_id: str,
    ctx: TenantContext = Depends(verify_tenant),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """產生工期 PDF 報表（StreamingResponse, application/pdf）。"""
    project = await _get_project_or_404(db, project_id, ctx.tenant_id)
    project_out = await get_project(project_id, ctx=ctx, db=db)

    # reportlab 產生 PDF 為 CPU 密集的同步作業, 以工作執行緒執行避免阻塞 event loop。
    pdf_bytes = await anyio.to_thread.run_sync(
        functools.partial(reports.generate_schedule_pdf, project_out, project.region)
    )
    filename = f"schedule_{project_id}.pdf"
    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )
