"""進階分析相關的 Pydantic v2 結構定義（Schemas / DTO）。

涵蓋 Phase 8 的兩大功能：
  - 資源撫平（Resource-Constrained Scheduling, RCS）：
      ResourceLimit / ResourceConfig / DayLoad / LevelingResult
  - 蒙地卡羅工期風險模擬（Monte Carlo / PERT-Beta）：
      RiskParam / SCurvePoint / CriticalityItem /
      SimulationRequest / SimulationResult

本檔案為純結構定義，不接觸資料庫，亦不依賴 ORM。
名稱與欄位須與 SPEC 完全一致，因前端、路由與引擎皆依賴這些契約。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.schemas.schedule import TaskResult

# ---------------------------------------------------------------------------
# 資源撫平（Resource Leveling）相關結構
# ---------------------------------------------------------------------------


class ResourceLimit(BaseModel):
    """單一資源類別的上限定義。

    resource_type 例如 "crane"（吊車）、"manpower"（人力）。
    max_capacity 為每日可用的最大數量（>= 0）。
    """

    resource_type: str
    max_capacity: int = Field(ge=0)


class ResourceConfig(BaseModel):
    """專案資源組態（讀 / 寫共用）。

    limits   各資源類別的每日上限清單。
    demands  每個任務的資源需求；外層 key 為 task_id，
             內層為 {resource_type: amount}，例如
             {"T-01": {"crane": 1, "manpower": 10}}。
    """

    limits: list[ResourceLimit] = Field(default_factory=list)
    demands: dict[str, dict[str, int]] = Field(default_factory=dict)


class DayLoad(BaseModel):
    """資源撫平時間軸上單一工作日的負載快照。

    day    工作日索引（0-based，對應 CPM 的 es..ef-1）。
    loads  當日各資源的總需求量 {resource_type: amount}。
    over   當日是否有任一資源超過上限。
    """

    day: int
    loads: dict[str, int] = Field(default_factory=dict)
    over: bool = False


class LevelingResult(BaseModel):
    """資源撫平結果。

    original_duration  撫平前的專案總工期。
    leveled_duration   撫平後的專案總工期。
    extended           工期是否因撫平而展延（leveled > original）。
    tasks              撫平後的任務 CPM 結果清單。
    timeline           逐日資源負載時間軸。
    over_capacity_days 撫平後仍超出上限的工作日索引清單。
    unresolved         無法化解衝突（無可移動任務）的 task_id 清單。
    """

    original_duration: int
    leveled_duration: int
    extended: bool
    tasks: list[TaskResult] = Field(default_factory=list)
    timeline: list[DayLoad] = Field(default_factory=list)
    over_capacity_days: list[int] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 蒙地卡羅 / PERT 風險模擬相關結構
# ---------------------------------------------------------------------------


class RiskParam(BaseModel):
    """單一任務的三點估計（three-point estimate）風險參數。

    optimistic_duration   樂觀工期（a）。
    most_likely_duration  最可能工期（m）。
    pessimistic_duration  悲觀工期（b）。
    criticality_index     關鍵性指數（0..1）；模擬後回填，輸入時忽略。
    """

    task_id: str
    optimistic_duration: int = Field(ge=0)
    most_likely_duration: int = Field(ge=0)
    pessimistic_duration: int = Field(ge=0)
    criticality_index: float = 0.0


class SCurvePoint(BaseModel):
    """S 曲線（累積機率曲線）上的一個資料點。

    duration     工期天數。
    probability  專案於此工期（含）內完成的累積機率（0..1）。
    """

    duration: int
    probability: float


class CriticalityItem(BaseModel):
    """單一任務的關鍵性指數（落在要徑上的模擬比例）。"""

    task_id: str
    index: float


class SimulationRequest(BaseModel):
    """蒙地卡羅模擬請求。

    iterations  模擬次數（預設 1000；上限 10000，避免 CPU 資源耗盡攻擊）。
    deadline    合約工期 / 期限（天）；提供時計算準時完成機率。
    """

    iterations: int = Field(default=1000, ge=1, le=10000)
    deadline: int | None = None


class SimulationResult(BaseModel):
    """蒙地卡羅模擬結果。

    iterations            實際執行的模擬次數。
    mean / std            專案工期的平均值與標準差。
    p10 / p50 / p90       工期的第 10 / 50 / 90 百分位數。
    s_curve               累積機率 S 曲線（非遞減，落於 [0, 1]）。
    criticality           各任務關鍵性指數清單。
    deadline              請求帶入的期限（回傳以利前端標示）。
    on_time_probability   準時完成機率（有 deadline 時，否則 None）。
    """

    iterations: int
    mean: float
    std: float
    p10: int
    p50: int
    p90: int
    s_curve: list[SCurvePoint] = Field(default_factory=list)
    criticality: list[CriticalityItem] = Field(default_factory=list)
    deadline: int | None = None
    on_time_probability: float | None = None
