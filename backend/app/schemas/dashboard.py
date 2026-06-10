"""儀表板 (Dashboard) 相關的 Pydantic v2 結構定義。

涵蓋 Phase 10 的投資組合儀表板 (portfolio dashboard)：
  - ProjectKpi       單一專案的關鍵指標 (KPI) 摘要
  - DashboardTotals  跨專案彙總總計
  - DashboardOut     /dashboard 端點的回應 (projects + totals)

設計重點：
  * 純結構定義，不接觸資料庫，亦不依賴 ORM。
  * spi / cpi 於「無基準線」或「分母為 0」時為 None (前端據此顯示 N/A)。
  * 名稱與欄位須與 SPEC 完全一致 (前端 store / DashboardView 依賴這些契約)。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ProjectKpi(BaseModel):
    """單一專案的關鍵指標 (KPI) 摘要 (供投資組合儀表板使用)。

    project_id           專案代碼。
    project_name         專案名稱。
    region               區域 (TW / CN)。
    task_count           任務數。
    project_duration     專案總工期 (= CPM 計算後最大 ef)。
    critical_count       要徑任務數 (is_critical 為真者)。
    has_baseline         是否已建立基準線 (決定 spi / cpi 是否可計算)。
    spi                  進度績效指標 (Schedule Performance Index)；無基準線或 PV<=0 時為 None。
    cpi                  成本績效指標 (Cost Performance Index)；無基準線或 AC<=0 時為 None。
    pending_risk_events  待處理的風險預警事件數 (sync_event_log RISK_PROVISION / PENDING)。
    """

    project_id: str
    project_name: str
    region: str
    task_count: int = 0
    project_duration: int = 0
    critical_count: int = 0
    has_baseline: bool = False
    spi: float | None = None
    cpi: float | None = None
    pending_risk_events: int = 0


class DashboardTotals(BaseModel):
    """投資組合 (portfolio) 跨專案彙總總計。

    project_count          專案數。
    task_count             所有專案任務總數。
    critical_count         所有專案要徑任務總數。
    baseline_count         已建立基準線的專案數。
    pending_risk_events    待處理風險預警事件總數。
    at_risk_count          進度或成本落後 (SPI<1 或 CPI<1) 的專案數。
    """

    project_count: int = 0
    task_count: int = 0
    critical_count: int = 0
    baseline_count: int = 0
    pending_risk_events: int = 0
    at_risk_count: int = 0


class DashboardOut(BaseModel):
    """/dashboard 端點回應：當前租戶所有專案的 KPI 清單 + 彙總總計。"""

    projects: list[ProjectKpi] = Field(default_factory=list)
    totals: DashboardTotals = Field(default_factory=DashboardTotals)
