"""使用者管理路由 (Users router) —— admin-only 的使用者 CRUD。

掛載: prefix="/users", 由 main.py 以 settings.api_v1_prefix 為前綴 include。

端點 (全部要求 role=admin):
  GET    /users        列出本租戶使用者 (list[UserOut])。
  POST   /users        建立使用者 (UserCreate -> UserOut); username 重複回 409。
  PUT    /users/{id}   更新使用者 (UserUpdate: role / is_active / password 部分更新)。
  DELETE /users/{id}   刪除使用者 -> {"ok": true}; 禁止刪除自己 / 最後一位 admin。

設計重點:
  * 全部端點以 Depends(require_role("admin")) 守門 —— 非 admin 一律 403。
    require_role 內部讀取的是同一個 TenantContext (verify_tenant 解析而來)。
  * app_users 「不」受 RLS 保護 (登入早於租戶情境), 故此處一律在程式碼層
    以 tenant_id == ctx.tenant_id 過濾 / 限定, 達成租戶隔離。寫入時 tenant_id
    一律取自 ctx (絕不信任輸入)。
  * 密碼一律以 hash_password 雜湊後存入; UserOut 不含 password_hash, 絕不外洩。
  * 安全護欄:
      - username 全域唯一 (app_users.username unique) -> 重複回 409。
      - 禁止刪除「自己」(以 ctx.sub 比對 username), 避免管理者自鎖。
      - 禁止刪除 / 停用 / 降級 致本租戶 active admin 歸零 (保留最後一位 admin)。
  * 稽核: 建立 / 更新 / 刪除 皆呼叫 audit.log_action (隨本端點交易一併提交)。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.security import hash_password
from app.deps import TenantContext, get_db, require_role, verify_tenant
from app.models.orm import AppUser
from app.schemas.users import UserCreate, UserOut, UserUpdate

logger = logging.getLogger("cpm.routers.users")

router = APIRouter(prefix="/users", tags=["users"])


# ---------------------------------------------------------------------------
# 內部工具
# ---------------------------------------------------------------------------
async def _get_user_or_404(
    db: AsyncSession, user_id: int, tenant_id: str
) -> AppUser:
    """依 id 取本租戶使用者; 不存在 (或屬其他租戶) 回 404。"""
    result = await db.execute(
        select(AppUser).where(
            AppUser.id == user_id, AppUser.tenant_id == tenant_id
        )
    )
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="User not found"
        )
    return user


async def _active_admin_count(
    db: AsyncSession, tenant_id: str, *, exclude_id: int | None = None
) -> int:
    """計算本租戶「啟用中的 admin」數量 (可選擇排除某使用者 id)。

    用於最後一位 admin 護欄: 在套用變更「之前」評估若移除 / 降級 / 停用該使用者,
    是否會使 active admin 歸零。
    """
    stmt = select(func.count()).select_from(AppUser).where(
        AppUser.tenant_id == tenant_id,
        AppUser.role == "admin",
        AppUser.is_active.is_(True),
    )
    if exclude_id is not None:
        stmt = stmt.where(AppUser.id != exclude_id)
    result = await db.execute(stmt)
    return int(result.scalar_one() or 0)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@router.get("", response_model=list[UserOut])
async def list_users(
    ctx: TenantContext = Depends(verify_tenant),
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_role("admin")),
) -> list[AppUser]:
    """列出本租戶使用者 (依 id 排序, 輸出穩定); 不含 password_hash。"""
    result = await db.execute(
        select(AppUser)
        .where(AppUser.tenant_id == ctx.tenant_id)
        .order_by(AppUser.id)
    )
    return list(result.scalars().all())


@router.post("", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def create_user(
    payload: UserCreate,
    ctx: TenantContext = Depends(verify_tenant),
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_role("admin")),
) -> AppUser:
    """建立使用者 (本租戶); username 重複回 409。密碼以 hash_password 雜湊存入。"""
    # 預檢 username 唯一 (全域 unique); 重複回 409。
    existing = await db.execute(
        select(AppUser).where(AppUser.username == payload.username)
    )
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Username already exists",
        )

    region = (payload.region or ctx.region or "TW").strip().upper()
    user = AppUser(
        tenant_id=ctx.tenant_id,  # 一律取自 ctx (寫入隔離)
        username=payload.username,
        password_hash=hash_password(payload.password),
        role=payload.role,
        region=region,
        is_active=True,
    )
    db.add(user)
    try:
        # flush 以觸發 unique 約束 (防競態) 並取得自增 id。
        await db.flush()
    except IntegrityError:
        # 競態下 (預檢通過後另一交易插入同名) 由 DB unique 約束攔截 -> 409。
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Username already exists",
        )

    await audit.log_action(
        db,
        ctx,
        "USER_CREATE",
        {"user_id": user.id, "username": user.username, "role": user.role},
    )
    return user


@router.put("/{user_id}", response_model=UserOut)
async def update_user(
    user_id: int,
    payload: UserUpdate,
    ctx: TenantContext = Depends(verify_tenant),
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_role("admin")),
) -> AppUser:
    """更新使用者 (本租戶; 部分更新: role / is_active / password)。

    最後一位 admin 護欄: 若此變更會「降級」或「停用」本租戶唯一一位 active admin,
    則回 400 (避免租戶失去所有管理者)。
    """
    user = await _get_user_or_404(db, user_id, ctx.tenant_id)

    # 評估此次變更是否會移除該使用者的 active-admin 身分。
    is_currently_active_admin = user.role == "admin" and bool(user.is_active)
    will_be_admin = payload.role if payload.role is not None else user.role
    will_be_active = (
        payload.is_active if payload.is_active is not None else bool(user.is_active)
    )
    losing_active_admin = is_currently_active_admin and not (
        will_be_admin == "admin" and will_be_active
    )
    if losing_active_admin:
        others = await _active_admin_count(
            db, ctx.tenant_id, exclude_id=user.id
        )
        if others == 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot demote or deactivate the last admin",
            )

    changed: dict[str, object] = {}
    if payload.role is not None:
        user.role = payload.role
        changed["role"] = payload.role
    if payload.is_active is not None:
        user.is_active = payload.is_active
        changed["is_active"] = payload.is_active
    if payload.password is not None:
        user.password_hash = hash_password(payload.password)
        changed["password"] = "***reset***"

    await db.flush()

    await audit.log_action(
        db,
        ctx,
        "USER_UPDATE",
        {"user_id": user.id, "username": user.username, "changed": changed},
    )
    return user


@router.delete("/{user_id}")
async def delete_user(
    user_id: int,
    ctx: TenantContext = Depends(verify_tenant),
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_role("admin")),
) -> dict:
    """刪除使用者 (本租戶) -> {"ok": true}。

    護欄:
      - 禁止刪除「自己」(以 ctx.sub 比對 username), 避免管理者自鎖 -> 400。
      - 禁止刪除致本租戶 active admin 歸零 (保留最後一位 admin) -> 400。
    """
    user = await _get_user_or_404(db, user_id, ctx.tenant_id)

    # 禁止刪除自己 (登入主體)。ctx.sub 於 header / dev 模式可能為空字串。
    if ctx.sub and user.username == ctx.sub:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete yourself",
        )

    # 最後一位 admin 護欄: 刪除一位 active admin 時, 須尚有其他 active admin。
    if user.role == "admin" and bool(user.is_active):
        others = await _active_admin_count(
            db, ctx.tenant_id, exclude_id=user.id
        )
        if others == 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot delete the last admin",
            )

    username = user.username
    await db.delete(user)
    await db.flush()

    await audit.log_action(
        db,
        ctx,
        "USER_DELETE",
        {"user_id": user_id, "username": username},
    )
    return {"ok": True}
