"""SQLAlchemy 2.0 declarative ORM 模型 —— 逐欄對應 db/init.sql。

兩個 schema：
  public (預設)               : tenants, projects, tasks, task_dependencies,
                                project_holidays 等 (受 RLS 保護)
  erp_integration (服務管理)  : tenant_erp_config, task_mapping, sync_event_log,
                                tenant_notification_config, notification_outbox (無 RLS)

erp_integration 模型以 __table_args__ = {"schema": "erp_integration"} 指定 schema。
"""
from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import TIMESTAMP as PG_TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

# ---------------------------------------------------------------------------
# 可攜型別 (portable types) —— 同一份 ORM 能於 PostgreSQL 與 sqlite 皆 create_all。
#   TIMESTAMP_TZ：PostgreSQL 用原生 TIMESTAMP(timezone=True)；其餘方言 (sqlite)
#                 退回通用 DateTime(timezone=True)。語義 (帶時區) 不變。
#   JSON_PORTABLE：PostgreSQL 用 JSONB；其餘方言 (sqlite) 退回通用 JSON。
# 欄位名稱 / 語義皆不變，僅讓 DDL 在兩種方言下都成立。
# ---------------------------------------------------------------------------
TIMESTAMP_TZ = DateTime(timezone=True).with_variant(
    PG_TIMESTAMP(timezone=True), "postgresql"
)
JSON_PORTABLE = JSON().with_variant(JSONB, "postgresql")

# 可攜自增主鍵型別 (portable autoincrement PK)。
#   PostgreSQL：BigInteger -> BIGSERIAL / BIGINT (與 init.sql 的 BIGSERIAL 對齊)。
#   sqlite    ：退回 Integer，使 DDL 產生「INTEGER PRIMARY KEY」—— 這是 sqlite
#               中「rowid 別名」的唯一寫法，欄位才會自動遞增。若維持 BigInteger
#               則 sqlite 產生 "BIGINT PRIMARY KEY"，不是 rowid 別名，INSERT 省略
#               id 時會 NOT NULL constraint failed (種子資料即因此失敗)。
#   語義不變：兩種方言皆為 64 位足夠的自增整數主鍵 (sqlite rowid 為 64-bit)。
BIGINT_PK = BigInteger().with_variant(Integer, "sqlite")


# ---------------------------------------------------------------------------
# public schema —— 核心應用資料表 (受 RLS 保護)
# ---------------------------------------------------------------------------
class Tenant(Base):
    """租戶 (tenants)。多租戶 SaaS 之頂層隔離單位。"""

    __tablename__ = "tenants"

    tenant_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    name: Mapped[str | None] = mapped_column(String(200))
    region: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'TW'")
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP_TZ, server_default=func.now()
    )

    projects: Mapped[list["Project"]] = relationship(
        back_populates="tenant", cascade="all, delete-orphan"
    )


