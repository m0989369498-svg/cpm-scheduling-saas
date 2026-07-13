"""0004 wbs constraints baselines — Batch 5 schema (顯式 DDL，無 autogenerate)。

Revision ID: 0004
Revises: 0003

內容 (與 db/init.sql / ORM 逐欄一致)：
  FEAT-1 WBS 階層：新表 wbs_nodes (project_id, tenant_id, wbs_code, name,
         parent_code, sort_order；UNIQUE(project_id, wbs_code)) + PostgreSQL
         RLS ENABLE+FORCE 多租戶政策 (方言保護，sqlite 略過)；
         tasks.wbs_code VARCHAR(60) NULL (選填參照，刻意不設 FK)。
  FEAT-2 活動限制 (P6-style constraints)：tasks.constraint_type VARCHAR(10) NULL
         (SNET/SNLT/FNET/FNLT/MSO/MFO)、tasks.constraint_day INT NULL、
         tasks.constraint_violated BOOLEAN NOT NULL DEFAULT false (float_time<0；
         隨 CPM 重算持久化，與 is_critical 同模式)。皆為 NULL/false = 今日行為
         不變 (向後相容)。
  FEAT-3 多組具名基準線：project_baselines.is_active BOOLEAN NOT NULL
         DEFAULT false (同專案僅一條為 true，應用層原子維護；全 false 時
         退回「最新」為作用中基準，向後相容)。

冪等性 (與 0002/0003 相同模式)：
  「全新空 DB」的 upgrade 路徑會先跑 0001_baseline —— 它以「目前 ORM」
  create_all，因此 0001 之後新欄位 / 新表「已經存在」。本 revision 以
  sa.inspect 檢查既有欄位 / 表，僅補齊缺漏者，使兩種路徑皆安全：
    - 空 DB：0001 建出完整 schema -> 0004 全數略過 (no-op)。
    - 既有部署 (stamp 在 0003)：0004 補上全部 Batch 5 欄位 / 新表。
  (由 init.sql 佈建的全新 compose DB 不會執行本檔 —— app.migrate 對
   「有核心表、無 alembic_version」者直接 stamp head。)

RLS / GRANT 權責：與 init.sql 一致 —— wbs_nodes 的 RLS 政策在此補上
(既有部署不會重跑 init.sql)；cpm_app 之資料表/序列權限若由 DB owner (cpm)
執行本遷移，將由 init.sql 的 ALTER DEFAULT PRIVILEGES 自動涵蓋，此處再以
存在性檢查的顯式 GRANT 雙保險 (沿用 0002 project_holidays 的模式)。
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0004"
down_revision = "0003"
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

    # 可攜自增主鍵型別 —— 與 ORM 對齊 (PG -> BIGINT/BIGSERIAL；sqlite -> INTEGER
    # rowid 別名才會自增)。
    bigint_pk = sa.BigInteger().with_variant(sa.Integer(), "sqlite")

    # ------------------------------------------------------------------ #
    # 1) tasks — FEAT-1 / FEAT-2 欄位 (缺者補上)
    # ------------------------------------------------------------------ #
    task_cols = _column_names(inspector, "tasks")
    if "wbs_code" not in task_cols:
        op.add_column("tasks", sa.Column("wbs_code", sa.String(60), nullable=True))
    if "constraint_type" not in task_cols:
        op.add_column(
            "tasks", sa.Column("constraint_type", sa.String(10), nullable=True)
        )
    if "constraint_day" not in task_cols:
        op.add_column(
            "tasks", sa.Column("constraint_day", sa.Integer(), nullable=True)
        )
    if "constraint_violated" not in task_cols:
        op.add_column(
            "tasks",
            sa.Column(
                "constraint_violated",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
        )

    # ------------------------------------------------------------------ #
    # 2) project_baselines — FEAT-3 欄位 (缺者補上)
    # ------------------------------------------------------------------ #
    baseline_cols = _column_names(inspector, "project_baselines")
    if "is_active" not in baseline_cols:
        op.add_column(
            "project_baselines",
            sa.Column(
                "is_active", sa.Boolean(), nullable=False, server_default=sa.false()
            ),
        )

    # ------------------------------------------------------------------ #
    # 3) wbs_nodes — FEAT-1 新表 (+索引 +PostgreSQL RLS/GRANT)
    # ------------------------------------------------------------------ #
    if not inspector.has_table("wbs_nodes"):
        op.create_table(
            "wbs_nodes",
            sa.Column("id", bigint_pk, primary_key=True, autoincrement=True),
            sa.Column(
                "project_id",
                sa.String(64),
                sa.ForeignKey("projects.project_id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("tenant_id", sa.String(50), nullable=False),
            sa.Column("wbs_code", sa.String(60), nullable=False),
            sa.Column("name", sa.String(255), nullable=False, server_default=""),
            sa.Column("parent_code", sa.String(60), nullable=True),
            sa.Column(
                "sort_order", sa.Integer(), nullable=False, server_default="0"
            ),
            sa.UniqueConstraint(
                "project_id", "wbs_code", name="uq_wbs_nodes_project_code"
            ),
        )
        op.create_index("idx_wbs_nodes_project", "wbs_nodes", ["project_id"])
        op.create_index("idx_wbs_nodes_tenant", "wbs_nodes", ["tenant_id"])

        if is_postgres:
            # RLS：與 tasks / projects 相同的多租戶政策 (ENABLE + FORCE)。
            # 僅 PostgreSQL；sqlite 無 RLS。
            op.execute("ALTER TABLE public.wbs_nodes ENABLE ROW LEVEL SECURITY")
            op.execute("ALTER TABLE public.wbs_nodes FORCE ROW LEVEL SECURITY")
            op.execute(
                "DROP POLICY IF EXISTS tenant_isolation_wbs_nodes "
                "ON public.wbs_nodes"
            )
            op.execute(
                "CREATE POLICY tenant_isolation_wbs_nodes "
                "ON public.wbs_nodes "
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
                            ON public.wbs_nodes TO cpm_app;
                        GRANT USAGE, SELECT
                            ON SEQUENCE public.wbs_nodes_id_seq TO cpm_app;
                    END IF;
                END $$;
                """
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table("wbs_nodes"):
        op.drop_table("wbs_nodes")

    baseline_cols = _column_names(inspector, "project_baselines")
    with op.batch_alter_table("project_baselines") as batch:
        if "is_active" in baseline_cols:
            batch.drop_column("is_active")

    # batch_alter_table：sqlite 不支援 (舊版) DROP COLUMN，batch 模式以重建表實現；
    # PostgreSQL 則直接產生 ALTER TABLE ... DROP COLUMN。
    task_cols = _column_names(inspector, "tasks")
    with op.batch_alter_table("tasks") as batch:
        for col in (
            "constraint_violated",
            "constraint_day",
            "constraint_type",
            "wbs_code",
        ):
            if col in task_cols:
                batch.drop_column(col)
