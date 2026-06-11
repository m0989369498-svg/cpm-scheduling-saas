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

  2) 通知 outbox (Batch 2 — transactional outbox)
     依 ctx.region 組裝雙語風險預警卡 (notifications.build_risk_card), 並於
     「呼叫端交易內」寫入 erp_integration.notification_outbox PENDING 列
     (與業務資料原子提交; 交易回滾則通知一併回滾, 不會發出幽靈通知)。
     實際投遞由 app.erp.worker.deliver_outbox_once 週期掃描執行
     (per-tenant 憑證優先, 退回全域 settings; 含重試 / DEAD)。

通道解析 (enqueue 時決定 channel):
  * 租戶於 erp_integration.tenant_notification_config 有「啟用中」設定列
    => 依該列已填的憑證欄位各入列一筆 (LINE / DINGTALK / WECOM)。
  * 否則退回全域 settings: region CN -> DINGTALK / WECOM (有設 webhook 才入列);
    其餘 (預設 TW) -> LINE (有設 token 才入列)。
  * 完全無任何通道 => 入列一筆 channel='LOG' (worker 僅記錄日誌即 SUCCESS),
    讓示範 / 無憑證環境的通知管線仍可觀測。

觸發來源 (呼叫端):
  - resources router  : 資源撫平導致工期展延 reason="LEVELING_EXTENSION"
  - analytics router  : 蒙地卡羅模擬準時機率偏低 reason="LOW_ONTIME_PROBABILITY"
  - progress router   : EVM 進度落後且成本超支 reason="SCHEDULE_COST_OVERRUN"

設計重點:
  * 純 async; 不自行 commit (沿用呼叫端 get_db 的交易邊界, 隨端點一起提交)。
  * 通知入列與 ERP 入列彼此獨立: 入列通知失敗不影響事件已成功入列。
  * 回傳 {"event_id": str, "notified": bool} 供端點記錄 / 回應參考
    (notified=True => 至少一筆 outbox 列已入列)。
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.automation import notifications
from app.config import settings
from app.deps import TenantContext
from app.models.orm import NotificationConfig, NotificationOutbox, SyncEvent

logger = logging.getLogger("cpm.core.risk_listener")

# 寫入 sync_event_log 的事件型別 (與 SCHEDULE_PUSH 區隔; worker 以同一佇列處理)
RISK_PROVISION_SYNC_TYPE = "RISK_PROVISION"


async def _resolve_channels(
    db: AsyncSession, tenant_id: str, region: str
) -> list[str]:
    """決定要入列哪些通知通道 (channel 字串清單)。

    優先序:
      1) per-tenant 設定 (tenant_notification_config, 啟用中) 已填憑證的通道。
      2) 全域 settings (region CN -> DINGTALK / WECOM; 其餘 -> LINE)。
    回傳可能為空清單 (呼叫端以 LOG 退路處理)。
    """
    region = (region or "TW").upper()
    channels: list[str] = []

    # 1) per-tenant 通知設定 (erp_integration, 無 RLS — 以 PK 直接查)
    cfg = await db.get(NotificationConfig, tenant_id)
    if cfg is not None and bool(cfg.is_active):
        if (cfg.line_token or "").strip():
            channels.append("LINE")
        if (cfg.dingtalk_webhook or "").strip():
            channels.append("DINGTALK")
        if (cfg.wecom_webhook or "").strip():
            channels.append("WECOM")
        if channels:
            return channels

    # 2) 全域 settings 退路 (區域感知: CN -> 釘釘/企業微信; 其餘 -> LINE)
    if region in ("CN", "CHINA"):
        if (settings.dingtalk_webhook_url or "").strip():
            channels.append("DINGTALK")
        if (getattr(settings, "wecom_webhook_url", "") or "").strip():
            channels.append("WECOM")
    else:
        if (settings.line_channel_access_token or "").strip():
            channels.append("LINE")
    return channels


async def enqueue_notification(
    db: AsyncSession, tenant_id: str, region: str, message: str
) -> int:
    """於呼叫端交易內寫入 notification_outbox PENDING 列 (transactional outbox)。

    完全無任何通道憑證時入列一筆 channel='LOG' (worker 記錄日誌即 SUCCESS),
    確保通知管線在示範 / 無憑證環境仍可觀測。不自行 commit。

    回傳: 入列筆數 (>=1)。
    """
    region = (region or "TW").upper()
    channels = await _resolve_channels(db, tenant_id, region)
    if not channels:
        channels = ["LOG"]

    for channel in channels:
        db.add(
            NotificationOutbox(
                tenant_id=tenant_id,
                region=region,
                channel=channel,
                message=message,
                status="PENDING",
                retry_count=0,
            )
        )
    await db.flush()

    logger.info(
        "notification_outbox enqueued: tenant=%s region=%s channels=%s",
        tenant_id,
        region,
        ",".join(channels),
    )
    return len(channels)


async def evaluate_and_dispatch(
    db: AsyncSession,
    ctx: TenantContext,
    project_id: str,
    *,
    reason: str,
    detail: dict[str, Any] | None = None,
) -> dict:
    """入列 RISK_PROVISION 事件並於同交易入列通知 outbox 列。

    參數:
      db        : 目前請求的 AsyncSession (沿用呼叫端交易, 此處不 commit)。
      ctx       : 租戶情境 (提供 tenant_id 與 region)。
      project_id: 觸發風險的專案代碼。
      reason    : 風險原因代碼 (LEVELING_EXTENSION / LOW_ONTIME_PROBABILITY ...)。
      detail    : 附帶細節 (寫入 payload 並用於組裝通知卡片)。

    回傳:
      {"event_id": <str>, "notified": <bool>}
      notified=True => 至少一筆 notification_outbox 列已入列 (含 LOG 退路)。
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
        # Batch 4 (PERF-3)：直接寫入專案代碼欄位 (除 payload 外)，
        # 供 dashboard / exports 以索引查詢，免掃 payload JSON。
        project_id=project_id,
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

    # --- 2) 通知 outbox (Batch 2: 取代 asyncio.create_task 直接外發) -----------
    # build_risk_card 為純函式 (僅組字串); outbox 列與業務資料同交易原子提交,
    # 實際的外部 I/O (httpx) 移至 worker.deliver_outbox_once (含重試 / DEAD)。
    # notified 語意: 是否已入列 >=1 筆 outbox 列; 回傳鍵維持不變以免破壞呼叫端。
    notified = False
    try:
        region = (ctx.region or "TW").upper()
        card = notifications.build_risk_card(region, reason, detail)
        inserted = await enqueue_notification(db, ctx.tenant_id, region, card)
        notified = inserted >= 1
    except Exception as exc:  # noqa: BLE001 - 通知入列失敗不可影響主流程
        logger.warning(
            "[risk_listener] 通知 outbox 入列失敗 (已忽略) project=%s reason=%s: %s",
            project_id,
            reason,
            exc,
        )

    return {"event_id": event_id, "notified": notified}
