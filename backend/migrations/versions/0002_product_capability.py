"""0002 product capability — Batch 3 schema (顯式 DDL，無 autogenerate)。

Revision ID: 0002
Revises: 0001

內容 (與 db/init.sql / ORM 逐欄一致)：
  FEAT-1 依賴型態 + 延遲：task_dependencies.dep_type (FS/SS/FF/SF, 預設 FS)、
         task_dependencies.lag_days (預設 0；可為負 = 提前 lead)。
  FEAT-2 真實日期 + 工作日曆：projects.start_date (DATE NULL)、
         projects.work_days (VARCHAR(7) 預設 '1111110'，週一..週日 1=工作日)；
         新表 project_holidays (每專案例外停工日，UNIQUE(project_id, holiday_date))
         + PostgreSQL RLS ENABLE+FORCE 多租戶政策 (方言保護，sqlite 略過)。
  FEAT-3 樂觀併發：projects.version (INT 預設 0)。
  FEAT-4 軟刪除 / 回收桶：projects.deleted_at (TIMESTAMPTZ NULL)、
         projects.deleted_by (VARCHAR(150) NULL)。

冪等性 (重要)：
  「全新空 DB」的 upgrade 路徑會先跑 0001_baseline —— 它以「目前 ORM」
  create_all，因此 0001 之後新欄位 / 新表「已經存在」。本 revision 以
  sa.inspect 檢查既有欄位 / 表，僅補齊缺漏者，使兩種路徑皆安全：
    - 空 DB：0001 建出完整 schema -> 0002 全數略過 (no-op)。
    - 既有部署 (stamp 在 0001)：0002 補上全部 Batch 3 欄位 / 新表。
  (由 init.sql 佈建的全新 compose DB 不會執行本檔 —— app.migrate 對
   「有核心表、無 alembic_version」者直接 stamp head。)

RLS / GRANT 權責：與 init.sql 一致 —— project_holidays 的 RLS 政策在此補上
(既有部署不會重跑 init.sql)；cpm_app 之資料表/序列權限若由 DB owner (cpm)
執行本遷移，將由 init.sql 的 ALTER DEFAULT PRIVILEGES 自動涵蓋，此處再以
存在性檢查的顯式 GRANT 雙保險。
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def _column_names(inspector: sa.engine.reflection.Inspector, table: str) -> set[str]:
    """回傳資料表既有欄位名集合 (表不存在 -> 空集合)。"""
    try:
        return {col["name"] for col in inspector.get_columns(table)}
    except Exception:  # noqa: BLE001 - 表不存在等情況一律視為「無欄位」
        return set()


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    is_postgres = bind.dialect.name == "postgresql"

    # 可攜型別 —— 與 ORM 對齊：
    #   TIMESTAMPTZ：PG -> TIMESTAMP WITH TIME ZONE；sqlite -> DATETIME。
    #   BIGINT PK ：PG -> BIGINT (autoincrement=BIGSERIAL)；sqlite -> INTEGER
    #               (rowid 別名才會自增)。
    timestamptz = sa.DateTime(timezone=True)
    bigint_pk = sa.BigInteger().with_variant(sa.Integer(), "sqlite")

    # ------------------------------------------------------------------ #
    # 1) projects — FEAT-2 / FEAT-3 / FEAT-4 欄位 (缺者補上)
    # ------------------------------------------------------------------ #
    project_cols = _column_names(inspector, "projects")
    if "start_date" not in project_cols:
        op.add_column("projects", sa.Column("start_date", sa.Date(), nullable=True))
    if "work_days" not in project_cols:
        op.add_column(
            "projects",
            sa.Column(
                "work_days",
                sa.String(7),
                nullable=False,
                server_default="1111110",
            ),
        )
    if "version" not in project_cols:
        op.add_column(
            "projects",
            sa.Column("version", sa.Integer(), nullable=False, server_default="0"),
        )
    if "deleted_at" not in project_cols:
        op.add_column("projects", sa.Column("deleted_at", timestamptz, nullable=True))
    if "deleted_by" not in project_cols:
        op.add_column(
            "projects", sa.Column("deleted_by", sa.String(150), nullable=True)
        )

    # ------------------------------------------------------------------ #
    # 2) task_dependencies — FEAT-1 欄位 (缺者補上)
    # ------------------------------------------------------------------ #
    dep_cols = _column_names(inspector, "task_dependencies")
    if "dep_type" not in dep_cols:
        op.add_column(
            "task_dependencies",
            sa.Column("dep_type", sa.String(2), nullable=False, server_default="FS"),
        )
    if "lag_days" not in dep_cols:
        op.add_column(
            "task_dependencies",
            sa.Column("lag_days", sa.Integer(), nullable=False, server_default="0"),
        )

    # ------------------------------------------------------------------ #
    # 3) project_holidays — FEAT-2 新表 (+索引 +PostgreSQL RLS/GRANT)
    # ------------------------------------------------------------------ #
    if not inspector.has_table("project_holidays"):
        op.create_table(
            "project_holidays",
            sa.Column("id", bigint_pk, primary_key=True, autoincrement=True),
            sa.Column(
                "project_id",
                sa.String(64),
                sa.ForeignKey("projects.project_id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("tenant_id", sa.String(50), nullable=False),
            sa.Column("holiday_date", sa.Date(), nullable=False),
            sa.Column("name", sa.String(120), server_default=""),
            sa.UniqueConstraint(
                "project_id", "holiday_date", name="uq_project_holidays_project_date"
            ),
        )
        op.create_index(
            "idx_project_holidays_project", "project_holidays", ["project_id"]
        )
        op.create_index(
            "idx_project_holidays_tenant", "project_holidays", ["tenant_id"]
        )

        if is_postgres:
            # RLS：與 tasks / projects 相同的多租戶政策 (ENABLE + FORCE —— App
            # 以表擁有者/一般角色連線時政策皆生效)。僅 PostgreSQL；sqlite 無 RLS。
            op.execute(
                "ALTER TABLE public.project_holidays ENABLE ROW LEVEL SECURITY"
            )
            op.execute(
                "ALTER TABLE public.project_holidays FORCE ROW LEVEL SECURITY"
            )
            op.execute(
                "DROP POLICY IF EXISTS tenant_isolation_project_holidays "
                "ON public.project_holidays"
            )
            op.execute(
                "CREATE POLICY tenant_isolation_project_holidays "
                "ON public.project_holidays "
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
                            ON public.project_holidays TO cpm_app;
                        GRANT USAGE, SELECT
                            ON SEQUENCE public.project_holidays_id_seq TO cpm_app;
                    END IF;
                END $$;
                """
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table("project_holidays"):
        op.drop_table("project_holidays")

    # batch_alter_table：sqlite 不支援 (舊版) DROP COLUMN，batch 模式以重建表實現；
    # PostgreSQL 則直接產生 ALTER TABLE ... DROP COLUMN。
    dep_cols = _column_names(inspector, "task_dependencies")
    with op.batch_alter_table("task_dependencies") as batch:
        if "lag_days" in dep_cols:
            batch.drop_column("lag_days")
        if "dep_type" in dep_cols:
            batch.drop_column("dep_type")

    project_cols = _column_names(inspector, "projects")
    with op.batch_alter_table("projects") as batch:
        for col in ("deleted_by", "deleted_at", "version", "work_days", "start_date"):
            if col in project_cols:
                batch.drop_column(col)
