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

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core import ratelimit
from app.core.security import create_access_token, hash_password, verify_password
from app import database
from app.deps import TenantContext, verify_tenant
from app.models.orm import AppUser, AuditLog
from app.schemas.auth import LoginRequest, MeResponse, TokenResponse

logger = logging.getLogger("cpm.routers.auth")

router = APIRouter(prefix="/auth", tags=["auth"])

# 預先計算的「假雜湊」：當帳號不存在時仍對它執行一次 verify_password，使
# 「未知帳號」與「已知帳號 / 密碼錯誤」兩條路徑耗時相近，消弭以回應時間
# 探測帳號是否存在的 timing oracle。模組載入時計算一次 (pbkdf2_sha256 純 Python)。
DUMMY_HASH = hash_password("timing-pad-not-a-real-password")

# 鎖定 / 計數無法判定租戶時的稽核 tenant 佔位 (帳號不存在時使用)。
_UNKNOWN_TENANT = "UNKNOWN"

# 稽核動作代碼。
_ACTION_LOGIN_FAILED = "LOGIN_FAILED"
_ACTION_LOGIN_SUCCESS = "LOGIN_SUCCESS"


def _client_ip(request: Request) -> str:
    """取得來源 IP (速率限制 key 之一)。client 可能為 None (測試 / 特殊傳輸)。"""
    client = getattr(request, "client", None)
    return (getattr(client, "host", None) or "unknown") if client else "unknown"


def _rl_key(username: str, ip: str) -> str:
    """速率限制 / 鎖定計數 key：以「帳號 (小寫) + 來源 IP」組成。"""
    return f"{(username or '').lower()}|{ip}"


async def _write_login_audit(
    session: AsyncSession,
    *,
    tenant_id: str,
    actor: str,
    action: str,
    detail: dict | None = None,
) -> None:
    """寫入一筆登入稽核日誌 (best-effort，絕不可使登入失敗)。

    login 早於任何租戶情境，無 TenantContext 可用，故不走 audit.log_action，
    而是直接組 AuditLog 並「先設定租戶 GUC」再 insert —— audit_log 受 RLS
    (enable+force, WITH CHECK tenant policy) 保護，於 PostgreSQL 上必須先以
    set_config('app.current_tenant', tenant, true) 設定，且 row 的 tenant_id
    需與之相符，WITH CHECK 方能通過 (sqlite 下 set_tenant_guc 為 no-op)。
    """
    try:
        await database.set_tenant_guc(session, tenant_id)
        session.add(
            AuditLog(
                tenant_id=tenant_id,
                actor=(actor or None),
                action=action,
                detail=detail or {},
            )
        )
        await session.flush()
        await session.commit()
    except Exception:  # noqa: BLE001 - 稽核失敗不得影響登入結果
        logger.exception("login audit 寫入失敗 (action=%s actor=%s)", action, actor)
        try:
            await session.rollback()
        except Exception:  # noqa: BLE001
            pass


async def _safe_register_failure(key: str) -> None:
    """累加登入失敗計數 (吞掉所有錯誤；速率限制不得使登入崩潰)。"""
    try:
        await ratelimit.register_failure(key)
    except Exception:  # noqa: BLE001
        logger.exception("ratelimit register_failure 失敗 (key=%s)", key)


async def _safe_reset(key: str) -> None:
    """重置登入失敗計數 (吞掉所有錯誤)。"""
    try:
        await ratelimit.reset(key)
    except Exception:  # noqa: BLE001
        logger.exception("ratelimit reset 失敗 (key=%s)", key)


@router.post("/login", response_model=TokenResponse)
async def login(payload: LoginRequest, request: Request) -> TokenResponse:
    """以帳號 / 密碼驗證並簽發 Bearer 權杖 (含速率限制 / 鎖定 / 稽核)。

    安全強化 (FIX-2)：
      1) 速率限制 / 鎖定：以「帳號 + 來源 IP」累計失敗次數，超過門檻後於
         鎖定視窗內回 429 (Redis；無 redis 時退回行程內計數)。
      2) timing-safe：帳號不存在時仍對 DUMMY_HASH 執行一次 verify_password，
         使未知帳號與密碼錯誤兩條路徑耗時相近，消弭 timing oracle。
      3) 稽核：成功 (LOGIN_SUCCESS) / 失敗 (LOGIN_FAILED) 皆寫入 audit_log
         (GUC-safe insert)。稽核 / 速率限制錯誤一律吞掉，絕不影響登入結果。

    app_users 無 RLS，登入早於任何租戶情境，查詢 app_users 本身無須設租戶 GUC；
    但寫稽核時會先設定該筆的 tenant GUC 以通過 RLS WITH CHECK。
    帳號不存在、停用或密碼錯誤一律回 401（不洩漏帳號是否存在）。
    """
    username = payload.username
    ip = _client_ip(request)
    key = _rl_key(username, ip)

    # (a) 已鎖定 -> 429 (在任何 DB 查詢 / 雜湊之前先擋下，避免被鎖期間仍消耗資源)。
    try:
        retry_after = await ratelimit.is_locked(key)
    except Exception:  # noqa: BLE001 - 速率限制查詢失敗不得阻斷登入
        logger.exception("ratelimit is_locked 失敗 (key=%s)，放行本次嘗試", key)
        retry_after = 0
    if retry_after > 0:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="嘗試次數過多，請稍後再試",
            headers={"Retry-After": str(retry_after)},
        )

    # 以 database.SessionLocal 於呼叫時引用 (延遲；見 app.database lazy 設計)，
    # 使測試 rebind 至 sqlite sessionmaker 生效。app_users 無 RLS，無須設租戶 GUC。
    async with database.SessionLocal() as session:
        result = await session.execute(
            select(AppUser).where(AppUser.username == username)
        )
        user = result.scalar_one_or_none()

        # (b) 帳號不存在：仍對 DUMMY_HASH 驗證一次 (timing pad)，再計失敗 + 稽核 + 401。
        if user is None:
            verify_password(payload.password, DUMMY_HASH)
            await _safe_register_failure(key)
            await _write_login_audit(
                session,
                tenant_id=_UNKNOWN_TENANT,
                actor=username,
                action=_ACTION_LOGIN_FAILED,
                detail={"ip": ip, "reason": "unknown_user"},
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid username or password",
                headers={"WWW-Authenticate": "Bearer"},
            )

        # (c) 帳號停用 / 密碼錯誤：計失敗 + 稽核 LOGIN_FAILED + 401。
        if not user.is_active or not verify_password(
            payload.password, user.password_hash
        ):
            await _safe_register_failure(key)
            await _write_login_audit(
                session,
                tenant_id=user.tenant_id,
                actor=username,
                action=_ACTION_LOGIN_FAILED,
                detail={
                    "ip": ip,
                    "reason": "inactive" if not user.is_active else "bad_password",
                },
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid username or password",
                headers={"WWW-Authenticate": "Bearer"},
            )

        # (d) 成功：重置失敗計數 + 稽核 LOGIN_SUCCESS。
        await _safe_reset(key)
        await _write_login_audit(
            session,
            tenant_id=user.tenant_id,
            actor=username,
            action=_ACTION_LOGIN_SUCCESS,
            detail={"ip": ip},
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
