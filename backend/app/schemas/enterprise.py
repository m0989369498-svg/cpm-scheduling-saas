"""企業級 (enterprise / tenant-level) 資源相關的 Pydantic v2 結構定義。Pro Batch E (FEATURE E1)。

涵蓋租戶層級資源池 (tenant_resources) 與投資組合資源分配 (portfolio resource
allocation) 兩者的請求 / 回應契約。本檔案為純結構定義，不接觸資料庫。
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class TenantResource(BaseModel):
    """租戶層級資源池的單一資源類別定義 (GET/PUT /resources/pool)。

    與 project_resource_limits (單一專案上限) 不同：capacity 為「租戶整體」
    每日可用上限，供跨專案投資組合資源分配彙總使用。
    """

    resource_type: str
    name: str = ""
    category: str = "labor"
    capacity: int = Field(default=0, ge=0)
    unit_cost: float = Field(default=0.0, ge=0)
    work_days: str = "1111100"

    @field_validator("work_days")
    @classmethod
    def _validate_work_days(cls, value: str) -> str:
        """work_days 必須為 7 碼、僅含 0/1（週一..週日）。"""
        if len(value) != 7 or any(ch not in "01" for ch in value):
            raise ValueError(
                "work_days 必須為 7 碼 0/1 字串（週一..週日 Mon..Sun）"
            )
        return value


class ResourceAllocationRow(BaseModel):
    """單一資源類別的投資組合週別分配情形。

    by_week  {ISO 週標籤 (YYYY-Www): 該週峰值日需求量}；僅列出有需求的週。
    peak     該資源於所有週中的最大值 (即 max(by_week.values())；無需求為 0)。
    over_weeks  峰值需求 > capacity 的週標籤清單 (依字串排序，恰為時間序)。
    """

    resource_type: str
    name: str = ""
    category: str = "labor"
    capacity: int = 0
    unit_cost: float = 0.0
    by_week: dict[str, int] = Field(default_factory=dict)
    peak: int = 0
    over_weeks: list[str] = Field(default_factory=list)


class ResourceAllocationResult(BaseModel):
    """投資組合資源分配結果 (GET /resources/allocation)。

    weeks               所有列中出現過的 ISO 週標籤聯集 (排序)。
    resources           各資源類別的週別分配列 (依 resource_type 排序)。
    unscheduled_projects  有資源需求但未設定 start_date 的專案 id 清單 (無法排入時間軸)。
    warnings            提示訊息 (例如 unscheduled_projects 非空時的說明)。
    """

    weeks: list[str] = Field(default_factory=list)
    resources: list[ResourceAllocationRow] = Field(default_factory=list)
    unscheduled_projects: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
