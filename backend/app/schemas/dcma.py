"""DCMA 14-point 排程健康評估相關的 Pydantic v2 結構定義（Schemas / DTO）。

Pro Batch D（FEATURE D2）：DCMA 14 點排程品質檢核（旗艦功能）。

本檔案為純結構定義，不接觸資料庫，亦不依賴 ORM。
名稱與欄位須與 SPEC 完全一致，因前端、路由與引擎皆依賴這些契約。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class DcmaCheck(BaseModel):
    """單一 DCMA 檢核項目的結果。

    key / name / name_cn  檢核代碼與雙語名稱。
    value       計算所得數值（可能為比率、原始計數，或 None = 資訊性/不評分）。
    threshold   合格門檻（部分檢核無固定門檻時可為 None）。
    comparison  比較方式：lte / gte / eq。
    count/total 計算 value 所用的分子/分母（供前端顯示明細）。
    passed      是否合格；denominator 為 0 或缺乏必要基準線時為 None（資訊性，不計分）。
    detail      違規項目清單（task_id / link 描述），最多 25 筆。
    """

    key: str
    name: str
    name_cn: str
    value: float | None = None
    threshold: float | None = None
    comparison: str = "lte"
    count: int = 0
    total: int = 0
    passed: bool | None = None
    detail: list[str] = Field(default_factory=list)


class DcmaReport(BaseModel):
    """DCMA 14-point 排程健康評估報告。

    data_date          資料截止日（working-day offset）。
    checks             14 項檢核結果清單。
    score              可評分項目中通過的比例（0.0 若 applicable_count 為 0）。
    passed_count       通過的檢核項目數。
    applicable_count   可評分（passed is not None）的檢核項目數。
    total_count        檢核項目總數（固定 14）。
    """

    data_date: int
    checks: list[DcmaCheck] = Field(default_factory=list)
    score: float = 0.0
    passed_count: int = 0
    applicable_count: int = 0
    total_count: int = 14
