"""分析路由 (Analytics router) —— Phase 8 風險參數 + 蒙地卡羅模擬。

職責:
  GET  /projects/{pid}/risk      讀取各任務三點估計 (task_risk_parameters)。
  PUT  /projects/{pid}/risk      upsert 三點估計 (輸入忽略 criticality_index)。
  POST /projects/{pid}/simulate  執行蒙地卡羅模擬, 回傳 SimulationResult, 並把各任務
                                 criticality_index 回寫 task_risk_parameters;
                                 若有合約工期且準時機率 < 0.70, 觸發風險自動化。

設計重點:
  * 重用 projects._get_project_or_404 / _load_tasks / _load_dependencies /
    _build_task_definitions。
  * 模擬引擎 (app.core.monte_carlo.simulate_schedule) 為純函式 (無 DB)。
  * 缺三點估計的任務退回其基礎工期 (base duration) 作為固定值。
  * 租戶隔離同 resources router: 讀以 _get_project_or_404 過濾, 寫入 tenant_id 取自 ctx。
"""

from __future__ import annotations

import functools
import logging

import anyio
from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit, risk_listener
from app.core.monte_carlo import simulate_schedule
from app.deps import TenantContext, get_db, require_role, verify_tenant
from app.models.orm import Task, TaskRiskParameter
from app.routers.projects import (
    _build_task_definitions,
    _get_project_or_404,
    _load_dependencies,
    _load_tasks,
)
from app.schemas.analytics import (
    RiskParam,
    SimulationRequest,
    SimulationResult,
)

logger = logging.getLogger("cpm.routers.analytics")

router = APIRouter(prefix="/projects", tags=["analytics"])

# 準時機率低於此門檻 (且有設定合約工期) 即觸發風險自動化
_ONTIME_THRESHOLD = 0.70


# ---------------------------------------------------------------------------
# 內部工具
# ---------------------------------------------------------------------------
async def _load_risk_params(
    db: AsyncSession, project_id: str
) -> list[TaskRiskParameter]:
    """載入專案的三點估計風險參數 (依 task_id 排序)。"""
    result = await db.execute(
        select(TaskRiskParameter)
        .where(TaskRiskParameter.project_id == project_id)
        .order_by(TaskRiskParameter.task_id)
    )
    return list(result.scalars().all())


