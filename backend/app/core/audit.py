"""稽核日誌 (Audit log)。

於敏感的管理操作 (使用者建立 / 更新 / 刪除等) 留下不可否認的軌跡:
  public.audit_log(tenant_id, actor, action, detail, created_at)

設計重點:
  * 純 async; 「不」自行 commit —— 沿用呼叫端 (get_db) 的交易邊界, 與被稽核的
    寫入操作同一筆交易一起提交 (操作 rollback 時稽核列亦一併 rollback, 保持一致)。
  * tenant_id 一律取自 ctx (絕不信任輸入); actor 取自 ctx.sub (登入主體 username),
    header / dev 模式下可能為空字串。
  * audit_log 受 RLS 保護 (enable + force + tenant policy), 故必須在已設定
    app.current_tenant GUC 的 session 內寫入 —— get_db 已於交易開頭設定, 因此
    傳入的 db 即符合條件。
  * detail 為可攜 JSON (PostgreSQL -> JSONB、sqlite -> JSON); None 時存空 dict。

公開 API:
  log_action(db, ctx, action, detail=None) -> AuditLog
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import TenantContext
from app.models.orm import AuditLog

logger = logging.getLogger("cpm.core.audit")


async def log_action(
    db: AsyncSession,
    ctx: TenantContext,
    action: str,
    detail: dict[str, Any] | None = None,
) -> AuditLog:
    """寫入一筆稽核日誌 (不 commit, 隨呼叫端交易一併提交)。

    參數:
      db     : 目前請求的 AsyncSession (已設定租戶 GUC; 沿用其交易)。
      ctx    : 租戶情境 (提供 tenant_id 與 actor=sub)。
      action : 動作代碼 (例: "USER_CREATE" / "USER_UPDATE" / "USER_DELETE")。
      detail : 附帶細節 (寫入 detail JSON 欄位); None 時存空 dict。

    回傳:
      已加入 session 並 flush 的 AuditLog 實例 (id 已產生)。
    """
    actor = (getattr(ctx, "sub", None) or "") or None
    entry = AuditLog(
        tenant_id=ctx.tenant_id,
        actor=actor,
        action=action,
        detail=detail or {},
    )
    db.add(entry)
    # flush 以取得自增 id 並讓 RLS 政策即時驗證; 不 commit, 交由呼叫端端點交易提交。
    await db.flush()

    logger.info(
        "audit: tenant=%s actor=%s action=%s id=%s",
        ctx.tenant_id,
        actor,
        action,
        entry.id,
    )
    return entry
