"""實獲值管理（Earned Value Management, EVM）相關的 Pydantic v2 結構定義。

涵蓋 Phase 9 的進度追蹤（progress tracking）與實獲值分析：
  - ProgressEntry     單一任務的進度 / 成本輸入（讀寫共用）
  - BaselineOut       專案基準線（baseline）輸出快照
  - EvmRequest        EVM 計算 / 風險拋轉的請求參數
  - BaselineCreate    建立基準線的請求（選填名稱）
  - PvCurvePoint      計畫價值（PV）S 曲線上的單點
  - EvmTaskBreakdown  各任務的 PV / EV / AC 拆解
  - EvmResult         EVM 計算總結果（PMI 標準指標）

本檔案為純結構定義，不接觸資料庫，亦不依賴 ORM。
名稱與欄位須與 SPEC 完全一致，因前端、路由與引擎皆依賴這些契約。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# 進度輸入 / 基準線（Progress input / Baseline）相關結構
# ---------------------------------------------------------------------------


class ProgressEntry(BaseModel):
    """單一任務的進度與成本（讀 / 寫共用）。

    task_id           任務代碼。
    budget            該任務預算（Budget at Completion 的組成；BAC = sum(budget)）。
    percent_complete  完成百分比（0..100，整數）。
    actual_cost       實際成本（Actual Cost, AC 的組成）。
    actual_start_day  實際開始日（0-based；None 表尚未開始）。
    actual_finish_day 實際完成日（0-based；None 表尚未完成）。
    """

    task_id: str
    budget: float = 0.0
    percent_complete: int = Field(default=0, ge=0, le=100)
    actual_cost: float = 0.0
    actual_start_day: int | None = None
    actual_finish_day: int | None = None


class ProgressTask(BaseModel):
    """基準線快照內的單一任務（snapshot.tasks 的元素）。

    es / ef    基準線記錄的最早開始 / 最早完成（Early Start / Early Finish）。
    duration   工期。
    budget     該任務預算。
    """

    task_id: str
    es: int = 0
    ef: int = 0
    duration: int = 0
    budget: float = 0.0


class BaselineCreate(BaseModel):
    """建立基準線（baseline）請求；name 選填，預設 'baseline'。"""

    name: str = "baseline"


class BaselineOut(BaseModel):
    """專案基準線輸出。

    id                基準線主鍵。
    name              基準線名稱。
    project_duration  建立基準線當下的專案總工期。
    created_at        建立時間（ISO 8601 字串）。
    tasks             基準線任務快照清單（含 es / ef / duration / budget）。
    """

    id: int
    name: str = "baseline"
    project_duration: int = 0
    created_at: str = ""
    tasks: list[ProgressTask] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# EVM 計算（Earned Value）相關結構
# ---------------------------------------------------------------------------


class EvmRequest(BaseModel):
    """EVM 計算 / 風險拋轉請求。

    data_date  資料截止日（status date，0-based）；None 時由端點預設為
               基準線的 project_duration。
    """

    data_date: int | None = None


class PvCurvePoint(BaseModel):
    """計畫價值（Planned Value, PV）累積 S 曲線上的單點。

    day  工作日索引（0..project_duration）。
    pv   截至該日（含）的累積計畫價值。
    """

    day: int
    pv: float


class EvmTaskBreakdown(BaseModel):
    """單一任務的 EVM 拆解。

    task_id          任務代碼。
    budget           任務預算。
    planned_pct      計畫完成百分比（依 data_date 與 es/duration 推算後 ×100，四捨五入）。
    percent_complete 實際完成百分比。
    pv               計畫價值 = budget × 計畫完成比例。
    ev               實獲值（Earned Value）= budget × percent_complete / 100。
    ac               實際成本（Actual Cost）。
    """

    task_id: str
    budget: float = 0.0
    planned_pct: int = 0
    percent_complete: int = 0
    pv: float = 0.0
    ev: float = 0.0
    ac: float = 0.0


class EvmResult(BaseModel):
    """實獲值管理（EVM）計算結果（PMI 標準指標）。

    指標定義：
      bac   完工預算（Budget at Completion）= sum(budget)。
      pv    計畫價值（Planned Value，又稱 BCWS）。
      ev    實獲值（Earned Value，又稱 BCWP）。
      ac    實際成本（Actual Cost，又稱 ACWP）。
      sv    進度差異（Schedule Variance）= EV - PV。
      cv    成本差異（Cost Variance）= EV - AC。
      spi   進度績效指標（Schedule Performance Index）= EV / PV（PV<=0 時為 None）。
      cpi   成本績效指標（Cost Performance Index）= EV / AC（AC<=0 時為 None）。
      eac   完工估計（Estimate at Completion）= BAC / CPI（CPI<=0 時為 None）。
      etc   完工尚需估計（Estimate to Complete）= EAC - AC（EAC 為 None 時為 None）。
      vac   完工差異（Variance at Completion）= BAC - EAC（EAC 為 None 時為 None）。
      tcpi  待完成績效指標（To-Complete Performance Index）
            = (BAC - EV) / (BAC - AC)（分母為 0 時為 None）。
      risk_flagged  是否觸發風險（SPI<0.9 或 CPI<0.9，僅在有定義時判定）。
      pv_curve      PV 累積 S 曲線（day 0..project_duration）。
      per_task      各任務 PV / EV / AC 拆解。
    """

    data_date: int
    bac: float
    pv: float
    ev: float
    ac: float
    sv: float
    cv: float
    spi: float | None = None
    cpi: float | None = None
    eac: float | None = None
    etc: float | None = None
    vac: float | None = None
    tcpi: float | None = None
    risk_flagged: bool = False
    pv_curve: list[PvCurvePoint] = Field(default_factory=list)
    per_task: list[EvmTaskBreakdown] = Field(default_factory=list)
