"""認證路由（Auth router）。

掛載：prefix="/auth"，由 main.py 以 settings.api_v1_prefix 為前綴 include。

端點：
  POST /auth/login  以帳號 / 密碼換取 Bearer 權杖。
  GET  /auth/me     回傳目前已驗證身分（由 TenantContext 解析）。

重要：app_users 不受 RLS 保護，且登入發生於「任何租戶情境之前」
（此時尚無 app.current_tenant GUC）。因此 /auth/login 直接使用
SessionLocal() 開啟一般 session、以純 SQLAlchemy select 依 username
查詢，不需設定租戶 GUC（在 sqlite 模式下 set_tenant_guc 亦為 no-op）。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select

from app.config import settings
from app.core.security import create_access_token, verify_password
from app import database
from app.deps import TenantContext, verify_tenant
from app.models.orm import AppUser
from app.schemas.auth import LoginRequest, MeResponse, TokenResponse

logger = logging.getLogger("cpm.routers.auth")

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=TokenResponse)
async def login(payload: LoginRequest) -> TokenResponse:
    """以帳號 / 密碼驗證並簽發 Bearer 權杖。

    app_users 無 RLS，登入早於任何租戶情境，故直接以 SessionLocal()
    查詢，無須設定 app.current_tenant GUC。帳號不存在、停用或密碼
    錯誤一律回 401（不洩漏帳號是否存在）。
    """
    # 以 database.SessionLocal 於呼叫時引用 (延遲；見 app.database lazy 設計)，
    # 使測試 rebind 至 sqlite sessionmaker 生效。app_users 無 RLS，無須設租戶 GUC。
    async with database.SessionLocal() as session:
        result = await session.execute(
            select(AppUser).where(AppUser.username == payload.username)
        )
        user = result.scalar_one_or_none()

    if user is None or not user.is_active or not verify_password(
        payload.password, user.password_hash
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    region = user.region or settings.default_region
    # 角色寫入 token claim 與回應。以 getattr 防呆：若 ORM 尚未具備 role 欄位
    # (向後相容過渡期) 或值為空，皆退回 'admin'，與 DB DEFAULT 'admin' 一致，
    # 既有 demo admin 用戶權限不受影響。
    role = getattr(user, "role", None) or "admin"
    token = create_access_token(
        sub=user.username,
        tenant_id=user.tenant_id,
        region=region,
        role=role,
    )
    return TokenResponse(
        access_token=token,
        token_type="bearer",
        tenant_id=user.tenant_id,
        region=region,
        role=role,
    )


@router.get("/me", response_model=MeResponse)
async def me(ctx: TenantContext = Depends(verify_tenant)) -> MeResponse:
    """回傳目前已驗證身分。

    sub 由 Bearer 權杖解析時可由 verify_tenant 暫存於 TenantContext；
    header 模式（無權杖）下可能為空字串。
    """
    username = getattr(ctx, "sub", None) or ""
    return MeResponse(
        username=username,
        tenant_id=ctx.tenant_id,
        region=ctx.region,
        role=getattr(ctx, "role", None) or "admin",
    )