class Project(Base):
    """專案 (projects)。一個租戶可有多個工程專案。"""

    __tablename__ = "projects"

    project_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(50), ForeignKey("tenants.tenant_id"), nullable=False
    )
    project_name: Mapped[str] = mapped_column(String(255), nullable=False)
    region: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'TW'")
    )
    # --- Batch 3 FEAT-2：真實日期 + 工作日曆 (real dates + working calendar) ---
    # start_date：開工日期 (NULL = 未設定，僅以相對天數呈現)。
    # work_days ：7 字元字串，週一..週日，'1'=工作日 (營造預設含週六 '1111110')。
    start_date: Mapped[date | None] = mapped_column(Date)
    work_days: Mapped[str] = mapped_column(
        String(7), nullable=False, server_default=text("'1111110'")
    )
    # --- Batch 3 FEAT-3：樂觀併發控制 (optimistic concurrency) ---
    # 每次排程重算 (recompute) / 專案更新時 +1；客戶端帶 expected_version 比對。
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    # --- Batch 3 FEAT-4：軟刪除 / 回收桶 (soft delete / recycle bin) ---
    # deleted_at 非 NULL = 已進回收桶；所有讀取路徑須過濾 deleted_at IS NULL。
    deleted_at: Mapped[datetime | None] = mapped_column(TIMESTAMP_TZ)
    deleted_by: Mapped[str | None] = mapped_column(String(150))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP_TZ, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP_TZ, server_default=func.now()
    )

    tenant: Mapped["Tenant"] = relationship(back_populates="projects")
    tasks: Mapped[list["Task"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )


class Task(Base):
    """任務 (tasks)。CPM 排程之節點，含正/反向計算結果欄位。"""

    __tablename__ = "tasks"
    __table_args__ = (
        UniqueConstraint("project_id", "task_id", name="uq_tasks_project_task"),
        CheckConstraint("duration >= 0", name="ck_tasks_duration_nonneg"),
    )

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("projects.project_id", ondelete="CASCADE"),
        nullable=False,
    )
    tenant_id: Mapped[str] = mapped_column(String(50), nullable=False)
    task_id: Mapped[str] = mapped_column(String(100), nullable=False)
    task_name: Mapped[str] = mapped_column(
        String(255), nullable=False, server_default=text("''")
    )
    duration: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'PENDING'")
    )
    # --- CPM 計算結果 ---
    es: Mapped[int] = mapped_column(Integer, server_default=text("0"))  # 最早開始
    ef: Mapped[int] = mapped_column(Integer, server_default=text("0"))  # 最早完成
    ls: Mapped[int] = mapped_column(Integer, server_default=text("0"))  # 最晚開始
    lf: Mapped[int] = mapped_column(Integer, server_default=text("0"))  # 最晚完成
    float_time: Mapped[int] = mapped_column(
        Integer, server_default=text("0")
    )  # 寬裕時間 / 總時差
    is_critical: Mapped[bool] = mapped_column(
        Boolean, server_default=text("false")
    )  # 是否位於要徑 / 關鍵路徑
    # 每任務資源需求 (resource_demands)，例：{"crane": 1, "manpower": 15}。
    # 供資源撫平 (RCS / resource leveling) 引擎與 Gantt 視覺化使用；NULL 表未設定。
    # 採可攜 JSON 型別：PostgreSQL -> JSONB、sqlite -> JSON。
    resource_demands: Mapped[dict | None] = mapped_column(JSON_PORTABLE)
    # --- Batch 5 FEAT-1：WBS 階層歸屬 ---
    # 所屬 WBS 節點代碼 (對應 wbs_nodes.wbs_code)；刻意不設 FK，容許匯入懸空
    # (前端歸類為「未分類」)。NULL = 未分類。
    wbs_code: Mapped[str | None] = mapped_column(String(60))
    # --- Batch 5 FEAT-2：活動限制 (P6-style constraints) ---
    # constraint_type/constraint_day 皆為 NULL = 不受限 (今日行為不變)。
    # constraint_type ∈ {SNET, SNLT, FNET, FNLT, MSO, MFO}。
    constraint_type: Mapped[str | None] = mapped_column(String(10))
    # 限制日 (工作日 offset，與 es/ef 同軸)。
    constraint_day: Mapped[int | None] = mapped_column(Integer)
    # 限制衝突 (float_time < 0)；隨 CPM 重算持久化 (與 is_critical 同模式)，
    # 使讀取路徑無須每次重算即可得知。
    constraint_violated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP_TZ, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP_TZ, server_default=func.now()
    )

    project: Mapped["Project"] = relationship(back_populates="tasks")


class TaskDependency(Base):
    """任務相依 (task_dependencies)。task_id 與 predecessor_task_id 之約束關係。

    Batch 3 FEAT-1：新增依賴型態 dep_type (FS/SS/FF/SF) 與 lag_days (延遲天數，
    可為負 = 提前 lead)。預設 FS + 0 => 舊版「完成-開始」語義完全向後相容。
    """

    __tablename__ = "task_dependencies"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "task_id",
            "predecessor_task_id",
            name="uq_task_deps_project_task_pred",
        ),
    )

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(50), nullable=False)
    task_id: Mapped[str] = mapped_column(String(100), nullable=False)
    predecessor_task_id: Mapped[str] = mapped_column(String(100), nullable=False)
    # 依賴型態：FS (完成-開始) / SS (開始-開始) / FF (完成-完成) / SF (開始-完成)。
    dep_type: Mapped[str] = mapped_column(
        String(2), nullable=False, server_default=text("'FS'")
    )
    # 延遲天數 (lag)；負值表示提前 (lead)。
    lag_days: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )


