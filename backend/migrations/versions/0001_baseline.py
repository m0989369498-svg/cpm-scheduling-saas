"""0001 baseline — 以 ORM metadata 建出「目前完整 schema」。

Revision ID: 0001
Revises: None (起點)

行為：
- upgrade()：於「空 DB」上以 ``Base.metadata.create_all`` 建立目前全部資料表
  (checkfirst=True，已存在的表略過)。PostgreSQL 分支會先確保
  ``erp_integration`` schema 存在。
- downgrade()：不支援 (baseline 無可回退的前一狀態) -> NotImplementedError。

權責劃分 (重要)：
- RLS 政策 (ENABLE/FORCE ROW LEVEL SECURITY + policy)、``cpm_app`` 角色與
  GRANT/ALTER DEFAULT PRIVILEGES、以及種子資料，仍由 ``db/init.sql`` 權威管理
  (compose 全新啟動由 docker-entrypoint-initdb.d 套用)，或由 ops 手動套用。
  本 revision「只建表」—— 在未跑 init.sql 的空 DB 上 upgrade 後，請另行套用
  init.sql 的 RLS / 角色 / GRANT 區塊，多租戶隔離才會生效。
- 既有部署 (init.sql 已建表) 不應執行本 revision 的 DDL：``app.migrate`` 會
  改以 ``alembic stamp head`` 認定現況即 baseline。
- 未來的 schema 變更：新增 revision (alembic revision)，同時更新 db/init.sql
  與 ORM，保持 column-for-column 一致。
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 延遲匯入：確保所有 ORM 表都註冊到 Base.metadata 後再 create_all
    from app.database import Base
    from app.models import orm  # noqa: F401

    bind = op.get_bind()

    if bind.dialect.name == "postgresql":
        # ORM 中 erp_integration.* 表需要 schema 先存在 (init.sql 之外的空 DB)
        op.execute("CREATE SCHEMA IF NOT EXISTS erp_integration")

    # checkfirst=True：冪等 —— 已存在的表 (例如部分佈建) 不重建、不報錯
    Base.metadata.create_all(bind=bind, checkfirst=True)


def downgrade() -> None:
    raise NotImplementedError(
        "0001 baseline 不支援 downgrade (起點之前沒有可回退的 schema 狀態)"
    )
