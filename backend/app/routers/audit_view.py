"""稽核日誌查詢路由 (Audit view router) —— admin-only 唯讀查詢。

掛載: prefix="/audit", 由 main.py 以 settings.api_v1_prefix 為前綴 include。

端點:
  GET /audit   列出本租戶最近的稽核日誌 (admin-only)。
    查詢參數:
      limit  : 每頁筆數 (預設 50, 最大 200)。
      offset : 略過筆數 (分頁)。
      action : 選填, 動作代碼「完全比對」過濾 (例: "LOGIN_SUCCESS")。
    排序: created_at DESC (同時間以 id DESC 決勝, 輸出穩定)。
    回傳: [{id, actor, action, detail, created_at}]。

設計重點:
  * 以 Depends(require_role("admin")) 守門 —— 非 admin 一律 403。
  * 租戶隔離: PostgreSQL 由 RLS 強制 (Depends(get_db) 已於交易開頭設定
    app.current_tenant GUC); sqlite (dev, 無 RLS) 額外以
    ``AuditLog.tenant_id == ctx.tenant_id`` 在查詢層過濾, 兩種後端行為一致
    (於 PostgreSQL 為冗餘但無害)。
  * 唯讀端點: 不寫入、無副作用。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import TenantContext, get_db, require_role, verify_tenant
from app.models.orm import AuditLog

router = APIRouter(prefix="/audit", tags=["audit"])


@router.get("")
async def list_audit_log(
    limit: int = Query(default=50, ge=1, le=200, description="每頁筆數 (<=200)"),
    offset: int = Query(default=0, ge=0, description="略過筆數 (分頁)"),
    action: str | None = Query(
        default=None, description="動作代碼完全比對過濾 (例: LOGIN_SUCCESS)"
    ),
    ctx: TenantContext = Depends(verify_tenant),
    db: AsyncSession = Depends(get_db),
    _: TenantContext = Depends(require_role("admin")),
) -> list[dict[str, Any]]:
    """列出本租戶稽核日誌 (新到舊), 支援分頁與 action 過濾。"""
    stmt = select(AuditLog).where(AuditLog.tenant_id == ctx.tenant_id)
    if action:
        stmt = stmt.where(AuditLog.action == action)
    stmt = (
        stmt.order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .offset(offset)
        .limit(limit)
    )

    result = await db.execute(stmt)
    rows = list(result.scalars().all())

    return [
        {
            "id": int(r.id),
            "actor": r.actor,
            "action": r.action,
            "detail": r.detail or {},
            "created_at": (
                r.created_at.isoformat() if r.created_at is not None else None
            ),
        }
        for r in rows
    ]