class ProjectHoliday(Base):
    """專案假日 (project_holidays)。Batch 3 FEAT-2。

    每專案的例外停工日 (國定假日 / 颱風假等)；搭配 projects.work_days 工作日曆
    將 CPM 的「工作日 offset」換算為真實日期 (app/core/workcal.py)。
    受 RLS 保護 (與 tasks / projects 一致)，故置於 public schema。
    (project_id, holiday_date) 唯一 —— PUT /projects/{pid}/holidays 以替換式 upsert。
    """

    __tablename__ = "project_holidays"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "holiday_date", name="uq_project_holidays_project_date"
        ),
    )

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("projects.project_id", ondelete="CASCADE"),
        nullable=False,
    )
    tenant_id: Mapped[str] = mapped_column(String(50), nullable=False)
    holiday_date: Mapped[date] = mapped_column(Date, nullable=False)
    name: Mapped[str | None] = mapped_column(String(120), server_default=text("''"))


class WbsNode(Base):
    """WBS 節點 (wbs_nodes)。Batch 5 FEAT-1。

    每專案的工作分解結構 (Work Breakdown Structure)；扁平表 (parent_code 自
    參照上層節點代碼)，前端組樹 (buildWbsTree)。PUT /projects/{pid}/wbs 以
    整批替換式 upsert 維護 (驗證代碼唯一、parent_code 需存在於同批清單或為
    NULL、不得成環)。tasks.wbs_code 為選填參照，刻意不設 FK —— 容許匯入時
    懸空，前端歸類為「未分類」。
    受 RLS 保護 (與 tasks / projects 一致)，故置於 public schema。
    (project_id, wbs_code) 唯一。
    """

    __tablename__ = "wbs_nodes"
    __table_args__ = (
        UniqueConstraint("project_id", "wbs_code", name="uq_wbs_nodes_project_code"),
    )

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("projects.project_id", ondelete="CASCADE"),
        nullable=False,
    )
    tenant_id: Mapped[str] = mapped_column(String(50), nullable=False)
    wbs_code: Mapped[str] = mapped_column(String(60), nullable=False)
    name: Mapped[str] = mapped_column(
        String(255), nullable=False, server_default=text("''")
    )
    parent_code: Mapped[str | None] = mapped_column(String(60))
    sort_order: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )


class ProjectResourceLimit(Base):
    """專案資源上限 (project_resource_limits)。

    每個專案對每種資源 (resource_type，例：crane / manpower) 設定可用上限
    (max_capacity)，供資源撫平 (resource leveling) 偵測逐日超載。
    受 RLS 保護 (與 tasks / projects 一致)，故置於 public schema。

    Pro Batch D (FEATURE D1)：新增 unit_cost / category 兩個向下相容欄位
    (皆有預設值)，供成本負載 (cost loading) 計算引擎使用；不影響既有的
    資源撫平 (resource leveling) 邏輯。
    """

    __tablename__ = "project_resource_limits"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "resource_type", name="uq_resource_limit_project_type"
        ),
        CheckConstraint("max_capacity >= 0", name="ck_resource_limit_capacity_nonneg"),
    )

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("projects.project_id", ondelete="CASCADE"),
        nullable=False,
    )
    tenant_id: Mapped[str] = mapped_column(String(50), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(50), nullable=False)
    max_capacity: Mapped[int] = mapped_column(Integer, nullable=False)
    # Pro Batch D (FEATURE D1)：每單位資源每工作日的成本 (供成本負載計算)。
    unit_cost: Mapped[float] = mapped_column(
        Float, nullable=False, server_default=text("0")
    )
    # Pro Batch D (FEATURE D1)：資源類別 labor / equipment / material / subcontract。
    category: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'labor'")
    )


