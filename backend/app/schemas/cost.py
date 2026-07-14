"""成本負載（Cost Loading）相關的 Pydantic v2 結構定義（Schemas / DTO）。

Pro Batch D（FEATURE D1）：資源池 + 費率 + 成本負載。

本檔案為純結構定義，不接觸資料庫，亦不依賴 ORM。
名稱與欄位須與 SPEC 完全一致，因前端、路由與引擎皆依賴這些契約。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class CostTaskBreakdown(BaseModel):
    """單一任務的成本明細。

    task_id / task_name / duration  任務基本資訊。
    cost           該任務的總成本（duration * Σ qty*rate）。
    per_resource   該任務各資源類別的成本 {resource_type: cost}。
    """

    task_id: str
    task_name: str = ""
    duration: int = 0
    cost: float = 0.0
    per_resource: dict[str, float] = Field(default_factory=dict)


class CostCurvePoint(BaseModel):
    """成本 S 曲線（cost curve）上的一個資料點。

    day         工作日索引（0-based）。
    cost        該日的花費（各任務成本於 [es, ef) 區間均勻攤銷後加總）。
    cumulative  累積花費（非遞減）。
    """

    day: int
    cost: float = 0.0
    cumulative: float = 0.0


class CostResult(BaseModel):
    """成本負載計算結果。

    total_cost    專案總成本（各任務成本加總）。
    by_resource   各資源類別的成本加總 {resource_type: total}。
    by_category   各資源大類的成本加總 {category: total}。
    by_wbs        各 WBS 節點的成本加總 {wbs_code or '': total}（未分類以空字串為 key）。
    per_task      各任務的成本明細清單。
    cost_curve    成本 S 曲線（day 0..project_duration）。
    """

    total_cost: float = 0.0
    by_resource: dict[str, float] = Field(default_factory=dict)
    by_category: dict[str, float] = Field(default_factory=dict)
    by_wbs: dict[str, float] = Field(default_factory=dict)
    per_task: list[CostTaskBreakdown] = Field(default_factory=list)
    cost_curve: list[CostCurvePoint] = Field(default_factory=list)
