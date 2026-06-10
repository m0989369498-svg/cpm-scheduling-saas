"""排程相關的 Pydantic v2 結構定義（Schemas / DTO）。

本檔案是整個系統的契約來源（contract source）：
- API 請求 / 回應的驗證與序列化
- CPM 引擎的輸入（TaskDefinition）與輸出（TaskResult）

注意：所有名稱與欄位必須與 SPEC 完全一致，因為前端、ORM、ERP ACL
與其他 agent 產生的檔案都依賴這些契約。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

# 系統共用的狀態值（status）：
#   PENDING      待處理
#   IN_PROGRESS  進行中
#   COMPLETED    已完成
#   DELAYED      延遲 / 延期
STATUS_VALUES = ("PENDING", "IN_PROGRESS", "COMPLETED", "DELAYED")


class TaskDefinition(BaseModel):
    """單一任務（活動）的定義 —— CPM 引擎的輸入單元。

    predecessors 為前置任務的 task_id 清單，描述有向相依關係（DAG）。
    """

    task_id: str
    task_name: str = ""
    duration: int = Field(ge=0)
    predecessors: list[str] = Field(default_factory=list)
    status: str = "PENDING"


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


class ProjectBase(BaseModel):
    """專案基礎欄位。"""

    project_name: str
    region: str = "TW"


class ProjectCreate(ProjectBase):
    """建立專案請求。

    project_id 若為 None 則由後端產生；schedule_data 為初始任務清單。
    """

    project_id: str | None = None
    schedule_data: list[TaskDefinition] = Field(default_factory=list)


class ProjectOut(ProjectBase):
    """專案完整輸出（含 CPM 結果）。"""

    model_config = ConfigDict(from_attributes=True)

    project_id: str
    tenant_id: str
    project_duration: int = 0
    tasks: list[TaskResult] = Field(default_factory=list)


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


class TaskDurationUpdate(BaseModel):
    """更新工期請求（拖曳重算路徑使用）。"""

    duration: int = Field(ge=0)


class TaskUpdate(BaseModel):
    """部分更新任務請求；未提供的欄位（None）表示不變更。"""

    task_name: str | None = None
    duration: int | None = Field(default=None, ge=0)
    status: str | None = None
    predecessors: list[str] | None = None


class ErpSyncRequest(BaseModel):
    """ERP 拋轉請求。"""

    sync_type: str = "SCHEDULE_PUSH"