class ResourceCalendar(Base):
    """單一資源之工作日曆 (resource_calendars)。Pro Batch D (FEATURE D3)。

    每專案對每種資源 (resource_type) 可設定專屬的工作日型態 (work_days，
    7 碼 週一..週日 '1'=工作日)，供資源撫平 (resource leveling) 計算逐日
    可用產能 (availability)：該資源在非其工作日時，當日可用產能視為 0。
    未設定日曆的資源，退回專案層級的 max_capacity 純量上限 (向下相容)。
    受 RLS 保護 (與 tasks / projects 一致)，故置於 public schema。
    """

    __tablename__ = "resource_calendars"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "resource_type", name="uq_resource_calendar_project_type"
        ),
    )

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("projects.project_id", ondelete="CASCADE"),
        nullable=False,
    )
    tenant_id: Mapped[str] = mapped_column(String(50), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(50), nullable=False)
    work_days: Mapped[str] = mapped_column(
        String(7), nullable=False, server_default=text("'1111110'")
    )


class TenantResource(Base):
    """租戶層級 (enterprise) 資源池 (tenant_resources)。Pro Batch E (FEATURE E1)。

    與 project_resource_limits 不同：本表以 tenant_id 為範圍 (而非單一專案)，
    供跨專案的投資組合資源分配 (portfolio resource allocation) 彙總使用。
    受 RLS 保護 (與 project_resource_limits 一致)，故置於 public schema。
    (tenant_id, resource_type) 唯一 —— PUT /resources/pool 以此 upsert。
    """

    __tablename__ = "tenant_resources"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "resource_type", name="uq_tenant_resources_tenant_type"
        ),
        Index("idx_tenant_resources_tenant", "tenant_id"),
    )

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(50), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(50), nullable=False)
    name: Mapped[str] = mapped_column(
        String(120), nullable=False, server_default=text("''")
    )
    category: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'labor'")
    )
    capacity: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    unit_cost: Mapped[float] = mapped_column(
        Float, nullable=False, server_default=text("0")
    )
    work_days: Mapped[str] = mapped_column(
        String(7), nullable=False, server_default=text("'1111100'")
    )


class ResourceCalendarHoliday(Base):
    """單一資源之工作日曆例外假日 (resource_calendar_holidays)。Pro Batch E (FEATURE E2)。

    補完 Batch D 的 resource_calendars：除了「該資源的週工作日型態」
    (work_days) 之外，另可設定該資源專屬的例外停工日 (例：吊車保養日)，
    與專案層級的 project_holidays 語義相同，但範圍限定於單一資源。
    受 RLS 保護 (與 resource_calendars 一致)，故置於 public schema。
    (project_id, resource_type, holiday_date) 唯一。
    """

    __tablename__ = "resource_calendar_holidays"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "resource_type", "holiday_date",
            name="uq_resource_cal_holidays_project_type_date",
        ),
        Index("idx_resource_cal_holidays_project", "project_id"),
    )

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("projects.project_id", ondelete="CASCADE"),
        nullable=False,
    )
    tenant_id: Mapped[str] = mapped_column(String(50), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(50), nullable=False)
    holiday_date: Mapped[date] = mapped_column(Date, nullable=False)
    name: Mapped[str] = mapped_column(
        String(120), nullable=False, server_default=text("''")
    )


class TaskRiskParameter(Base):
    """任務風險參數 (task_risk_parameters)。

    每個任務的三點估計 (PERT)：樂觀 / 最可能 / 悲觀工期，供蒙地卡羅模擬
    (Monte Carlo) 抽樣使用；模擬後將要徑機率 (criticality_index) 回寫此表。
    受 RLS 保護 (與 tasks / projects 一致)，故置於 public schema。
    """

    __tablename__ = "task_risk_parameters"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "task_id", name="uq_risk_param_project_task"
        ),
        CheckConstraint(
            "optimistic_duration >= 0", name="ck_risk_param_optimistic_nonneg"
        ),
    )

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("projects.project_id", ondelete="CASCADE"),
        nullable=False,
    )
    tenant_id: Mapped[str] = mapped_column(String(50), nullable=False)
    task_id: Mapped[str] = mapped_column(String(100), nullable=False)
    optimistic_duration: Mapped[int] = mapped_column(Integer, nullable=False)
    most_likely_duration: Mapped[int] = mapped_column(Integer, nullable=False)
    pessimistic_duration: Mapped[int] = mapped_column(Integer, nullable=False)
    # 要徑機率 (criticality index)：模擬中該任務落在要徑的比例 [0,1]。
    criticality_index: Mapped[float] = mapped_column(
        Float, nullable=False, server_default=text("0.0")
    )


