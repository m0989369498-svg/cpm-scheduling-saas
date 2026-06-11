"""進度 / 實獲值路由 (Progress / EVM router) —— Phase 9 進度追蹤 + 實獲值管理。

職責:
  GET  /projects/{pid}/progress       讀取各任務進度 (task_progress); 無列者預設 budget0/pct0。
  PUT  /projects/{pid}/progress       upsert 各任務進度 (per (project_id, task_id))。
  POST /projects/{pid}/baseline       由 _load_tasks -> calculate_cpm 取得 es/ef/duration,
                                      budget 取自 task_progress (預設 0), 存成 snapshot 並回傳。
  GET  /projects/{pid}/baseline       取最新基準線 (max created_at / max id); 無則 404。
  GET  /projects/{pid}/evm            以最新基準線計算 EVM (唯讀, 無副作用); 無基準線回 409。
  POST /projects/{pid}/evm/alert      重算 EVM; 若 risk_flagged 則拋轉風險預警 (risk_listener)。

設計重點:
  * 重用 projects._get_project_or_404 / _load_tasks / _load_dependencies /
    _build_task_definitions, 不重複實作載入與 DAG 組裝。
  * EVM 引擎 (app.core.evm.compute_evm) 為純函式 (無 DB)。
  * 基準線快照 (project_baselines.snapshot) 為計算時的凍結基準, EVM 一律以快照為準
    (而非當下 CPM), 確保「計畫 vs 實際」可追溯。
  * 租戶隔離: PostgreSQL 由 RLS 強制; sqlite (dev) 由 _get_project_or_404 以 tenant_id
    過濾。寫入時 tenant_id 一律取自 ctx (絕不信任輸入)。
  * /evm 為唯讀 (不寫入、不派工); 僅 /evm/alert 在 risk_flagged 時呼叫 risk_listener。
  * task_progress / project_baselines 皆位於 public schema (受 RLS 保護)。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit, risk_listener
from app.core.cpm_engine import calculate_cpm, project_duration
from app.core.evm import compute_evm
from app.core.i18n import t
from app.deps import TenantContext, get_db, require_role, verify_tenant
from app.models.orm import ProjectBaseline, TaskProgress
from app.routers.projects import (
    _build_task_definitions,
    _get_project_or_404,
    _load_dependencies,
    _load_tasks,
)
from app.schemas.evm import BaselineOut, EvmResult, ProgressEntry

logger = logging.getLogger("cpm.routers.progress")

router = APIRouter(prefix="/projects", tags=["progress"])

# 基準線建立通知標題 (雙語; 與 i18n 風格一致 —— TW 繁體 / CN 簡體)
_BASELINE_CREATED_TITLE = {
    "TW": "基準線已建立",
    "CN": "基准线已建立",
}


# ---------------------------------------------------------------------------
# 內部工具
# ---------------------------------------------------------------------------
async def _load_progress(db: AsyncSession, project_id: str) -> list[TaskProgress]:
    """載入專案所有進度列 (依 task_id 排序, 輸出穩定)。"""
    result = await db.execute(
        select(TaskProgress)
        .where(TaskProgress.project_id == project_id)
        .order_by(TaskProgress.task_id)
    )
    return list(result.scalars().all())


def _progress_to_schema(row: TaskProgress) -> ProgressEntry:
    """ORM -> ProgressEntry (回應)。"""
    return ProgressEntry(
        task_id=row.task_id,
        budget=float(row.budget or 0),
        percent_complete=int(row.percent_complete or 0),
        actual_cost=float(row.actual_cost or 0),
        actual_start_day=row.actual_start_day,
        actual_finish_day=row.actual_finish_day,
    )


async def _load_latest_baseline(
    db: AsyncSession, project_id: str
) -> ProjectBaseline | None:
    """取得最新基準線 (max created_at, 同時間以 max id 決勝); 無則回 None。"""
    result = await db.execute(
        select(ProjectBaseline)
        .where(ProjectBaseline.project_id == project_id)
        .order_by(ProjectBaseline.created_at.desc(), ProjectBaseline.id.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


def _baseline_to_schema(row: ProjectBaseline) -> BaselineOut:
    """ORM -> BaselineOut。snapshot 為凍結的 CPM + 預算快照。"""
    snapshot = row.snapshot or {}
    tasks = list(snapshot.get("tasks", []))
    return BaselineOut(
        id=int(row.id),
        name=row.name,
        project_duration=int(snapshot.get("project_duration", 0)),
        created_at=row.created_at.isoformat() if row.created_at is not None else "",
        tasks=tasks,
    )


def _baseline_tasks_for_evm(row: ProjectBaseline) -> list[dict]:
    """由基準線快照取出 EVM 引擎所需的任務清單 [{task_id, es, duration, budget}]。"""
    snapshot = row.snapshot or {}
    out: list[dict] = []
    for t in snapshot.get("tasks", []):
        out.append(
            {
                "task_id": t.get("task_id"),
                "es": int(t.get("es", 0)),
                "duration": int(t.get("duration", 0)),
                "budget": float(t.get("budget", 0)),
            }
        )
    return out


async def _progress_map_for_evm(
    db: AsyncSession, project_id: str
) -> dict[str, dict]:
    """由 task_progress 組出 EVM 引擎所需的進度對映:
    {task_id: {percent_complete:int, actual_cost:float}}。
    """
    rows = await _load_progress(db, project_id)
    return {
        r.task_id: {
            "percent_complete": int(r.percent_complete or 0),
            "actual_cost": float(r.actual_cost or 0),
        }
        for r in rows
    }


# ---------------------------------------------------------------------------
# Endpoints —— 進度 (progress)
# ---------------------------------------------------------------------------
@router.get("/{project_id}/progress", response_model=list[ProgressEntry])
async def get_progress(
    project_id: str,
    ctx: TenantContext = Depends(verify_tenant),
    db: AsyncSession = Depends(get_db),
) -> list[ProgressEntry]:
    """讀取專案各任務進度。

    回傳 task_progress 既有列; 尚無進度列的任務不在此補零 (前端可由排程任務清單
    自行補上預設 budget0/pct0)。輸出依 task_id 排序。
    """
    await _get_project_or_404(db, project_id, ctx.tenant_id)
    rows = await _load_progress(db, project_id)
    return [_progress_to_schema(r) for r in rows]


@router.put("/{project_id}/progress", response_model=list[ProgressEntry])
async def set_progress(
    project_id: str,
    payload: list[ProgressEntry],
    ctx: TenantContext = Depends(verify_tenant),
    db: AsyncSession = Depends(get_db),
    _role: None = Depends(require_role("editor")),
) -> list[ProgressEntry]:
    """upsert 各任務進度 (per (project_id, task_id))。

    - 依 task_id upsert 至 task_progress; payload 未列出的既有進度列保留不動。
    - tenant_id 一律取自 ctx (寫入隔離, 絕不信任輸入)。
    回傳更新後的完整進度清單 (依 task_id 排序)。
    """
    await _get_project_or_404(db, project_id, ctx.tenant_id)

    existing = await _load_progress(db, project_id)
    by_task = {row.task_id: row for row in existing}

    for entry in payload:
        row = by_task.get(entry.task_id)
        if row is not None:
            row.tenant_id = ctx.tenant_id
            row.budget = float(entry.budget)
            row.percent_complete = int(entry.percent_complete)
            row.actual_cost = float(entry.actual_cost)
            row.actual_start_day = entry.actual_start_day
            row.actual_finish_day = entry.actual_finish_day
        else:
            db.add(
                TaskProgress(
                    project_id=project_id,
                    tenant_id=ctx.tenant_id,
                    task_id=entry.task_id,
                    budget=float(entry.budget),
                    percent_complete=int(entry.percent_complete),
                    actual_cost=float(entry.actual_cost),
                    actual_start_day=entry.actual_start_day,
                    actual_finish_day=entry.actual_finish_day,
                )
            )

    await db.flush()

    # 稽核 (best-effort): 失敗僅記錄, 絕不中斷主要操作。
    try:
        await audit.log_action(
            db,
            ctx,
            "PROGRESS_UPDATE",
            {
                "project_id": project_id,
                "task_ids": [e.task_id for e in payload],
            },
        )
    except Exception as exc:  # noqa: BLE001 - 稽核失敗不可中斷主要操作
        logger.warning("audit PROGRESS_UPDATE failed (ignored): %s", exc)

    rows = await _load_progress(db, project_id)
    return [_progress_to_schema(r) for r in rows]


# ---------------------------------------------------------------------------
# Endpoints —— 基準線 (baseline)
# ---------------------------------------------------------------------------
@router.post("/{project_id}/baseline", response_model=BaselineOut)
async def create_baseline(
    project_id: str,
    payload: dict | None = None,
    ctx: TenantContext = Depends(verify_tenant),
    db: AsyncSession = Depends(get_db),
    _role: None = Depends(require_role("editor")),
) -> BaselineOut:
    """建立 (凍結) 一條基準線。

    流程:
      1) _load_tasks + _load_dependencies -> _build_task_definitions。
      2) calculate_cpm 取得各任務 es/ef/duration 與專案總工期。
      3) budget 取自 task_progress (per task_id; 預設 0)。
      4) 組成 snapshot {"project_duration", "tasks":[{task_id,es,ef,duration,budget}]}
         並寫入 project_baselines (允許多條, 最新者為作用中)。
      5) 入列基準線建立通知 (notification_outbox; 與本交易原子提交, worker 投遞)。
    回傳新建立的 BaselineOut。
    """
    project = await _get_project_or_404(db, project_id, ctx.tenant_id)

    name = "baseline"
    if isinstance(payload, dict):
        raw_name = payload.get("name")
        if raw_name:
            name = str(raw_name)

    tasks = await _load_tasks(db, project_id)
    deps = await _load_dependencies(db, project_id)
    definitions = _build_task_definitions(tasks, deps)

    # 預算: 以 task_progress 為準 (預設 0)。
    progress_rows = await _load_progress(db, project_id)
    budget_by_task = {r.task_id: float(r.budget or 0) for r in progress_rows}

    if definitions:
        try:
            task_results = calculate_cpm(definitions)
        except ValueError as exc:
            # 資料無法構成有效 DAG (環路 / 未知前置) -> 422。
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"CPM calculation failed: {exc}",
            )
        duration = project_duration(task_results)
        snapshot_tasks = [
            {
                "task_id": res.task_id,
                "es": int(res.es),
                "ef": int(res.ef),
                "duration": int(res.duration),
                "budget": budget_by_task.get(res.task_id, 0.0),
            }
            for res in task_results.values()
        ]
        # 依 (es, ef, task_id) 排序, 輸出穩定。
        snapshot_tasks.sort(key=lambda t: (t["es"], t["ef"], t["task_id"]))
    else:
        duration = 0
        snapshot_tasks = []

    snapshot = {"project_duration": int(duration), "tasks": snapshot_tasks}

    baseline = ProjectBaseline(
        project_id=project_id,
        tenant_id=ctx.tenant_id,
        name=name,
        snapshot=snapshot,
    )
    db.add(baseline)
    await db.flush()
    # 取回 server_default (created_at) 等欄位, 確保回應完整。
    await db.refresh(baseline)

    # 稽核 (best-effort): 失敗僅記錄, 絕不中斷主要操作。
    try:
        await audit.log_action(
            db,
            ctx,
            "BASELINE_CREATE",
            {
                "project_id": project_id,
                "baseline_id": int(baseline.id),
                "name": name,
                "project_duration": int(duration),
            },
        )
    except Exception as exc:  # noqa: BLE001 - 稽核失敗不可中斷主要操作
        logger.warning("audit BASELINE_CREATE failed (ignored): %s", exc)

    # 基準線建立通知 (best-effort): 入列 notification_outbox (與本交易原子提交),
    # 由 worker.deliver_outbox_once 實際投遞。入列失敗僅記錄, 絕不中斷主要操作。
    try:
        region = (ctx.region or "TW").upper()
        title = _BASELINE_CREATED_TITLE.get(region, _BASELINE_CREATED_TITLE["TW"])
        message = (
            f"📌 {title}: {name}\n"
            f"{t(region, 'project')}: {project.project_name} ({project_id})\n"
            f"{t(region, 'projectDuration')}: {int(duration)} {t(region, 'days')}"
        )
        await risk_listener.enqueue_notification(db, ctx.tenant_id, region, message)
    except Exception as exc:  # noqa: BLE001 - 通知入列失敗不可中斷主要操作
        logger.warning(
            "baseline notification enqueue failed (ignored) project=%s: %s",
            project_id,
            exc,
        )

    return _baseline_to_schema(baseline)


@router.get("/{project_id}/baseline", response_model=BaselineOut)
async def get_baseline(
    project_id: str,
    ctx: TenantContext = Depends(verify_tenant),
    db: AsyncSession = Depends(get_db),
) -> BaselineOut:
    """取得最新 (作用中) 基準線; 若無任何基準線回 404。"""
    await _get_project_or_404(db, project_id, ctx.tenant_id)
    baseline = await _load_latest_baseline(db, project_id)
    if baseline is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No baseline found for project '{project_id}'",
        )
    return _baseline_to_schema(baseline)


# ---------------------------------------------------------------------------
# Endpoints —— 實獲值 (EVM)
# ---------------------------------------------------------------------------
@router.get("/{project_id}/evm", response_model=EvmResult)
async def get_evm(
    project_id: str,
    data_date: int | None = Query(
        default=None,
        description="資料截止日 (data date); 省略時預設為基準線專案總工期。",
    ),
    ctx: TenantContext = Depends(verify_tenant),
    db: AsyncSession = Depends(get_db),
) -> EvmResult:
    """計算 EVM (實獲值) —— 唯讀, 無任何副作用。

    以最新基準線為計畫基準, 進度取自 task_progress。data_date 省略時預設為基準線
    專案總工期。無基準線回 409 (需先建立基準線)。
    """
    await _get_project_or_404(db, project_id, ctx.tenant_id)

    baseline = await _load_latest_baseline(db, project_id)
    if baseline is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"No baseline for project '{project_id}'. "
                "Create a baseline before computing EVM."
            ),
        )

    snapshot = baseline.snapshot or {}
    if data_date is None:
        data_date = int(snapshot.get("project_duration", 0))

    baseline_tasks = _baseline_tasks_for_evm(baseline)
    progress = await _progress_map_for_evm(db, project_id)

    return compute_evm(baseline_tasks, progress, int(data_date))


@router.post("/{project_id}/evm/alert", response_model=dict)
async def dispatch_evm_alert(
    project_id: str,
    data_date: int | None = Query(
        default=None,
        description="資料截止日 (data date); 省略時預設為基準線專案總工期。",
    ),
    ctx: TenantContext = Depends(verify_tenant),
    db: AsyncSession = Depends(get_db),
    _role: None = Depends(require_role("editor")),
) -> dict:
    """重算 EVM; 若 risk_flagged 則拋轉排程/成本超支風險預警。

    risk_flagged 為真時呼叫 risk_listener.evaluate_and_dispatch
    (reason="SCHEDULE_COST_OVERRUN", detail={spi,cpi,sv,cv,eac,vac}), 入列
    RISK_PROVISION 事件並 best-effort 通知, 回傳 dispatched=true; 否則
    dispatched=false (不派工)。
    """
    project = await _get_project_or_404(db, project_id, ctx.tenant_id)

    baseline = await _load_latest_baseline(db, project_id)
    if baseline is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"No baseline for project '{project_id}'. "
                "Create a baseline before dispatching an EVM alert."
            ),
        )

    snapshot = baseline.snapshot or {}
    if data_date is None:
        data_date = int(snapshot.get("project_duration", 0))

    baseline_tasks = _baseline_tasks_for_evm(baseline)
    progress = await _progress_map_for_evm(db, project_id)

    evm: EvmResult = compute_evm(baseline_tasks, progress, int(data_date))

    if not evm.risk_flagged:
        return {
            "dispatched": False,
            "risk_flagged": False,
            "data_date": int(data_date),
            "spi": evm.spi,
            "cpi": evm.cpi,
        }

    detail = {
        "project_id": project.project_id,
        "data_date": int(data_date),
        "spi": evm.spi,
        "cpi": evm.cpi,
        "sv": evm.sv,
        "cv": evm.cv,
        "eac": evm.eac,
        "vac": evm.vac,
    }
    dispatch = await risk_listener.evaluate_and_dispatch(
        db,
        ctx,
        project.project_id,
        reason="SCHEDULE_COST_OVERRUN",
        detail=detail,
    )

    # 稽核 (best-effort): 失敗僅記錄, 絕不中斷主要操作。
    event_id = dispatch.get("event_id")
    try:
        await audit.log_action(
            db,
            ctx,
            "EVM_ALERT_DISPATCH",
            {
                "project_id": project.project_id,
                "data_date": int(data_date),
                "spi": evm.spi,
                "cpi": evm.cpi,
                "event_id": str(event_id) if event_id is not None else None,
            },
        )
    except Exception as exc:  # noqa: BLE001 - 稽核失敗不可中斷主要操作
        logger.warning("audit EVM_ALERT_DISPATCH failed (ignored): %s", exc)

    return {
        "dispatched": True,
        "risk_flagged": True,
        "data_date": int(data_date),
        "spi": evm.spi,
        "cpi": evm.cpi,
        "event_id": dispatch.get("event_id"),
        "notified": dispatch.get("notified", False),
    }
