"""租戶開通 CLI (Tenant provisioning CLI) — Batch 3 FEAT-6.

以單一指令完成新租戶的「開通三件套」：
  1. public.tenants 租戶列 (已存在 -> 清楚報錯、結束碼 1)
  2. erp_integration.tenant_erp_config ERP 設定列 (erp_type 預設 DINGXIN_TW、
     api_endpoint 留空 => ERP 拋轉走模擬模式，待正式介接時再補端點)
  3. role=admin 的初始管理員 AppUser (密碼以 hash_password 雜湊；
     username 已存在 -> 清楚報錯、結束碼 1)

執行方式::

    python -m app.provision_tenant \
        --tenant-id TENT-NEW --name "新租戶營造" --region TW \
        --admin-username admin@new --admin-password s3cret \
        [--erp-type SAP|DINGXIN_TW|YONYOU_CN]

關鍵設計：
  - 使用「自己的」async engine / AsyncSession (與 app.erp.worker 同模式)：
    sqlite (dev) 分支以 schema_translate_map 將 erp_integration 映射為 None，
    PostgreSQL 正式環境直接連線。engine 於結束時 dispose (CLI 短生命週期)。
  - 「同一交易」內先 set_tenant_guc(tenant_id) (PostgreSQL 之 tenants 表受 RLS
    WITH CHECK 規範，須先設定 GUC 方可 INSERT；sqlite 為 no-op)，再依序插入
    三件套 —— 任一衝突即整體回滾，不留半套狀態。
  - 衝突 (租戶 / 帳號已存在) -> ProvisioningError -> 印出清楚訊息、exit 1。
  - ``main(argv)`` 可帶入參數清單供測試直接 import 呼叫 (asyncio)。

成功時印出雙語 (繁中 / English) 開通摘要。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import is_sqlite, settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("app.provision_tenant")

# 允許的地區 / ERP 類型 (與 schema 慣例對齊)
REGIONS = ("TW", "CN")
ERP_TYPES = ("SAP", "DINGXIN_TW", "YONYOU_CN")
DEFAULT_ERP_TYPE = "DINGXIN_TW"


class ProvisioningError(RuntimeError):
    """開通失敗 (衝突 / 驗證錯誤)。訊息直接面向操作者，務求清楚。"""


def _build_engine():
    """建立 CLI 專用 async engine (與 worker 相同的 sqlite / postgres 分支)。

    於「呼叫時」依 settings.database_url 建立 (非 import 時)，使測試以
    env-at-top 暫時 sqlite 重綁 DSN 後直接呼叫 main() 亦能連到正確 DB。
    """
    if is_sqlite():
        return create_async_engine(
            settings.database_url,
            future=True,
            connect_args={"check_same_thread": False},
            execution_options={"schema_translate_map": {"erp_integration": None}},
        )
    return create_async_engine(settings.database_url, pool_pre_ping=True, future=True)


async def provision_tenant(
    *,
    tenant_id: str,
    name: str,
    region: str,
    admin_username: str,
    admin_password: str,
    erp_type: str = DEFAULT_ERP_TYPE,
) -> dict:
    """開通新租戶 (tenants + tenant_erp_config + admin AppUser，單一交易)。

    成功回傳摘要 dict；衝突 / 驗證失敗拋出 :class:`ProvisioningError`。
    """
    # 延後匯入：避免 import 本模組時即觸發 ORM / passlib 載入順序問題。
    from app.core.security import hash_password
    from app.database import set_tenant_guc
    from app.models.orm import AppUser, ErpConfig, Tenant

    tenant_id = (tenant_id or "").strip()
    name = (name or "").strip()
    region = (region or "").strip().upper()
    admin_username = (admin_username or "").strip()
    erp_type = (erp_type or DEFAULT_ERP_TYPE).strip().upper() or DEFAULT_ERP_TYPE

    if not tenant_id:
        raise ProvisioningError("tenant-id 不可為空 | tenant-id must not be empty")
    if not name:
        raise ProvisioningError("name 不可為空 | name must not be empty")
    if region not in REGIONS:
        raise ProvisioningError(
            f"region 必須為 {'/'.join(REGIONS)} | region must be one of {REGIONS}"
        )
    if erp_type not in ERP_TYPES:
        raise ProvisioningError(
            f"erp-type 必須為 {'/'.join(ERP_TYPES)} | erp-type must be one of {ERP_TYPES}"
        )
    if not admin_username:
        raise ProvisioningError(
            "admin-username 不可為空 | admin-username must not be empty"
        )
    if not admin_password:
        raise ProvisioningError(
            "admin-password 不可為空 | admin-password must not be empty"
        )

    engine = _build_engine()
    SessionLocal = async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False
    )
    try:
        async with SessionLocal() as session:
            try:
                async with session.begin():
                    # 同一交易：先設 RLS GUC (is_local=true，限本交易；sqlite no-op)，
                    # tenants 之 INSERT 才能通過 PostgreSQL RLS WITH CHECK。
                    await set_tenant_guc(session, tenant_id)

                    # --- 衝突檢查 (清楚報錯，不靠 DB 例外猜原因) -----------------
                    if await session.get(Tenant, tenant_id) is not None:
                        raise ProvisioningError(
                            f"租戶已存在，拒絕重複開通：{tenant_id} | "
                            f"tenant already exists: {tenant_id}"
                        )
                    found = await session.execute(
                        select(AppUser).where(AppUser.username == admin_username)
                    )
                    if found.scalar_one_or_none() is not None:
                        raise ProvisioningError(
                            f"管理員帳號已存在：{admin_username} | "
                            f"admin username already exists: {admin_username}"
                        )
                    if await session.get(ErpConfig, tenant_id) is not None:
                        raise ProvisioningError(
                            f"ERP 設定已存在 (殘留資料?)：{tenant_id} | "
                            f"tenant_erp_config already exists: {tenant_id}"
                        )

                    # --- 開通三件套 (同交易原子提交) ----------------------------
                    session.add(Tenant(tenant_id=tenant_id, name=name, region=region))
                    session.add(
                        ErpConfig(
                            tenant_id=tenant_id,
                            erp_type=erp_type,
                            api_endpoint="",  # 留空 => ERP 拋轉走模擬模式
                            is_active=True,
                        )
                    )
                    session.add(
                        AppUser(
                            tenant_id=tenant_id,
                            username=admin_username,
                            password_hash=hash_password(admin_password),
                            region=region,
                            role="admin",
                            is_active=True,
                        )
                    )
                    # 離開 session.begin() 時自動 commit。
            except IntegrityError as exc:
                # 競態下的唯一鍵 / 主鍵衝突 (檢查通過後才撞到) —— 轉為清楚訊息。
                raise ProvisioningError(
                    f"資料庫唯一性衝突 (租戶或帳號已存在) | "
                    f"unique-constraint conflict (tenant or username already exists): {exc.orig}"
                ) from exc
    finally:
        await engine.dispose()

    return {
        "tenant_id": tenant_id,
        "name": name,
        "region": region,
        "erp_type": erp_type,
        "admin_username": admin_username,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.provision_tenant",
        description="開通新租戶 (tenants + ERP 設定 + 初始管理員) | "
        "Provision a new tenant (tenant row + ERP config + admin user)",
    )
    parser.add_argument("--tenant-id", required=True, help="租戶代碼 | tenant id")
    parser.add_argument("--name", required=True, help="租戶名稱 | tenant display name")
    parser.add_argument(
        "--region", required=True, choices=REGIONS, help="地區 TW/CN | region"
    )
    parser.add_argument(
        "--admin-username", required=True, help="初始管理員帳號 | initial admin username"
    )
    parser.add_argument(
        "--admin-password", required=True, help="初始管理員密碼 | initial admin password"
    )
    parser.add_argument(
        "--erp-type",
        default=DEFAULT_ERP_TYPE,
        choices=ERP_TYPES,
        help=f"ERP 類型 (預設 {DEFAULT_ERP_TYPE}) | ERP type",
    )
    return parser


def _print_summary(summary: dict) -> None:
    """印出雙語開通摘要 (繁中 / English)。"""
    print("=== 租戶開通完成 | Tenant provisioned successfully ===")
    print(
        f"  租戶 Tenant      : {summary['tenant_id']} ({summary['name']}) "
        f"region={summary['region']}"
    )
    print(
        f"  ERP 設定 Config  : {summary['erp_type']} "
        "(api_endpoint 未設定，拋轉為模擬模式 | endpoint empty -> simulate mode)"
    )
    print(f"  管理員 Admin     : {summary['admin_username']} (role=admin)")
    print(
        "  下一步 Next      : 以該管理員登入 POST /api/v1/auth/login | "
        "log in with this admin via POST /api/v1/auth/login"
    )


async def main(argv: list[str] | None = None) -> int:
    """CLI 進入點。回傳結束碼 (0=成功, 1=衝突/失敗)；亦可被測試直接呼叫。"""
    args = _build_parser().parse_args(argv)
    try:
        summary = await provision_tenant(
            tenant_id=args.tenant_id,
            name=args.name,
            region=args.region,
            admin_username=args.admin_username,
            admin_password=args.admin_password,
            erp_type=args.erp_type,
        )
    except ProvisioningError as exc:
        print(f"[錯誤 ERROR] 開通失敗 | provisioning failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - CLI 不可丟裸 traceback 給操作者當結果
        logger.exception("開通時發生未預期例外 | unexpected provisioning error")
        print(f"[錯誤 ERROR] 未預期例外 | unexpected error: {exc}", file=sys.stderr)
        return 1

    _print_summary(summary)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
