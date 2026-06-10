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
from app.core.security import decode_token
from app import database
from app.database import set_tenant_guc


@dataclass
class TenantContext:
    """目前請求之租戶情境。

    tenant_id：租戶識別碼 (對應 RLS app.current_tenant)。
    region：地區 (TW=台灣 / CN=中國大陸)，影響 i18n、通知通道、ERP adapter 預設。
    sub：登入主體 (JWT sub，通常為 username)；header 模式下為空字串。
    """

    tenant_id: str
    region: str
    sub: str = ""


async def verify_tenant(
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    x_tenant_id: Annotated[str | None, Header(alias="X-Tenant-Id")] = None,
    x_region: Annotated[str | None, Header(alias="X-Region")] = None,
) -> TenantContext:
    """解析認證並產生 TenantContext (雙模式：Bearer JWT / 多租戶標頭)。

    解析順序：
      1) 若 Authorization 為 "Bearer <token>" (不分大小寫)：解碼 JWT；無效/過期
         回 401。租戶/地區皆取自 token claims (X-Tenant-Id 標頭被忽略)。
      2) 否則 (無 bearer)：
         - settings.auth_required=True  => 401 (必須登入)。
         - settings.auth_required=False (dev/header 模式)：
             X-Tenant-Id 存在 => 由標頭組 TenantContext (region 取 X-Region 或預設)。
             X-Tenant-Id 缺漏 => 400 (保留既有契約，使 test_api 持續通過)。
    """
    if authorization and authorization.strip().lower().startswith("bearer "):
        token = authorization.strip()[7:].strip()
        try:
            claims = decode_token(token)
        except Exception:
            # decode_token 於無效/過期時拋出 (ValueError / JWTError)。
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        tenant_id = claims.get("tenant_id")
        if not tenant_id:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token: missing tenant_id",
                headers={"WWW-Authenticate": "Bearer"},
            )
        region = (claims.get("region") or settings.default_region).strip().upper()
        return TenantContext(
            tenant_id=str(tenant_id),
            region=region,
            sub=str(claims.get("sub") or ""),
        )

    # --- 無 Bearer token ---
    if settings.auth_required:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # dev / header 模式：保留既有 X-Tenant-Id 必填 (400) 契約。
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
    # 以 database.SessionLocal 於「呼叫時」引用 (延遲解析)，使測試 rebind
    # (將 database.SessionLocal 指向 sqlite sessionmaker) 能生效，且避免 import
    # 階段就建立 engine (見 app.database 之 lazy 設計)。
    async with database.SessionLocal() as session:
        async with session.begin():
            # 進入交易後、yield 前先設定租戶 GUC，RLS 政策方能依此過濾。
            await set_tenant_guc(session, ctx.tenant_id)
            yield session
            # 離開 session.begin() context 時，無例外即 commit，有例外即 rollback。


# 型別別名：DB session 依賴。
DbDep = Annotated[AsyncSession, Depends(get_db)]
