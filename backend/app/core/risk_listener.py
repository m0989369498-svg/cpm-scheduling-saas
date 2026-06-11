"""風險自動化監聽器 (Risk automation listener)。

當 Phase 8 的進階分析偵測到「需要提前準備資源 / 預警」的情況時, 由此模組
統一派工:

  1) 入列 ERP 拋轉事件
     於 erp_integration.sync_event_log 寫入一筆
       sync_type = "RISK_PROVISION"
       status    = "PENDING"
       tenant_id = ctx.tenant_id
       payload   = {"reason", "project_id", "detail"}
     沿用既有 ERP worker 的拋轉佇列 (worker 會跨租戶掃描 PENDING 事件並推送至
     鼎新 / 用友等 ERP, 進行資源提前備料 / 預警)。RISK_PROVISION 與 SCHEDULE_PUSH
     共用同一張表與同一個 worker, 無需新增基礎設施。

  2) Best-effort 區域感知通知
     依 ctx.region 組裝雙語風險預警卡 (notifications.build_risk_card) 並送出
     (CN -> 釘釘 / 選配企業微信; 其餘 -> LINE)。通知失敗一律吞掉, 絕不影響主流程。

觸發來源 (呼叫端):
  - resources router  : 資源撫平導致工期展延 reason="LEVELING_EXTENSION"
  - analytics router  : 蒙地卡羅模擬準時機率偏低 reason="LOW_ONTIME_PROBABILITY"

設計重點:
  * 純 async; 不自行 commit (沿用呼叫端 get_db 的交易邊界, 隨端點一起提交)。
  * 通知與 ERP 入列彼此獨立: 通知失敗不影響事件已成功入列。
  * 回傳 {"event_id": str, "notified": bool} 供端點記錄 / 回應參考。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.automation import notifications
from app.deps import TenantContext
from app.models.orm import SyncEvent

logger = logging.getLogger("cpm.core.risk_listener")

# 寫入 sync_event_log 的事件型別 (與 SCHEDULE_PUSH 區隔; worker 以同一佇列處理)
RISK_PROVISION_SYNC_TYPE = "RISK_PROVISION"


async def _notify_risk_bg(region: str, message: str) -> None:
    """背景風險通知 wrapper (僅吃純字串, 與請求交易完全解耦)。

    FIX-4: 改以 asyncio.create_task 在請求交易之外發送, 避免外部 httpx I/O
    (LINE / 釘釘, 10s timeout) 阻斷並延長呼叫端 DB 交易。此函式不接觸 db /
    session / ctx, 僅持有可序列化的純字串, 故即使請求交易已結束亦安全。
    任何例外一律吞掉並記錄, 絕不外溢。
    """
    try:
        await notifications.notify_risk(region, message)
    except Exception as exc:  # noqa: BLE001 - 背景通知失敗不可外溢
        logger.warning(
            "[risk_listener] 背景風險通知失敗 (已忽略) region=%s: %s",
            region,
            exc,
        )


async def evaluate_and_dispatch(
    db: AsyncSession,
    ctx: TenantContext,
    project_id: str,
    *,
    reason: str,
    detail: dict[str, Any] | None = None,
) -> dict:
    """入列 RISK_PROVISION 事件並 best-effort 發送雙語風險預警通知。

    參數:
      db        : 目前請求的 AsyncSession (沿用呼叫端交易, 此處不 commit)。
      ctx       : 租戶情境 (提供 tenant_id 與 region)。
      project_id: 觸發風險的專案代碼。
      reason    : 風險原因代碼 (LEVELING_EXTENSION / LOW_ONTIME_PROBABILITY ...)。
      detail    : 附帶細節 (寫入 payload 並用於組裝通知卡片)。

    回傳:
      {"event_id": <str>, "notified": <bool>}
    """
    detail = detail or {}

    # --- 1) 入列 ERP 拋轉事件 (RISK_PROVISION) --------------------------------
    payload = {
        "reason": reason,
        "project_id": project_id,
        "detail": detail,
    }
    event = SyncEvent(
        tenant_id=ctx.tenant_id,
        mapping_id=None,  # 風險預警事件無對應特定任務 mapping
        sync_type=RISK_PROVISION_SYNC_TYPE,
        payload=payload,
        status="PENDING",
        retry_count=0,
    )
    db.add(event)
    # flush 以取得 event_id (uuid); 不 commit, 交由呼叫端端點的交易一併提交。
    await db.flush()
    event_id = str(event.event_id)

    logger.info(
        "RISK_PROVISION enqueued: tenant=%s project=%s reason=%s event_id=%s",
        ctx.tenant_id,
        project_id,
        reason,
        event_id,
    )

    # --- 2) Best-effort 區域感知通知 (背景排程, 不阻斷請求交易) -----------------
    # build_risk_card 為純函式 (僅組字串), 於交易內同步組好卡片; 真正的外部 I/O
    # (httpx) 改以 asyncio.create_task 丟到背景, wrapper 僅持有純字串。
    # notified 語意改為「是否成功排程」(scheduled): 排程成功即 True; 回傳鍵維持不變
    # 以免破壞呼叫端 / 回應 schema。
    notified = False
    try:
        region = (ctx.region or "TW").upper()
        card = notifications.build_risk_card(region, reason, detail)
        asyncio.create_task(_notify_risk_bg(region, card))
        notified = True
    except Exception as exc:  # noqa: BLE001 - 排程/組卡片失敗不可影響主流程
        logger.warning(
            "[risk_listener] 風險通知排程失敗 (已忽略) project=%s reason=%s: %s",
            project_id,
            reason,
            exc,
        )

    return {"event_id": event_id, "notified": notified}
