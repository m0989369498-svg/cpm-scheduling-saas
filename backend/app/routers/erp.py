"""ERP 拋轉路由（ERP sync router）。

將專案任務拋轉至外部 ERP（SAP / 鼎新 / 用友）。本端點僅負責入列：
為每個任務在 erp_integration.sync_event_log 寫入一筆 PENDING 事件，
真正的翻譯與推送由背景 worker（app.erp.worker）非同步處理。

注意：erp_integration.* schema 無 RLS（service-managed），因此此處所有查詢
都「明確以 tenant_id 過濾」，避免跨租戶資料外洩。
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from fastapi import APIRouter, Depends, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.deps import verify_tenant, get_db, TenantContext, require_role
from app.models.orm import Task, TaskMapping, SyncEvent
from app.schemas.schedule import ErpSyncRequest
from app.routers.projects import _get_project_or_404, _load_tasks

# Batch 3 (FEAT-2)：真實日期 / 工作曆 —— workcal 與 ProjectHoliday 由同批次的
# schema 工作項提供；以防呆方式匯入，於尚未落地的中間狀態維持既有行為
# (payload 之 start_date / finish_date 保持 None)，落地後即自動啟用。
try:  # pragma: no cover - 純粹的可用性偵測
    from app.core.workcal import offset_to_date as _offset_to_date
except ImportError:  # workcal 尚未落地
    _offset_to_date = None
try:  # pragma: no cover
    from app.models.orm import ProjectHoliday as _ProjectHoliday
except ImportError:  # project_holidays 尚未落地
    _ProjectHoliday = None

logger = logging.getLogger("cpm.routers.erp")

router = APIRouter(prefix="/projects", tags=["erp"])


async def _get_or_create_mapping(
    db: AsyncSession,
    tenant_id: str,
    schedule_task_id: str,
) -> TaskMapping:
    """取得任務對應；若不存在則即時建立（WBS code 預設沿用 task_id）。

    對應表為 service-managed（無 RLS），因此明確以 tenant_id 過濾。
    """
    result = await db.execute(
        select(TaskMapping).where(
            TaskMapping.tenant_id == tenant_id,
            TaskMapping.schedule_task_id == schedule_task_id,
        )
    )
    mapping = result.scalar_one_or_none()
    if mapping is not None:
        return mapping

    mapping = TaskMapping(
        tenant_id=tenant_id,
        schedule_task_id=schedule_task_id,
        erp_wbs_code=schedule_task_id,  # 預設 WBS = 排程任務代碼，後續可由 ERP config 調整
    )
    db.add(mapping)
    await db.flush()  # 取得 mapping_id
    return mapping


async def _build_offset_to_iso(
    db: AsyncSession, project
) -> Callable[[int], str] | None:
    """當專案已設定開工日期時，回傳「天數偏移 -> ISO 日期字串」函式；否則 None。

    Batch 3 (FEAT-2)：以 workcal.offset_to_date 把 CPM 的 es/ef (工作天偏移)
    換算成真實日曆日期 (跳過非工作日 work_days 與 project_holidays)。
    防呆：start_date / work_days 欄位或 workcal 模組尚未落地時回 None，
    payload 維持既有形狀 (日期為 None)。
    """
    start_date = getattr(project, "start_date", None)
    if start_date is None or _offset_to_date is None:
        return None

    work_days = (getattr(project, "work_days", None) or "1111110")
    holidays: set = set()
    if _ProjectHoliday is not None:
        rows = await db.execute(
            select(_ProjectHoliday.holiday_date).where(
                _ProjectHoliday.project_id == project.project_id
            )
        )
        holidays = {d for d in rows.scalars().all() if d is not None}

    def to_iso(offset: int) -> str:
        return _offset_to_date(
            start_date, max(int(offset or 0), 0), work_days, holidays
        ).isoformat()

    return to_iso


@router.post(
    "/{project_id}/erp/sync",
    status_code=status.HTTP_202_ACCEPTED,
)
async def sync_erp(
    project_id: str,
    payload: ErpSyncRequest,
    ctx: TenantContext = Depends(verify_tenant),
    db: AsyncSession = Depends(get_db),
    _role: None = Depends(require_role("editor")),
) -> dict:
    """拋轉專案至 ERP：為每個任務寫入一筆 PENDING 同步事件。

    回傳 {enqueued: N, event_ids: [...]}，由背景 worker 後續處理。
    若任務尚無 ERP 對應，會即時建立 task_mapping。
    """
    project = await _get_project_or_404(db, project_id, ctx.tenant_id)
    tasks = await _load_tasks(db, project_id)

    # Batch 3 (FEAT-2)：專案有開工日期時，把 es/ef 換算成真實 ISO 日期帶入 payload。
    offset_to_iso = await _build_offset_to_iso(db, project)

    event_ids: list[str] = []
    for t in tasks:
        mapping = await _get_or_create_mapping(db, ctx.tenant_id, t.task_id)

        # 標準化 payload（canonical 模型快照），交由 worker 翻譯成各 ERP 格式
        sync_payload = {
            "project_id": project.project_id,
            "tenant_id": ctx.tenant_id,
            "region": project.region,
            "task_id": t.task_id,
            "task_name": t.task_name or "",
            "wbs_code": mapping.erp_wbs_code,
            "duration": t.duration or 0,
            "status": t.status or "PENDING",
            "es": t.es or 0,
            "ef": t.ef or 0,
            "float_time": t.float_time or 0,
            "is_critical": bool(t.is_critical),
        }
        if offset_to_iso is not None:
            es = int(t.es or 0)
            ef = int(t.ef or 0)
            # 計畫開工 = 第 es 個工作天；計畫完工 = 第 ef-1 個工作天 (最後一個
            # 施作日)。零工期 (里程碑) 任務以開工日為完工日，避免負偏移。
            sync_payload["start_date"] = offset_to_iso(es)
            sync_payload["finish_date"] = offset_to_iso(max(ef - 1, es))

        event = SyncEvent(
            tenant_id=ctx.tenant_id,
            mapping_id=mapping.mapping_id,
            # Batch 4 (PERF-3)：直接寫入專案代碼欄位 (除 payload 外)，
            # 供 dashboard / exports 以索引查詢，免掃 payload JSON。
            project_id=project.project_id,
            sync_type=payload.sync_type or "SCHEDULE_PUSH",
            payload=sync_payload,
            status="PENDING",
            retry_count=0,
        )
        db.add(event)
        await db.flush()  # 取得 event_id（uuid）
        event_ids.append(str(event.event_id))

    logger.info(
        "ERP sync enqueued: tenant=%s project=%s type=%s count=%d",
        ctx.tenant_id,
        project_id,
        payload.sync_type,
        len(event_ids),
    )

    # 稽核 (best-effort): 失敗僅記錄, 絕不中斷主要操作。
    try:
        await audit.log_action(
            db,
            ctx,
            "ERP_SYNC_ENQUEUE",
            {
                "project_id": project_id,
                "sync_type": payload.sync_type or "SCHEDULE_PUSH",
                "enqueued": len(event_ids),
            },
        )
    except Exception as exc:  # noqa: BLE001 - 稽核失敗不可中斷主要操作
        logger.warning("audit ERP_SYNC_ENQUEUE failed (ignored): %s", exc)

    return {"enqueued": len(event_ids), "event_ids": event_ids}
