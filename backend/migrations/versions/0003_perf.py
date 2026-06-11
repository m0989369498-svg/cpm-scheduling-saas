"""0003 perf — Batch 4 schema (顯式 DDL，無 autogenerate)。

Revision ID: 0003
Revises: 0002

內容 (與 db/init.sql / ORM 逐欄一致)：
  PERF-3 sync_event_log 去 JSON 掃描：
    * erp_integration.sync_event_log.project_id VARCHAR(64) NULL —— 事件所屬
      專案；寫入端 (erp router / risk_listener) 直接設定。
    * 複合索引 (tenant_id, sync_type, status) —— dashboard / exports 的
      風險事件統計查詢 (WHERE tenant_id+sync_type+status GROUP BY project_id)。
    * 回填 (backfill)：既有列的 project_id 自 payload JSON 取出
      (payload->>'project_id')。僅 PostgreSQL —— sqlite dev DB 由
      app/main.py 的冪等 ALTER + json_extract 回填 (live upgrade)。

冪等性 (與 0002 相同模式)：
  「全新空 DB」的 upgrade 路徑會先跑 0001_baseline —— 它以「目前 ORM」
  create_all，因此 0001 之後新欄位 / 新索引「已經存在」。本 revision 以
  sa.inspect 檢查既有欄位 / 索引，僅補齊缺漏者，使兩種路徑皆安全：
    - 空 DB：0001 建出完整 schema -> 0003 全數略過 (僅跑回填，no-op)。
    - 既有部署 (stamp 在 0002)：0003 補上欄位 + 索引 + 回填。
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

# sqlite 無 schema 概念 (env.py 以 schema_translate_map 將 erp_integration
# 映射為 None)；PostgreSQL 則為實際 schema。
_SYNC_TABLE = "sync_event_log"
_INDEX_NAME = "ix_sync_event_log_tenant_type_status"


def _schema_for(bind: sa.engine.Connection) -> str | None:
    return "erp_integration" if bind.dialect.name == "postgresql" else None


def _column_names(
    inspector: sa.engine.reflection.Inspector, table: str, schema: str | None
) -> set[str]:
    """回傳資料表既有欄位名集合 (表不存在 -> 空集合)。"""
    try:
        return {col["name"] for col in inspector.get_columns(table, schema=schema)}
    except Exception:  # noqa: BLE001 - 表不存在等情況一律視為「無欄位」
        return set()


def _index_names(
    inspector: sa.engine.reflection.Inspector, table: str, schema: str | None
) -> set[str]:
    """回傳資料表既有索引名集合 (表不存在 -> 空集合)。"""
    try:
        return {
            ix["name"]
            for ix in inspector.get_indexes(table, schema=schema)
            if ix.get("name")
        }
    except Exception:  # noqa: BLE001
        return set()


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    is_postgres = bind.dialect.name == "postgresql"
    schema = _schema_for(bind)

    # ------------------------------------------------------------------ #
    # 1) sync_event_log.project_id (缺者補上)
    # ------------------------------------------------------------------ #
    cols = _column_names(inspector, _SYNC_TABLE, schema)
    if cols and "project_id" not in cols:
        op.add_column(
            _SYNC_TABLE,
            sa.Column("project_id", sa.String(64), nullable=True),
            schema=schema,
        )

    # ------------------------------------------------------------------ #
    # 2) 複合索引 (tenant_id, sync_type, status) (缺者補上)
    # ------------------------------------------------------------------ #
    if cols and _INDEX_NAME not in _index_names(inspector, _SYNC_TABLE, schema):
        op.create_index(
            _INDEX_NAME,
            _SYNC_TABLE,
            ["tenant_id", "sync_type", "status"],
            schema=schema,
        )

    # ------------------------------------------------------------------ #
    # 3) 回填：既有列的 project_id 自 payload JSON 取出 (僅 PostgreSQL；
    #    sqlite 由 main.py 的冪等 ALTER 以 json_extract 回填)。
    # ------------------------------------------------------------------ #
    if is_postgres and cols:
        op.execute(
            "UPDATE erp_integration.sync_event_log "
            "SET project_id = payload->>'project_id' "
            "WHERE project_id IS NULL"
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    schema = _schema_for(bind)

    if _INDEX_NAME in _index_names(inspector, _SYNC_TABLE, schema):
        op.drop_index(_INDEX_NAME, table_name=_SYNC_TABLE, schema=schema)

    cols = _column_names(inspector, _SYNC_TABLE, schema)
    if "project_id" in cols:
        # batch_alter_table：sqlite (舊版) 不支援 DROP COLUMN，batch 模式以
        # 重建表實現；PostgreSQL 直接產生 ALTER TABLE ... DROP COLUMN。
        with op.batch_alter_table(_SYNC_TABLE, schema=schema) as batch:
            batch.drop_column("project_id")