# ---------------------------------------------------------------------------
# public schema —— Phase 9 進度追蹤 / 實獲值管理 (EVM) 表 (受 RLS 保護)
# ---------------------------------------------------------------------------
class TaskProgress(Base):
    """任務進度 (task_progress)。

    每個任務的進度與成本實況，供實獲值管理 (EVM / Earned Value Management) 計算：
      budget          預算 (BAC 的組成；計畫值 PV / 實獲值 EV 之基準)
      percent_complete完成百分比 [0,100] (EV = budget * pct/100)
      actual_cost     實際成本 (AC)
      actual_start_day/actual_finish_day  實際起訖 (相對於專案第 0 天)
    受 RLS 保護 (與 tasks / projects 一致)，故置於 public schema。
    (project_id, task_id) 唯一 —— 每任務一列，PUT 以此 upsert。
    """

    __tablename__ = "task_progress"
    __table_args__ = (
        UniqueConstraint("project_id", "task_id", name="uq_task_progress_project_task"),
        CheckConstraint(
            "percent_complete BETWEEN 0 AND 100",
            name="ck_task_progress_percent_range",
        ),
    )

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("projects.project_id", ondelete="CASCADE"),
        nullable=False,
    )
    tenant_id: Mapped[str] = mapped_column(String(50), nullable=False)
    task_id: Mapped[str] = mapped_column(String(100), nullable=False)
    budget: Mapped[float] = mapped_column(
        Float, nullable=False, server_default=text("0")
    )
    percent_complete: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    actual_cost: Mapped[float] = mapped_column(
        Float, nullable=False, server_default=text("0")
    )
    actual_start_day: Mapped[int | None] = mapped_column(Integer)
    actual_finish_day: Mapped[int | None] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP_TZ, server_default=func.now()
    )


class ProjectBaseline(Base):
    """專案基準線 (project_baselines)。

    某時點的排程 + 預算快照 (snapshot)，作為 EVM 的計畫值 (PV) 基準。
    snapshot JSON 形狀：
      {"project_duration": int,
       "tasks": [{"task_id": str, "es": int, "ef": int,
                  "duration": int, "budget": float}, ...]}
    允許多條基準線；以「最新者」(最大 created_at / 最大 id) 為作用中基準。
    受 RLS 保護 (與 tasks / projects 一致)，故置於 public schema。
    """

    __tablename__ = "project_baselines"

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("projects.project_id", ondelete="CASCADE"),
        nullable=False,
    )
    tenant_id: Mapped[str] = mapped_column(String(50), nullable=False)
    name: Mapped[str] = mapped_column(
        String(120), nullable=False, server_default=text("'baseline'")
    )
    # 排程 + 預算快照 (可攜 JSON：PostgreSQL -> JSONB、sqlite -> JSON)。
    snapshot: Mapped[dict] = mapped_column(JSON_PORTABLE, nullable=False)
    # --- Batch 5 FEAT-3：多組具名基準線 ---
    # 同專案僅一條為 TRUE (應用層原子維護)；全 FALSE 時 (剛遷移的既有列)
    # 退回「最新」為作用中基準，向後相容。
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP_TZ, server_default=func.now()
    )


