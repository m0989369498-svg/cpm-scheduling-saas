"""0006 resource cost + calendars — Pro Batch D schema (顯式 DDL，無 autogenerate)。

Revision ID: 0006
Revises: 0005

內容 (與 db/init.sql / ORM 逐欄一致)：
  FEATURE D1 資源池 + 費率 + 成本負載：
    project_resource_limits 新增兩個向下相容欄位：
      unit_cost  REAL/DOUBLE PRECISION NOT NULL DEFAULT 0
      category   VARCHAR(20) NOT NULL DEFAULT 'labor'
  FEATURE D3 每資源專屬工作日曆：新表 resource_calendars
    (project_id, tenant_id, resource_type, work_days，
     UNIQUE(project_id, resource_type))
    + PostgreSQL RLS ENABLE+FORCE 多租戶政策 (方言保護，sqlite 略過)。

冪等性 (與 0002/0003/0004/0005 相同模式)：
  「全新空 DB」的 upgrade 路徑會先跑 0001_baseline —— 它以「目前 ORM」
  create_all，因此 0001 之後新欄位/新表「已經存在」。本 revision 以
  sa.inspect 檢查既有欄位/表，僅在缺漏時新增，使兩種路徑皆安全：
    - 空 DB：0001 建出完整 schema -> 0006 全數略過 (no-op)。
    - 既有部署 (stamp 在 0005)：0006 補上新欄位 + resource_calendars 新表。
  (由 init.sql 佈建的全新 compose DB 不會執行本檔 —— app.migrate 對
   「有核心表、無 alembic_version」者直接 stamp head。)

RLS / GRANT 權責：與 init.sql 一致 —— resource_calendars 的 RLS 政策在此
補上 (既有部署不會重跑 init.sql)；cpm_app 之資料表/序列權限若由 DB owner
(cpm) 執行本遷移，將由 init.sql 的 ALTER DEFAULT PRIVILEGES 自動涵蓋，
此處再以存在性檢查的顯式 GRANT 雙保險 (沿用 0002/0004/0005 的模式)。
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    is_postgres = bind.dialect.name == "postgresql"

    bigint_pk = sa.BigInteger().with_variant(sa.Integer(), "sqlite")

    # ------------------------------------------------------------------ #
    # project_resource_limits — FEATURE D1 新欄位 (unit_cost / category)
    # ------------------------------------------------------------------ #
    existing_cols = {
        c["name"] for c in inspector.get_columns("project_resource_limits")
    }
    if "unit_cost" not in existing_cols:
        op.add_column(
            "project_resource_limits",
            sa.Column(
                "unit_cost",
                sa.Float(),
                nullable=False,
                server_default="0",
            ),
        )
    if "category" not in existing_cols:
        op.add_column(
            "project_resource_limits",
            sa.Column(
                "category",
                sa.String(20),
                nullable=False,
                server_default="labor",
            ),
        )

    # ------------------------------------------------------------------ #
    # resource_calendars — FEATURE D3 新表 (+索引 +PostgreSQL RLS/GRANT)
    # ------------------------------------------------------------------ #
    if not inspector.has_table("resource_calendars"):
        op.create_table(
            "resource_calendars",
            sa.Column("id", bigint_pk, primary_key=True, autoincrement=True),
            sa.Column(
                "project_id",
                sa.String(64),
                sa.ForeignKey("projects.project_id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("tenant_id", sa.String(50), nullable=False),
            sa.Column("resource_type", sa.String(50), nullable=False),
            sa.Column(
                "work_days", sa.String(7), nullable=False, server_default="1111110"
            ),
            sa.UniqueConstraint(
                "project_id", "resource_type",
                name="uq_resource_calendar_project_type",
            ),
        )
        op.create_index(
            "idx_resource_calendars_project", "resource_calendars", ["project_id"]
        )
        op.create_index(
            "idx_resource_calendars_tenant", "resource_calendars", ["tenant_id"]
        )

        if is_postgres:
            # RLS：與 project_resource_limits 相同的多租戶政策 (ENABLE + FORCE)。
            # 僅 PostgreSQL；sqlite 無 RLS。
            op.execute(
                "ALTER TABLE public.resource_calendars ENABLE ROW LEVEL SECURITY"
            )
            op.execute(
                "ALTER TABLE public.resource_calendars FORCE ROW LEVEL SECURITY"
            )
            op.execute(
                "DROP POLICY IF EXISTS tenant_isolation_resource_calendars "
                "ON public.resource_calendars"
            )
            op.execute(
                "CREATE POLICY tenant_isolation_resource_calendars "
                "ON public.resource_calendars "
                "USING (tenant_id = current_setting('app.current_tenant', true)) "
                "WITH CHECK "
                "(tenant_id = current_setting('app.current_tenant', true))"
            )
            # GRANT 雙保險：若應用角色 cpm_app 存在 (init.sql 佈建的部署)，
            # 顯式授權新表/新序列 —— 涵蓋「遷移執行者非 cpm (DB owner)、
            # ALTER DEFAULT PRIVILEGES 未生效」的邊角情況。
            op.execute(
                """
                DO $$
                BEGIN
                    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cpm_app') THEN
                        GRANT SELECT, INSERT, UPDATE, DELETE
                            ON public.resource_calendars TO cpm_app;
                        GRANT USAGE, SELECT
                            ON SEQUENCE public.resource_calendars_id_seq TO cpm_app;
                    END IF;
                END $$;
                """
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table("resource_calendars"):
        op.drop_table("resource_calendars")

    existing_cols = {
        c["name"] for c in inspector.get_columns("project_resource_limits")
    }
    if "category" in existing_cols:
        op.drop_column("project_resource_limits", "category")
    if "unit_cost" in existing_cols:
        op.drop_column("project_resource_limits", "unit_cost")
