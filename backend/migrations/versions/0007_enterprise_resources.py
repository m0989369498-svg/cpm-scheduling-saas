"""0007 enterprise resources — Pro Batch E schema (顯式 DDL，無 autogenerate)。

Revision ID: 0007
Revises: 0006

內容 (與 db/init.sql / ORM 逐欄一致)：
  FEATURE E1 企業級 (tenant-level) 資源池：新表 tenant_resources
    (id, tenant_id, resource_type, name, category, capacity, unit_cost,
     work_days, UNIQUE(tenant_id, resource_type))
    + PostgreSQL RLS ENABLE+FORCE 多租戶政策 (方言保護，sqlite 略過)。
  FEATURE E2 單一資源專屬例外停工日：新表 resource_calendar_holidays
    (id, project_id, tenant_id, resource_type, holiday_date, name,
     UNIQUE(project_id, resource_type, holiday_date))
    + PostgreSQL RLS ENABLE+FORCE 多租戶政策 (方言保護，sqlite 略過)。

冪等性 (與 0002/0003/0004/0005/0006 相同模式)：
  「全新空 DB」的 upgrade 路徑會先跑 0001_baseline —— 它以「目前 ORM」
  create_all，因此 0001 之後新表「已經存在」。本 revision 以 sa.inspect
  檢查既有表，僅在缺漏時新增，使兩種路徑皆安全：
    - 空 DB：0001 建出完整 schema -> 0007 全數略過 (no-op)。
    - 既有部署 (stamp 在 0006)：0007 補上兩張新表。
  (由 init.sql 佈建的全新 compose DB 不會執行本檔 —— app.migrate 對
   「有核心表、無 alembic_version」者直接 stamp head。)

RLS / GRANT 權責：與 init.sql 一致 —— 兩表的 RLS 政策在此補上 (既有部署
不會重跑 init.sql)；cpm_app 之資料表/序列權限若由 DB owner (cpm) 執行本
遷移，將由 init.sql 的 ALTER DEFAULT PRIVILEGES 自動涵蓋，此處再以存在性
檢查的顯式 GRANT 雙保險 (沿用 0006 的模式)。
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    is_postgres = bind.dialect.name == "postgresql"

    bigint_pk = sa.BigInteger().with_variant(sa.Integer(), "sqlite")

    # ------------------------------------------------------------------ #
    # tenant_resources — FEATURE E1 新表 (+索引 +PostgreSQL RLS/GRANT)
    # ------------------------------------------------------------------ #
    if not inspector.has_table("tenant_resources"):
        op.create_table(
            "tenant_resources",
            sa.Column("id", bigint_pk, primary_key=True, autoincrement=True),
            sa.Column("tenant_id", sa.String(50), nullable=False),
            sa.Column("resource_type", sa.String(50), nullable=False),
            sa.Column(
                "name", sa.String(120), nullable=False, server_default=""
            ),
            sa.Column(
                "category", sa.String(20), nullable=False, server_default="labor"
            ),
            sa.Column("capacity", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("unit_cost", sa.Float(), nullable=False, server_default="0"),
            sa.Column(
                "work_days", sa.String(7), nullable=False, server_default="1111100"
            ),
            sa.UniqueConstraint(
                "tenant_id", "resource_type",
                name="uq_tenant_resources_tenant_type",
            ),
        )
        op.create_index(
            "idx_tenant_resources_tenant", "tenant_resources", ["tenant_id"]
        )

        if is_postgres:
            op.execute(
                "ALTER TABLE public.tenant_resources ENABLE ROW LEVEL SECURITY"
            )
            op.execute(
                "ALTER TABLE public.tenant_resources FORCE ROW LEVEL SECURITY"
            )
            op.execute(
                "DROP POLICY IF EXISTS tenant_isolation_tenant_resources "
                "ON public.tenant_resources"
            )
            op.execute(
                "CREATE POLICY tenant_isolation_tenant_resources "
                "ON public.tenant_resources "
                "USING (tenant_id = current_setting('app.current_tenant', true)) "
                "WITH CHECK "
                "(tenant_id = current_setting('app.current_tenant', true))"
            )
            op.execute(
                """
                DO $$
                BEGIN
                    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cpm_app') THEN
                        GRANT SELECT, INSERT, UPDATE, DELETE
                            ON public.tenant_resources TO cpm_app;
                        GRANT USAGE, SELECT
                            ON SEQUENCE public.tenant_resources_id_seq TO cpm_app;
                    END IF;
                END $$;
                """
            )

    # ------------------------------------------------------------------ #
    # resource_calendar_holidays — FEATURE E2 新表 (+索引 +PostgreSQL RLS/GRANT)
    # ------------------------------------------------------------------ #
    if not inspector.has_table("resource_calendar_holidays"):
        op.create_table(
            "resource_calendar_holidays",
            sa.Column("id", bigint_pk, primary_key=True, autoincrement=True),
            sa.Column(
                "project_id",
                sa.String(64),
                sa.ForeignKey("projects.project_id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("tenant_id", sa.String(50), nullable=False),
            sa.Column("resource_type", sa.String(50), nullable=False),
            sa.Column("holiday_date", sa.Date(), nullable=False),
            sa.Column(
                "name", sa.String(120), nullable=False, server_default=""
            ),
            sa.UniqueConstraint(
                "project_id", "resource_type", "holiday_date",
                name="uq_resource_cal_holidays_project_type_date",
            ),
        )
        op.create_index(
            "idx_resource_cal_holidays_project",
            "resource_calendar_holidays",
            ["project_id"],
        )

        if is_postgres:
            op.execute(
                "ALTER TABLE public.resource_calendar_holidays "
                "ENABLE ROW LEVEL SECURITY"
            )
            op.execute(
                "ALTER TABLE public.resource_calendar_holidays "
                "FORCE ROW LEVEL SECURITY"
            )
            op.execute(
                "DROP POLICY IF EXISTS tenant_isolation_resource_cal_holidays "
                "ON public.resource_calendar_holidays"
            )
            op.execute(
                "CREATE POLICY tenant_isolation_resource_cal_holidays "
                "ON public.resource_calendar_holidays "
                "USING (tenant_id = current_setting('app.current_tenant', true)) "
                "WITH CHECK "
                "(tenant_id = current_setting('app.current_tenant', true))"
            )
            op.execute(
                """
                DO $$
                BEGIN
                    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cpm_app') THEN
                        GRANT SELECT, INSERT, UPDATE, DELETE
                            ON public.resource_calendar_holidays TO cpm_app;
                        GRANT USAGE, SELECT
                            ON SEQUENCE public.resource_calendar_holidays_id_seq
                            TO cpm_app;
                    END IF;
                END $$;
                """
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table("resource_calendar_holidays"):
        op.drop_table("resource_calendar_holidays")
    if inspector.has_table("tenant_resources"):
        op.drop_table("tenant_resources")
