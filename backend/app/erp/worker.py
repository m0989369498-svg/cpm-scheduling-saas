"""ERP 同步 + 通知投遞 背景 Worker (standalone entrypoint).

執行方式::

    python -m app.erp.worker

職責：
  1) ERP 拋轉 (``scan_once``)：
    以 APScheduler (AsyncIOScheduler) 每 ``settings.erp_scan_interval_seconds`` 秒掃描一次
    ``erp_integration.sync_event_log`` 中 status='PENDING' 且 retry_count < ERP_MAX_RETRIES
    的事件，逐筆：
        1. 依 tenant_id 查 erp_integration.tenant_erp_config 取得 erp_type / api_endpoint
        2. (若 mapping_id 有效) 查 task_mapping 補上 erp_wbs_code / schedule_task_id
        3. 由 get_adapter() 取得對應 Adapter -> translate() -> push()
        4. 成功     => status='SUCCESS'
           失敗     => retry_count += 1、寫入 last_error；
                       若 retry_count >= ERP_MAX_RETRIES => status='DEAD'，否則維持 'PENDING'
  2) 通知投遞 (``deliver_outbox_once``, Batch 2)：
    同間隔掃描 ``erp_integration.notification_outbox`` PENDING 列，逐筆解析憑證
    (per-tenant tenant_notification_config 優先，退回全域 settings) 後經
    notifications.notify_line / notify_dingtalk / notify_wecom 投遞；
    channel='LOG' 僅記錄日誌即 SUCCESS。失敗 => retry_count+1 + last_error，
    達上限 => DEAD。

關鍵設計：
    - Worker 使用「自己的」async engine / AsyncSession (sqlite dev 模式下與
      app.database 相同地以 schema_translate_map 將 erp_integration 映射為 None)。
    - Worker 「不」設定 RLS 的 app.current_tenant；因為 erp_integration schema 未啟用 RLS，
      跨租戶 worker 可直接掃描，並由程式碼自行以 tenant_id 過濾 (此處事件本身已帶 tenant_id)。
    - ``scan_once()`` / ``deliver_outbox_once()`` 為冪等的單次掃描，供測試 / 手動執行；
      ``main()`` 啟動排程器常駐執行。
    - 心跳 (Batch 2 / CHANGE-6b)：每個 tick touch ``/tmp/worker_heartbeat``，
      供 docker-compose healthcheck 以 mtime 年齡判斷 worker 存活。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from pathlib import Path
from typing import Any, Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.automation import notifications
from app.config import is_sqlite, settings
from app.erp.acl import CanonicalSyncItem, ErpPushError
from app.erp.adapters import get_adapter
from app.models.orm import NotificationConfig, NotificationOutbox

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("app.erp.worker")

# 每次 tick 處理的批次上限，避免單次掃描占用過久
BATCH_LIMIT = 100
# 通知 outbox 每次 tick 投遞的批次上限
OUTBOX_BATCH_LIMIT = 100

# 心跳檔 (CHANGE-6b)：每個 tick touch 一次，compose healthcheck 以 mtime 判斷存活。
HEARTBEAT_FILE = Path(os.environ.get("WORKER_HEARTBEAT_FILE", "/tmp/worker_heartbeat"))


def _touch_heartbeat() -> None:
    """touch 心跳檔 (更新 mtime)。失敗絕不影響主流程 (如 Windows dev 無 /tmp)。"""
    try:
        HEARTBEAT_FILE.touch(exist_ok=True)
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# 獨立的引擎 / Session 工廠 (worker 專用，不與 API 進程共用連線池)
#   sqlite (dev / 測試) 分支與 app.database._build_engine 對齊：
#   schema_translate_map 將 erp_integration 映射為 None，使 ORM 查詢
#   (notification_outbox 等) 在單一 sqlite 檔上亦可運作。
# --------------------------------------------------------------------------- #
def _build_worker_engine():
    if is_sqlite():
        return create_async_engine(
            settings.database_url,
            future=True,
            connect_args={"check_same_thread": False},
            execution_options={"schema_translate_map": {"erp_integration": None}},
        )
    return create_async_engine(settings.database_url, pool_pre_ping=True, future=True)


_engine = _build_worker_engine()
WorkerSessionLocal = async_sessionmaker(
    bind=_engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


def _to_jsonable(value: Any) -> Any:
    """把 DB 取出的 payload 欄位 (可能是 dict / JSON 字串) 正規化成 dict。"""
    if value is None:
        return {}
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return {"raw": value}
    return {"raw": str(value)}


async def _fetch_pending_events(session: AsyncSession) -> list[dict[str, Any]]:
    """撈出待處理事件 (PENDING 且尚可重試)。"""
    rows = (
        await session.execute(
            text(
                """
                SELECT event_id, tenant_id, mapping_id, sync_type, payload,
                       status, retry_count, last_error
                FROM erp_integration.sync_event_log
                WHERE status = 'PENDING'
                  AND retry_count < :max_retries
                ORDER BY created_at ASC
                LIMIT :limit
                """
            ),
            {"max_retries": settings.erp_max_retries, "limit": BATCH_LIMIT},
        )
    ).mappings().all()
    return [dict(r) for r in rows]


async def _load_tenant_erp_config(
    session: AsyncSession, tenant_id: str
) -> Optional[dict[str, Any]]:
    """取得租戶的 ERP 設定 (erp_type / api_endpoint / is_active)。"""
    row = (
        await session.execute(
            text(
                """
                SELECT tenant_id, erp_type, api_endpoint, is_active
                FROM erp_integration.tenant_erp_config
                WHERE tenant_id = :tenant_id
                """
            ),
            {"tenant_id": tenant_id},
        )
    ).mappings().first()
    return dict(row) if row else None


async def _load_mapping(
    session: AsyncSession, mapping_id: Optional[int]
) -> Optional[dict[str, Any]]:
    """依 mapping_id 取得 task_mapping (schedule_task_id / erp_wbs_code)。"""
    if mapping_id is None:
        return None
    row = (
        await session.execute(
            text(
                """
                SELECT mapping_id, tenant_id, schedule_task_id, erp_wbs_code
                FROM erp_integration.task_mapping
                WHERE mapping_id = :mapping_id
                """
            ),
            {"mapping_id": mapping_id},
        )
    ).mappings().first()
    return dict(row) if row else None


def _build_canonical(
    event: dict[str, Any],
    mapping: Optional[dict[str, Any]],
) -> CanonicalSyncItem:
    """以事件 payload + mapping 組出內部正規模型 (canonical)。

    payload 來源為 router 在拋轉時寫入的 CPM 結果快照 (task_id / duration / status /
    es / ef / float_time / is_critical 等)。mapping 提供 ERP 端的 wbs_code。
    """
    payload = _to_jsonable(event.get("payload"))
    if not isinstance(payload, dict):
        payload = {}

    # task_id：優先用 payload，其次用 mapping 的 schedule_task_id
    task_id = (
        payload.get("task_id")
        or (mapping.get("schedule_task_id") if mapping else None)
        or ""
    )
    # wbs_code：優先 mapping 的 erp_wbs_code，其次 payload，最後退回 task_id
    wbs_code = (
        (mapping.get("erp_wbs_code") if mapping else None)
        or payload.get("erp_wbs_code")
        or payload.get("wbs_code")
        or task_id
    )

    return CanonicalSyncItem(
        tenant_id=event["tenant_id"],
        task_id=str(task_id),
        wbs_code=str(wbs_code),
        task_name=str(payload.get("task_name", "")),
        duration=int(payload.get("duration", 0) or 0),
        status=str(payload.get("status", "PENDING")),
        es=int(payload.get("es", 0) or 0),
        ef=int(payload.get("ef", 0) or 0),
        is_critical=bool(payload.get("is_critical", False)),
        float_time=int(payload.get("float_time", payload.get("float", 0)) or 0),
        start_date=payload.get("start_date"),
        finish_date=payload.get("finish_date"),
        extra={"sync_type": event.get("sync_type")},
    )


async def _mark_success(session: AsyncSession, event_id: Any, response: dict[str, Any]) -> None:
    await session.execute(
        text(
            """
            UPDATE erp_integration.sync_event_log
            SET status = 'SUCCESS',
                last_error = NULL,
                updated_at = now()
            WHERE event_id = :event_id
            """
        ),
        {"event_id": event_id},
    )


async def _mark_failure(
    session: AsyncSession,
    event_id: Any,
    current_retry: int,
    error: str,
) -> str:
    """失敗處理：retry_count + 1；若達上限則標記 DEAD，否則維持 PENDING。"""
    new_retry = (current_retry or 0) + 1
    new_status = "DEAD" if new_retry >= settings.erp_max_retries else "PENDING"
    await session.execute(
        text(
            """
            UPDATE erp_integration.sync_event_log
            SET status = :status,
                retry_count = :retry_count,
                last_error = :last_error,
                updated_at = now()
            WHERE event_id = :event_id
            """
        ),
        {
            "status": new_status,
            "retry_count": new_retry,
            "last_error": (error or "")[:2000],  # 避免過長 last_error
            "event_id": event_id,
        },
    )
    return new_status


async def _process_event(session: AsyncSession, event: dict[str, Any]) -> str:
    """處理單一事件，回傳結果狀態字串 ('SUCCESS' / 'PENDING' / 'DEAD')。

    每筆事件以獨立 try/except 包裹，確保單筆失敗不影響整批掃描。
    """
    event_id = event["event_id"]
    tenant_id = event["tenant_id"]
    try:
        config = await _load_tenant_erp_config(session, tenant_id)
        if config is None:
            msg = f"找不到租戶 {tenant_id} 的 ERP 設定 (tenant_erp_config)"
            logger.warning("[event=%s] %s", event_id, msg)
            return await _mark_failure(session, event_id, event.get("retry_count", 0), msg)

        if not config.get("is_active", True):
            msg = f"租戶 {tenant_id} 的 ERP 設定為停用 (is_active=false)"
            logger.warning("[event=%s] %s", event_id, msg)
            return await _mark_failure(session, event_id, event.get("retry_count", 0), msg)

        mapping = await _load_mapping(session, event.get("mapping_id"))
        canonical = _build_canonical(event, mapping)

        adapter = get_adapter(config.get("erp_type"), config.get("api_endpoint"))
        logger.info(
            "[event=%s] 拋轉 tenant=%s erp=%s task=%s wbs=%s",
            event_id,
            tenant_id,
            adapter.erp_type,
            canonical.task_id,
            canonical.wbs_code,
        )

        result = await adapter.translate_and_push(canonical)

        # push() 成功才會回傳 (失敗時改為拋出 ErpPushError，於下方攔截)；
        # 仍保留 result.ok 判斷以相容模擬模式與任何回傳 ok=False 的實作。
        if result.ok:
            await _mark_success(session, event_id, result.response)
            logger.info(
                "[event=%s] 拋轉成功 (%s) erp_ref=%s",
                event_id,
                result.message,
                result.erp_ref,
            )
            return "SUCCESS"

        status = await _mark_failure(
            session, event_id, event.get("retry_count", 0), result.message
        )
        logger.warning(
            "[event=%s] 拋轉失敗 -> %s (%s)", event_id, status, result.message
        )
        return status
    except ErpPushError as exc:
        # 推送失敗 (transport 例外或非 2xx)：記錄 last_error、retry_count + 1，
        # 達 ERP_MAX_RETRIES 則翻為 DEAD。錯誤訊息直接取自例外，便於診斷。
        status = await _mark_failure(
            session, event_id, event.get("retry_count", 0), str(exc)
        )
        logger.warning("[event=%s] 拋轉失敗 -> %s (%s)", event_id, status, exc)
        return status
    except Exception as exc:  # noqa: BLE001 - 單筆事件不可拖垮整批
        logger.exception("[event=%s] 處理時發生未預期例外", event_id)
        return await _mark_failure(
            session, event_id, event.get("retry_count", 0), f"unexpected: {exc}"
        )


async def scan_once() -> dict[str, int]:
    """執行單次掃描 (冪等)，回傳統計結果。

    供測試與手動執行使用。每筆事件處理後即時 commit，避免一筆例外導致整批回滾，
    同時讓重試計數確實落地。
    """
    _touch_heartbeat()  # CHANGE-6b：每個 tick 更新心跳檔 (healthcheck 依 mtime 判斷)
    stats = {"processed": 0, "success": 0, "pending": 0, "dead": 0}
    async with WorkerSessionLocal() as session:
        events = await _fetch_pending_events(session)
        if not events:
            logger.debug("本次掃描無待處理事件")
            return stats

        logger.info("本次掃描取得 %d 筆待處理事件", len(events))
        for event in events:
            result_status = await _process_event(session, event)
            await session.commit()  # 逐筆落地，確保狀態 / 重試計數不因後續例外丟失
            stats["processed"] += 1
            if result_status == "SUCCESS":
                stats["success"] += 1
            elif result_status == "DEAD":
                stats["dead"] += 1
            else:
                stats["pending"] += 1

    logger.info(
        "掃描完成：processed=%d success=%d pending=%d dead=%d",
        stats["processed"],
        stats["success"],
        stats["pending"],
        stats["dead"],
    )
    return stats


# --------------------------------------------------------------------------- #
# 通知 outbox 投遞 (Batch 2 — CHANGE-3c)
# --------------------------------------------------------------------------- #
def _outbox_mark_success(row: NotificationOutbox) -> None:
    """投遞成功：SUCCESS、清空 last_error、更新 updated_at。"""
    row.status = "SUCCESS"
    row.last_error = None
    row.updated_at = func.now()


def _outbox_mark_failure(row: NotificationOutbox, error: str) -> str:
    """投遞失敗：retry_count + 1；達上限 => DEAD，否則維持 PENDING。"""
    new_retry = (row.retry_count or 0) + 1
    row.retry_count = new_retry
    row.last_error = (error or "")[:2000]  # 避免過長 last_error
    row.status = "DEAD" if new_retry >= settings.erp_max_retries else "PENDING"
    row.updated_at = func.now()
    return row.status


async def _deliver_outbox_row(session: AsyncSession, row: NotificationOutbox) -> str:
    """投遞單筆 outbox 列，回傳結果狀態字串 ('SUCCESS' / 'PENDING' / 'DEAD')。

    憑證解析：per-tenant tenant_notification_config (啟用中) 優先，
    退回全域 settings。每筆以獨立 try/except 包裹，單筆失敗不影響整批。
    """
    channel = (row.channel or "").upper()
    try:
        # LOG 通道：無任何憑證時的可觀測退路 —— 僅記錄日誌即成功。
        if channel == "LOG":
            logger.info(
                "[outbox=%s] LOG 通道 (無外部投遞) tenant=%s region=%s: %s",
                row.id,
                row.tenant_id,
                row.region,
                row.message,
            )
            _outbox_mark_success(row)
            return "SUCCESS"

        # per-tenant 通知設定 (停用視同不存在 => 退回全域 settings)
        cfg = await session.get(NotificationConfig, row.tenant_id)
        if cfg is not None and not bool(cfg.is_active):
            cfg = None

        if channel == "LINE":
            token = ((cfg.line_token if cfg else "") or "").strip() or (
                settings.line_channel_access_token or ""
            ).strip()
            target = ((cfg.line_target_id if cfg else "") or "").strip() or None
            result = await notifications.notify_line(
                row.message, token=token, target_id=target
            )
        elif channel == "DINGTALK":
            webhook = ((cfg.dingtalk_webhook if cfg else "") or "").strip() or (
                settings.dingtalk_webhook_url or ""
            ).strip()
            result = await notifications.notify_dingtalk(row.message, webhook=webhook)
        elif channel == "WECOM":
            webhook = ((cfg.wecom_webhook if cfg else "") or "").strip() or (
                getattr(settings, "wecom_webhook_url", "") or ""
            ).strip()
            result = await notifications.notify_wecom(row.message, webhook=webhook)
        else:
            status = _outbox_mark_failure(
                row, f"未知通知通道 (unknown channel): {row.channel}"
            )
            logger.warning("[outbox=%s] 未知通道 %s -> %s", row.id, row.channel, status)
            return status

        if result.get("sent"):
            _outbox_mark_success(row)
            logger.info("[outbox=%s] 投遞成功 channel=%s", row.id, channel)
            return "SUCCESS"

        # 未送出：可能是缺憑證 (skipped) 或對端回錯 (status_code / errcode / error)
        if result.get("skipped"):
            error = "缺少憑證 (no credentials configured)"
        else:
            error = str(
                result.get("error")
                or f"status_code={result.get('status_code')} errcode={result.get('errcode')}"
            )
        status = _outbox_mark_failure(row, error)
        logger.warning(
            "[outbox=%s] 投遞失敗 channel=%s -> %s (%s)", row.id, channel, status, error
        )
        return status
    except Exception as exc:  # noqa: BLE001 - 單筆通知不可拖垮整批
        logger.exception("[outbox=%s] 投遞時發生未預期例外", row.id)
        return _outbox_mark_failure(row, f"unexpected: {exc}")


async def deliver_outbox_once() -> dict[str, int]:
    """執行單次通知 outbox 投遞 (冪等)，回傳統計結果。

    供測試與手動執行使用。掃描 notification_outbox 中 status='PENDING' 且
    retry_count < ERP_MAX_RETRIES 的列 (LIMIT 批次)，逐筆投遞並即時 commit
    (與 scan_once 相同：避免一筆例外導致整批回滾，重試計數確實落地)。
    """
    _touch_heartbeat()  # CHANGE-6b：每個 tick 更新心跳檔
    stats = {"processed": 0, "success": 0, "pending": 0, "dead": 0}
    async with WorkerSessionLocal() as session:
        rows = (
            (
                await session.execute(
                    select(NotificationOutbox)
                    .where(
                        NotificationOutbox.status == "PENDING",
                        NotificationOutbox.retry_count < settings.erp_max_retries,
                    )
                    .order_by(
                        NotificationOutbox.created_at.asc(),
                        NotificationOutbox.id.asc(),
                    )
                    .limit(OUTBOX_BATCH_LIMIT)
                )
            )
            .scalars()
            .all()
        )
        if not rows:
            logger.debug("本次 outbox 掃描無待投遞通知")
            return stats

        logger.info("本次 outbox 掃描取得 %d 筆待投遞通知", len(rows))
        for row in rows:
            result_status = await _deliver_outbox_row(session, row)
            await session.commit()  # 逐筆落地，確保狀態 / 重試計數不因後續例外丟失
            stats["processed"] += 1
            if result_status == "SUCCESS":
                stats["success"] += 1
            elif result_status == "DEAD":
                stats["dead"] += 1
            else:
                stats["pending"] += 1

    logger.info(
        "outbox 投遞完成：processed=%d success=%d pending=%d dead=%d",
        stats["processed"],
        stats["success"],
        stats["pending"],
        stats["dead"],
    )
    return stats


async def main() -> None:
    """啟動 APScheduler 排程器並常駐執行。

    以 ``settings.erp_scan_interval_seconds`` 為間隔週期性呼叫 ``scan_once``
    (ERP 拋轉) 與 ``deliver_outbox_once`` (通知投遞)。支援 SIGINT / SIGTERM
    優雅關閉。
    """
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        scan_once,
        trigger="interval",
        seconds=settings.erp_scan_interval_seconds,
        id="erp_sync_scan",
        max_instances=1,          # 避免上一輪未結束又啟動下一輪
        coalesce=True,            # 落後的觸發合併為一次
        next_run_time=None,       # 等待第一個間隔後再執行 (下方手動先跑一次)
    )
    # Batch 2：通知 outbox 投遞 job (同間隔；與 ERP 拋轉彼此獨立)
    scheduler.add_job(
        deliver_outbox_once,
        trigger="interval",
        seconds=settings.erp_scan_interval_seconds,
        id="notification_outbox_deliver",
        max_instances=1,
        coalesce=True,
        next_run_time=None,
    )

    logger.info(
        "ERP 同步 / 通知投遞 Worker 啟動，掃描間隔=%ds，最大重試=%d",
        settings.erp_scan_interval_seconds,
        settings.erp_max_retries,
    )

    scheduler.start()

    # 啟動後先立即各跑一次，縮短首批事件 / 通知的延遲
    try:
        await scan_once()
    except Exception:  # noqa: BLE001
        logger.exception("啟動時首次掃描失敗 (將由排程繼續重試)")
    try:
        await deliver_outbox_once()
    except Exception:  # noqa: BLE001
        logger.exception("啟動時首次 outbox 投遞失敗 (將由排程繼續重試)")

    # 以 Event 等待關閉訊號，保持常駐
    stop_event = asyncio.Event()

    def _request_stop(*_args: Any) -> None:
        logger.info("收到關閉訊號，準備優雅關閉…")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig_name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, _request_stop)
        except NotImplementedError:
            # Windows 不支援 add_signal_handler，退回 signal.signal
            try:
                signal.signal(sig, _request_stop)
            except (ValueError, OSError):
                pass

    try:
        await stop_event.wait()
    finally:
        scheduler.shutdown(wait=False)
        await _engine.dispose()
        logger.info("ERP 同步 Worker 已關閉")


if __name__ == "__main__":
    asyncio.run(main())