# ---------------------------------------------------------------------------
# public schema —— Pro Batch C (FEATURE 1) 任務照片附件 (受 RLS 保護)
# ---------------------------------------------------------------------------
class TaskPhoto(Base):
    """任務照片附件 (task_photos)。行動端現場回報 (mobile field reporting)。

    實際檔案內容存於磁碟 (settings.upload_dir)，本表僅存中繼資料；
    stored_name 為伺服器產生的 uuid4hex 檔名 (絕不取自使用者輸入)，
    content_type 由上傳時的 magic bytes 判定 (見 app/routers/photos.py)。
    受 RLS 保護 (與 tasks / projects 一致)，故置於 public schema。
    """

    __tablename__ = "task_photos"
    __table_args__ = (
        UniqueConstraint("stored_name", name="uq_task_photos_stored_name"),
        Index("ix_task_photos_project_task", "project_id", "task_id"),
    )

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("projects.project_id", ondelete="CASCADE"),
        nullable=False,
    )
    tenant_id: Mapped[str] = mapped_column(String(50), nullable=False)
    task_id: Mapped[str] = mapped_column(String(100), nullable=False)
    stored_name: Mapped[str] = mapped_column(String(80), nullable=False)
    original_name: Mapped[str] = mapped_column(
        String(255), nullable=False, server_default=text("''")
    )
    content_type: Mapped[str] = mapped_column(String(50), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    note: Mapped[str] = mapped_column(
        String(500), nullable=False, server_default=text("''")
    )
    uploaded_by: Mapped[str] = mapped_column(
        String(150), nullable=False, server_default=text("''")
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP_TZ, server_default=func.now()
    )


# ---------------------------------------------------------------------------
# public schema —— 應用使用者 (app_users)；登入用,「不」受 RLS 保護
# ---------------------------------------------------------------------------
class AppUser(Base):
    """應用登入帳號 (app_users)。

    重要：登入 (POST /auth/login) 是在「尚未建立任何 tenant 情境」之前，
    依 username 查詢此表 —— 因此 app_users 「絕不」可置於 RLS 之下，否則
    set_config('app.current_tenant') 尚未設定時查詢會被 RLS 過濾為空。
    db/init.sql 不對此表 ENABLE ROW LEVEL SECURITY。
    """

    __tablename__ = "app_users"

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(
        String(50), ForeignKey("tenants.tenant_id"), nullable=False
    )
    username: Mapped[str] = mapped_column(String(150), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    region: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'TW'")
    )
    # 角色 (role)：admin > editor > viewer。預設 admin —— 既有舊帳號 (未帶 role)
    # 升級後仍享完整權限，向後相容 (既有測試以 admin@tw / header-mode 皆視為 admin)。
    role: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'admin'")
    )
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP_TZ, server_default=func.now()
    )


class AuditLog(Base):
    """稽核日誌 (audit_log)。

    記錄使用者管理等敏感操作 (建立 / 更新 / 刪除帳號)，供追溯與合規。
    受 RLS 保護 (與 tasks / projects 一致；以 tenant_id 隔離)，故置於 public schema。
    detail 採可攜 JSON 型別 (PostgreSQL -> JSONB、sqlite -> JSON)。
    """

    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_log_tenant", "tenant_id"),)

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(50), nullable=False)
    actor: Mapped[str | None] = mapped_column(String(150))
    action: Mapped[str] = mapped_column(String(80), nullable=False)
    detail: Mapped[dict | None] = mapped_column(JSON_PORTABLE)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP_TZ, server_default=func.now()
    )


# ---------------------------------------------------------------------------
# erp_integration schema —— 服務管理 (無 RLS；程式以 tenant_id 過濾)
# ---------------------------------------------------------------------------
class ErpConfig(Base):
    """租戶 ERP 設定 (erp_integration.tenant_erp_config)。

    erp_type 例：SAP / DINGXIN_TW (鼎新) / YONYOU_CN (用友)。
    """

    __tablename__ = "tenant_erp_config"
    __table_args__ = {"schema": "erp_integration"}

    tenant_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    erp_type: Mapped[str] = mapped_column(String(20), nullable=False)
    api_endpoint: Mapped[str | None] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))