def _risk_param_to_schema(row: TaskRiskParameter) -> RiskParam:
    """ORM -> RiskParam (回應)。"""
    return RiskParam(
        task_id=row.task_id,
        optimistic_duration=int(row.optimistic_duration),
        most_likely_duration=int(row.most_likely_duration),
        pessimistic_duration=int(row.pessimistic_duration),
        criticality_index=float(row.criticality_index or 0.0),
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@router.get("/{project_id}/risk", response_model=list[RiskParam])
async def get_risk(
    project_id: str,
    ctx: TenantContext = Depends(verify_tenant),
    db: AsyncSession = Depends(get_db),
) -> list[RiskParam]:
    """讀取專案各任務的三點估計與 (上次模擬得到的) criticality_index。"""
    await _get_project_or_404(db, project_id, ctx.tenant_id)
    rows = await _load_risk_params(db, project_id)
    return [_risk_param_to_schema(r) for r in rows]


@router.put("/{project_id}/risk", response_model=list[RiskParam])
async def set_risk(
    project_id: str,
    payload: list[RiskParam],
    ctx: TenantContext = Depends(verify_tenant),
    db: AsyncSession = Depends(get_db),
    _role: None = Depends(require_role("editor")),
) -> list[RiskParam]:
    """upsert 各任務三點估計 (task_risk_parameters)。

    輸入的 criticality_index 被忽略 (該值僅由模擬計算與回寫); 既有列的 criticality_index
    在 upsert 時保留不動。tenant_id 一律取自 ctx。
    """
    await _get_project_or_404(db, project_id, ctx.tenant_id)

    existing = await _load_risk_params(db, project_id)
    by_task = {row.task_id: row for row in existing}

    for rp in payload:
        row = by_task.get(rp.task_id)
        if row is not None:
            row.optimistic_duration = int(rp.optimistic_duration)
            row.most_likely_duration = int(rp.most_likely_duration)
            row.pessimistic_duration = int(rp.pessimistic_duration)
            # 輸入忽略 criticality_index: 既有值保留不動。
        else:
            db.add(
                TaskRiskParameter(
                    project_id=project_id,
                    tenant_id=ctx.tenant_id,
                    task_id=rp.task_id,
                    optimistic_duration=int(rp.optimistic_duration),
                    most_likely_duration=int(rp.most_likely_duration),
                    pessimistic_duration=int(rp.pessimistic_duration),
                    criticality_index=0.0,
                )
            )

    await db.flush()

    # 稽核 (best-effort): 失敗僅記錄, 絕不中斷主要操作。
    try:
        await audit.log_action(
            db,
            ctx,
            "RISK_PARAMS_UPDATE",
            {
                "project_id": project_id,
                "task_ids": [rp.task_id for rp in payload],
            },
        )
    except Exception as exc:  # noqa: BLE001 - 稽核失敗不可中斷主要操作
        logger.warning("audit RISK_PARAMS_UPDATE failed (ignored): %s", exc)

    rows = await _load_risk_params(db, project_id)
    return [_risk_param_to_schema(r) for r in rows]


@router.post("/{project_id}/simulate", response_model=SimulationResult)
async def simulate(
    project_id: str,
    payload: SimulationRequest,
    ctx: TenantContext = Depends(verify_tenant),
    db: AsyncSession = Depends(get_db),
) -> SimulationResult:
    """執行蒙地卡羅排程模擬。

    流程:
      1) 載入任務 + DAG -> TaskDefinition。
      2) 由 task_risk_parameters 組三點估計 (缺者退回基礎工期固定值)。
      3) simulate_schedule(...) 得 SimulationResult (含每任務 criticality)。
      4) 把各任務 criticality_index 回寫 task_risk_parameters。
      5) 若 deadline 有設定且 on_time_probability < 0.70, 觸發風險自動化。
    """
    project = await _get_project_or_404(db, project_id, ctx.tenant_id)

    tasks = await _load_tasks(db, project_id)
    deps = await _load_dependencies(db, project_id)
    definitions = _build_task_definitions(tasks, deps)

    # 三點估計: {task_id: (optimistic, most_likely, pessimistic)}; 缺者由引擎退回基礎工期。
    risk_rows = await _load_risk_params(db, project_id)
    risk: dict[str, tuple[int, int, int]] = {
        r.task_id: (
            int(r.optimistic_duration),
            int(r.most_likely_duration),
            int(r.pessimistic_duration),
        )
        for r in risk_rows
    }

    # CPU 密集的蒙地卡羅模擬以工作執行緒執行, 避免阻塞 async event loop
    # (大量 iterations 仍受 SimulationRequest 上限 10000 保護)。
    result: SimulationResult = await anyio.to_thread.run_sync(
        functools.partial(
            simulate_schedule,
            definitions,
            risk,
            iterations=payload.iterations,
            deadline=payload.deadline,
        )
    )

    # --- 回寫 criticality_index 至 task_risk_parameters ---
    by_task = {r.task_id: r for r in risk_rows}
    for item in result.criticality:
        row = by_task.get(item.task_id)
        if row is not None:
            row.criticality_index = float(item.index)
        else:
            # 該任務無三點估計列 (模擬用了基礎工期); 建立一列以保存 criticality。
            # 缺三點估計時, 以基礎工期填入 a=m=b, 確保資料完整且後續可編輯。
            base = next(
                (d.duration for d in definitions if d.task_id == item.task_id), 0
            )
            db.add(
                TaskRiskParameter(
                    project_id=project_id,
                    tenant_id=ctx.tenant_id,
                    task_id=item.task_id,
                    optimistic_duration=int(base),
                    most_likely_duration=int(base),
                    pessimistic_duration=int(base),
                    criticality_index=float(item.index),
                )
            )

    await db.flush()

    # --- 準時機率偏低 -> 觸發風險自動化 ---
    if (
        result.deadline is not None
        and result.on_time_probability is not None
        and result.on_time_probability < _ONTIME_THRESHOLD
    ):
        try:
            await risk_listener.evaluate_and_dispatch(
                db,
                ctx,
                project.project_id,
                reason="LOW_ONTIME_PROBABILITY",
                detail={
                    "project_id": project.project_id,
                    "deadline": result.deadline,
                    "on_time_probability": result.on_time_probability,
                    "p50": result.p50,
                    "p90": result.p90,
                    "mean": result.mean,
                },
            )
        except Exception as exc:  # noqa: BLE001 - 風險派工失敗不可中斷模擬結果回傳
            logger.warning(
                "simulate: risk dispatch failed (ignored) project=%s: %s",
                project_id,
                exc,
            )

    return result
