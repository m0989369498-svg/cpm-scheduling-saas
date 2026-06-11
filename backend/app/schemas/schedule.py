"""排程相關的 Pydantic v2 結構定義（Schemas / DTO）。

本檔案是整個系統的契約來源（contract source）：
- API 請求 / 回應的驗證與序列化
- CPM 引擎的輸入（TaskDefinition）與輸出（TaskResult）

注意：所有名稱與欄位必須與 SPEC 完全一致，因為前端、ORM、ERP ACL
與其他 agent 產生的檔案都依賴這些契約。
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict, Field, field_validator

# 系統共用的狀態值（status）：
#   PENDING      待處理
#   IN_PROGRESS  進行中
#   COMPLETED    已完成
#   DELAYED      延遲 / 延期
STATUS_VALUES = ("PENDING", "IN_PROGRESS", "COMPLETED", "DELAYED")

# 相依型態（dependency types）：
#   FS  完成-開始（Finish-to-Start，傳統 CPM 預設）
#   SS  開始-開始（Start-to-Start）
#   FF  完成-完成（Finish-to-Finish）
#   SF  開始-完成（Start-to-Finish）
DEP_TYPE_VALUES = ("FS", "SS", "FF", "SF")


class DependencyLink(BaseModel):
    """任務相依連結（dependency link）。

    描述「前置任務 → 本任務」的單一排程約束（scheduling constraint）：
      predecessor_task_id  前置任務的 task_id
      dep_type             相依型態（FS / SS / FF / SF）
      lag_days             延時天數（lag）；可為負值（lead，提前/領先）
    """

    predecessor_task_id: str
    dep_type: str = "FS"
    lag_days: int = 0

    @field_validator("dep_type")
    @classmethod
    def _validate_dep_type(cls, value: str) -> str:
        """正規化並驗證相依型態必須為 FS / SS / FF / SF。"""
        normalized = value.strip().upper()
        if normalized not in DEP_TYPE_VALUES:
            raise ValueError(
                f"不支援的相依型態（unsupported dep_type）：{value}，"
                f"允許值：{', '.join(DEP_TYPE_VALUES)}"
            )
        return normalized


class TaskDefinition(BaseModel):
    """單一任務（活動）的定義 —— CPM 引擎的輸入單元。

    predecessors 為前置任務的 task_id 清單，描述有向相依關係（DAG）。
    links 為帶型態與延時的相依連結（dependency links）；為 None 時
    由 predecessors 推導為傳統 FS + lag 0（向下相容 / backward compatible）。
    """

    task_id: str
    task_name: str = ""
    duration: int = Field(ge=0)
    predecessors: list[str] = Field(default_factory=list)
    status: str = "PENDING"
    # 相依連結（選填）：含 dep_type（FS/SS/FF/SF）與 lag_days；
    # None 表示沿用 predecessors（視為 FS + 0）。
    links: list[DependencyLink] | None = None


class TaskResult(TaskDefinition):
    """CPM 計算後的任務結果。

    在 TaskDefinition 之上附加要徑法（Critical Path Method）計算欄位：
      es / ef  最早開始 / 最早完成（Early Start / Early Finish）
      ls / lf  最晚開始 / 最晚完成（Late Start / Late Finish）
      float_time  寬裕時間 / 總時差（Total Float）= ls - es
      is_critical 是否位於要徑（float_time == 0）
    """

    es: int = 0
    ef: int = 0
    ls: int = 0
    lf: int = 0
    float_time: int = 0
    is_critical: bool = False
    # 每任務資源需求（resource_demands），例：{"crane": 1, "manpower": 15}。
    # 供資源撫平（resource leveling）與 Gantt 視覺化使用；None 表未設定。
    resource_demands: dict[str, int] | None = None


class ProjectBase(BaseModel):
    """專案基礎欄位。"""

    project_name: str
    region: str = "TW"
    # FEAT-2 實際日期 + 工作日曆：
    #   start_date 開工日期（None = 未設定，僅以相對日偏移呈現）。
    #   work_days  7 碼字串（週一..週日 Mon..Sun），'1'=工作日；
    #              營造業預設 '1111110'（週一至週六上工、週日休）。
    start_date: date | None = None
    work_days: str = "1111110"

    @field_validator("work_days")
    @classmethod
    def _validate_work_days(cls, value: str) -> str:
        """work_days 必須為 7 碼、僅含 0/1（週一..週日）。"""
        if len(value) != 7 or any(ch not in "01" for ch in value):
            raise ValueError(
                "work_days 必須為 7 碼 0/1 字串（週一..週日 Mon..Sun）"
            )
        return value


class ProjectCreate(ProjectBase):
    """建立專案請求。

    project_id 若為 None 則由後端產生；schedule_data 為初始任務清單。
    """

    project_id: str | None = None
    schedule_data: list[TaskDefinition] = Field(default_factory=list)


class ProjectUpdate(ProjectBase):
    """更新專案中繼資料請求（PUT /projects/{pid}）。

    expected_version（FEAT-3 樂觀併發）：提供時須等於當前 project.version，
    否則 409 版本衝突；省略（None）則不檢查（向下相容）。
    """

    expected_version: int | None = None


class ProjectOut(ProjectBase):
    """專案完整輸出（含 CPM 結果）。"""

    model_config = ConfigDict(from_attributes=True)

    project_id: str
    tenant_id: str
    project_duration: int = 0
    tasks: list[TaskResult] = Field(default_factory=list)
    # FEAT-3 樂觀併發：專案版本（每次重算 / 中繼資料更新 +1）。
    version: int = 0
    # FEAT-2：偏移 0..project_duration 對應的 ISO 日期清單；
    # 僅在 start_date 已設定時提供（None = 未設定開工日期）。
    day_dates: list[str] | None = None


class HolidayEntry(BaseModel):
    """專案例外假日（project_holidays）單筆項目。"""

    holiday_date: date
    name: str = ""


class ProjectSummary(BaseModel):
    """專案摘要（清單檢視用）。"""

    project_id: str
    project_name: str
    region: str
    tenant_id: str
    task_count: int
    project_duration: int


class TaskCreate(BaseModel):
    """新增任務請求。"""

    task_id: str
    task_name: str = ""
    duration: int = Field(ge=0)
    predecessors: list[str] = Field(default_factory=list)
    status: str = "PENDING"
    # 每任務資源需求（選填），例：{"crane": 1, "manpower": 15}。
    resource_demands: dict[str, int] | None = None
    # 相依連結（選填）：提供時 predecessors 將被忽略並由 links 重新推導。
    links: list[DependencyLink] | None = None
    # FEAT-3 樂觀併發：提供時須等於當前 project.version，否則 409。
    expected_version: int | None = None


class TaskDurationUpdate(BaseModel):
    """更新工期請求（拖曳重算路徑使用）。"""

    duration: int = Field(ge=0)
    # FEAT-3 樂觀併發：提供時須等於當前 project.version，否則 409。
    expected_version: int | None = None


class TaskUpdate(BaseModel):
    """部分更新任務請求；未提供的欄位（None）表示不變更。"""

    task_name: str | None = None
    duration: int | None = Field(default=None, ge=0)
    status: str | None = None
    predecessors: list[str] | None = None
    # 每任務資源需求（選填）；None 表示不變更。
    resource_demands: dict[str, int] | None = None
    # 相依連結（選填）：提供時 predecessors 將被忽略並由 links 重新推導；
    # None 表示不變更相依。
    links: list[DependencyLink] | None = None
    # FEAT-3 樂觀併發：提供時須等於當前 project.version，否則 409。
    expected_version: int | None = None


class ErpSyncRequest(BaseModel):
    """ERP 拋轉請求。"""

    sync_type: str = "SCHEDULE_PUSH"
