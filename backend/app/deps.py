"""FastAPI 依賴 (dependencies)。

依賴鏈：
  verify_tenant  -> 解析 X-Tenant-Id / X-Region 標頭 -> TenantContext
  get_db         -> 開啟 AsyncSession 並設定 RLS GUC (依 TenantContext.tenant_id)

所有 API 端點皆要求 X-Tenant-Id (必填)；X-Region 選填 (預設取自設定)。
"""
from __future__ import annotations

from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import SessionLocal, set_tenant_guc


@dataclass
class TenantContext:
    """目前請求之租戶情境。

    tenant_id：租戶識別碼 (對應 RLS app.current_tenant)。
    region：地區 (TW=台灣 / CN=中國大陸)，影響 i18n、通知通道、ERP adapter 預設。
    """

    tenant_id: str
    region: str


async def verify_tenant(
    x_tenant_id: Annotated[str | None, Header(alias="X-Tenant-Id")] = None,
    x_region: Annotated[str | None, Header(alias="X-Region")] = None,
) -> TenantContext:
    """解析多租戶標頭並產生 TenantContext。

    X-Tenant-Id 為必填；缺漏則回傳 400。
    X-Region 選填，未提供時採用 settings.default_region。
    """
    if not x_tenant_id or not x_tenant_id.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing required header: X-Tenant-Id",
        )
    region = (x_region or settings.default_region).strip().upper()
    return TenantContext(tenant_id=x_tenant_id.strip(), region=region)


# 型別別名：方便 router 以 Annotated 形式注入。
TenantDep = Annotated[TenantContext, Depends(verify_tenant)]


async def get_db(
    ctx: TenantDep,
) -> AsyncGenerator[AsyncSession, None]:
    """開啟 AsyncSession，於交易內設定 RLS 租戶 GUC，再 yield 給端點使用。

    模式 (SPEC 權威)：
      async with SessionLocal() as session:
          async with session.begin():
              SELECT set_config('app.current_tenant', :t, true)
              yield session
      成功 => commit (由 begin() 結束時自動)；例外 => rollback 並重拋。
    """
    async with SessionLocal() as session:
        async with session.begin():
            # 進入交易後、yield 前先設定租戶 GUC，RLS 政策方能依此過濾。
            await set_tenant_guc(session, ctx.tenant_id)
            yield session
            # 離開 session.begin() context 時，無例外即 commit，有例外即 rollback。


# 型別別名：DB session 依賴。
DbDep = Annotated[AsyncSession, Depends(get_db)]