class TaskMapping(Base):
    """任務對應 (erp_integration.task_mapping)。

    將排程任務 (schedule_task_id) 對應到 ERP 之 WBS 代碼 (erp_wbs_code)。
    """

    __tablename__ = "task_mapping"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "schedule_task_id", name="uq_task_mapping_tenant_task"
        ),
        {"schema": "erp_integration"},
    )

    mapping_id: Mapped[int] = mapped_column(
        BIGINT_PK, primary_key=True, autoincrement=True
    )
    tenant_id: Mapped[str] = mapped_column(String(50), nullable=False)
    schedule_task_id: Mapped[str] = mapped_column(String(100), nullable=False)
    erp_wbs_code: Mapped[str] = mapped_column(String(100), nullable=False)


class NotificationConfig(Base):
    """租戶通知憑證 (erp_integration.tenant_notification_config)。

    每租戶自有的 LINE token / 釘釘 / 企業微信 webhook；欄位留空 => 投遞時
    退回全域 settings。與 tenant_erp_config 同理：服務管理資料、無 RLS，
    由程式碼以 tenant_id 過濾 (跨租戶 worker 需可掃描)。
    """

    __tablename__ = "tenant_notification_config"
    __table_args__ = {"schema": "erp_integration"}

    tenant_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    line_token: Mapped[str | None] = mapped_column(String(255))
    line_target_id: Mapped[str | None] = mapped_column(String(100))
    dingtalk_webhook: Mapped[str | None] = mapped_column(String(255))
    wecom_webhook: Mapped[str | None] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))


class NotificationOutbox(Base):
    """通知 outbox (erp_integration.notification_outbox)。

    交易性 outbox：API 於業務交易內寫入 PENDING 列 (與業務資料同交易、原子提交)，
    worker (deliver_outbox_once) 週期掃描並實際投遞。
    狀態：PENDING -> SUCCESS / DEAD (retry_count >= 上限)。
    channel：LINE / DINGTALK / WECOM / LOG (LOG 僅記錄日誌 => SUCCESS)。
    """

    __tablename__ = "notification_outbox"
    __table_args__ = (
        Index("ix_notification_outbox_status_retry", "status", "retry_count"),
        {"schema": "erp_integration"},
    )

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(50), nullable=False)
    region: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'TW'")
    )
    channel: Mapped[str] = mapped_column(String(20), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'PENDING'")
    )
    retry_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP_TZ, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP_TZ, server_default=func.now()
    )


class SyncEvent(Base):
    """同步事件記錄 (erp_integration.sync_event_log)。

    拋轉佇列：API 寫入 PENDING 列，worker 跨租戶掃描後逐筆推送至 ERP。
    狀態：PENDING -> SUCCESS / DEAD (retry_count >= 上限)。
    """

    __tablename__ = "sync_event_log"
    __table_args__ = (
        Index("ix_sync_event_log_status_retry", "status", "retry_count"),
        # Batch 4 (PERF-3)：dashboard / exports 風險事件統計的複合索引
        # (WHERE tenant_id+sync_type+status，GROUP BY project_id)。
        Index(
            "ix_sync_event_log_tenant_type_status",
            "tenant_id",
            "sync_type",
            "status",
        ),
        {"schema": "erp_integration"},
    )

    event_id: Mapped[uuid.UUID] = mapped_column(
        # 可攜 UUID：PostgreSQL 用原生 uuid、sqlite 用 CHAR(32)。
        # 以 Python 端 default=uuid.uuid4 產生主鍵 (移除 PG 專屬的
        # server_default gen_random_uuid()；PostgreSQL 正式環境的 DEFAULT
        # 仍由 db/init.sql 提供，兩者並存無衝突)。
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    tenant_id: Mapped[str] = mapped_column(String(50), nullable=False)
    mapping_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("erp_integration.task_mapping.mapping_id")
    )
    # Batch 4 (PERF-3)：事件所屬專案。寫入端 (erp router / risk_listener) 直接
    # 設定；舊列由遷移 0003 / main.py sqlite ALTER 自 payload 回填。可為 NULL
    # (例：COST_PULL 為租戶層級事件，無單一專案)。
    project_id: Mapped[str | None] = mapped_column(String(64))
    sync_type: Mapped[str] = mapped_column(String(50), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON_PORTABLE, nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'PENDING'")
    )
    retry_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP_TZ, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP_TZ, server_default=func.now()
    )
