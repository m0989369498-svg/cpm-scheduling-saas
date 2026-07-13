"""0005 field reporting — Pro Batch C schema (顯式 DDL，無 autogenerate)。

Revision ID: 0005
Revises: 0004

內容 (與 db/init.sql / ORM 逐欄一致)：
  FEATURE 1 任務照片附件 (mobile field reporting)：新表 task_photos
    (project_id, tenant_id, task_id, stored_name UNIQUE, original_name,
     content_type, size_bytes, note, uploaded_by, created_at)
    + PostgreSQL RLS ENABLE+FORCE 多租戶政策 (方言保護，sqlite 略過)。
  實際檔案內容存於磁碟 (settings.upload_dir / UPLOAD_DIR)，本表僅存中繼資料；
  stored_name 為伺服器產生的 uuid4hex 檔名 (絕不取自使用者輸入)。

冪等性 (與 0002/0003/0004 相同模式)：
  「全新空 DB」的 upgrade 路徑會先跑 0001_baseline —— 它以「目前 ORM」
  create_all，因此 0001 之後新表「已經存在」。本 revision 以 sa.inspect
  檢查既有表，僅在缺漏時建立，使兩種路徑皆安全：
    - 空 DB：0001 建出完整 schema -> 0005 全數略過 (no-op)。
    - 既有部署 (stamp 在 0004)：0005 補上 task_photos 新表。
  (由 init.sql 佈建的全新 compose DB 不會執行本檔 —— app.migrate 對
   「有核心表、無 alembic_version」者直接 stamp head。)

RLS / GRANT 權責：與 init.sql 一致 —— task_photos 的 RLS 政策在此補上
(既有部署不會重跑 init.sql)；cpm_app 之資料表/序列權限若由 DB owner (cpm)
執行本遷移，將由 init.sql 的 ALTER DEFAULT PRIVILEGES 自動涵蓋，此處再以
存在性檢查的顯式 GRANT 雙保險 (沿用 0002/0004 的模式)。
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    is_postgres = bind.dialect.name == "postgresql"

    # 可攜型別 —— 與 ORM 對齊：
    #   BIGINT PK ：PG -> BIGINT (autoincrement=BIGSERIAL)；sqlite -> INTEGER
    #               (rowid 別名才會自增)。
    #   TIMESTAMPTZ：PG -> TIMESTAMP WITH TIME ZONE；sqlite -> DATETIME。
    bigint_pk = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
    timestamptz = sa.DateTime(timezone=True)

    # ------------------------------------------------------------------ #
    # task_photos — FEATURE 1 新表 (+索引 +PostgreSQL RLS/GRANT)
    # ------------------------------------------------------------------ #
    if not inspector.has_table("task_photos"):
        op.create_table(
            "task_photos",
            sa.Column("id", bigint_pk, primary_key=True, autoincrement=True),
            sa.Column(
                "project_id",
                sa.String(64),
                sa.ForeignKey("projects.project_id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("tenant_id", sa.String(50), nullable=False),
            sa.Column("task_id", sa.String(100), nullable=False),
            sa.Column("stored_name", sa.String(80), nullable=False),
            sa.Column(
                "original_name", sa.String(255), nullable=False, server_default=""
            ),
            sa.Column("content_type", sa.String(50), nullable=False),
            sa.Column("size_bytes", sa.Integer(), nullable=False),
            sa.Column("note", sa.String(500), nullable=False, server_default=""),
            sa.Column(
                "uploaded_by", sa.String(150), nullable=False, server_default=""
            ),
            sa.Column("created_at", timestamptz, server_default=sa.func.now()),
            sa.UniqueConstraint("stored_name", name="uq_task_photos_stored_name"),
        )
        op.create_index("idx_task_photos_project", "task_photos", ["project_id"])
        op.create_index("idx_task_photos_tenant", "task_photos", ["tenant_id"])
        op.create_index(
            "idx_task_photos_task", "task_photos", ["project_id", "task_id"]
        )

        if is_postgres:
            # RLS：與 tasks / projects 相同的多租戶政策 (ENABLE + FORCE)。
            # 僅 PostgreSQL；sqlite 無 RLS。
            op.execute("ALTER TABLE public.task_photos ENABLE ROW LEVEL SECURITY")
            op.execute("ALTER TABLE public.task_photos FORCE ROW LEVEL SECURITY")
            op.execute(
                "DROP POLICY IF EXISTS tenant_isolation_task_photos "
                "ON public.task_photos"
            )
            op.execute(
                "CREATE POLICY tenant_isolation_task_photos "
                "ON public.task_photos "
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
                            ON public.task_photos TO cpm_app;
                        GRANT USAGE, SELECT
                            ON SEQUENCE public.task_photos_id_seq TO cpm_app;
                    END IF;
                END $$;
                """
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table("task_photos"):
        op.drop_table("task_photos")
