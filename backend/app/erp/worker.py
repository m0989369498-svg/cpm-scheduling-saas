"""ERP 同步背景 Worker (standalone entrypoint).

執行方式::

    python -m app.erp.worker

職責：
    以 APScheduler (AsyncIOScheduler) 每 ``settings.erp_scan_interval_seconds`` 秒掃描一次
    ``erp_integration.sync_event_log`` 中 status='PENDING' 且 retry_count < ERP_MAX_RETRIES
    的事件，逐筆：
        1. 依 tenant_id 查 erp_integration.tenant_erp_config 取得 erp_type / api_endpoint
        2. (若 mapping_id 有效) 查 task_mapping 補上 erp_wbs_code / schedule_task_id
        3. 由 get_adapter() 取得對應 Adapter -> translate() -> push()
        4. 成功     => status='SUCCESS'
           失敗     => retry_count += 1、寫入 last_error；
                       若 retry_count >= ERP_MAX_RETRIES => status='DEAD'，否則維持 'PENDING'

關鍵設計：
    - Worker 使用「自己的」async engine / AsyncSession。
    - Worker 「不」設定 RLS 的 app.current_tenant；因為 erp_integration schema 未啟用 RLS，
      跨租戶 worker 可直接掃描，並由程式碼自行以 tenant_id 過濾 (此處事件本身已帶 tenant_id)。
    - ``scan_once()`` 為冪等的單次掃描，供測試 / 手動執行；``main()`` 啟動排程器常駐執行。
"""

from __future__ import annotations

import asyncio
import json
import logging
import signal
from typing import Any, Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.erp.acl import CanonicalSyncItem, ErpPushError
from app.erp.adapters import get_adapter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("app.erp.worker")

# 每次 tick 處理的批次上限，避免單次掃描占用過久
BATCH_LIMIT = 100


# --------------------------------------------------------------------------- #
# 獨立的引擎 / Session 工廠 (worker 專用，不與 API 進程共用連線池)
# --------------------------------------------------------------------------- #
_engine = create_async_engine(settings.database_url, pool_pre_ping=True, future=True)
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


async def main() -> None:
    """啟動 APScheduler 排程器並常駐執行。

    以 ``settings.erp_scan_interval_seconds`` 為間隔週期性呼叫 ``scan_once``。
    支援 SIGINT / SIGTERM 優雅關閉。
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

    logger.info(
        "ERP 同步 Worker 啟動，掃描間隔=%ds，最大重試=%d",
        settings.erp_scan_interval_seconds,
        settings.erp_max_retries,
    )

    scheduler.start()

    # 啟動後先立即跑一次，縮短首批事件的延遲
    try:
        await scan_once()
    except Exception:  # noqa: BLE001
        logger.exception("啟動時首次掃描失敗 (將由排程繼續重試)")

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
