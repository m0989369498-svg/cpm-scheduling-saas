"""成本負載引擎（Cost Loading Engine）。Pro Batch D（FEATURE D1）。

純函式模組：不接觸資料庫、不依賴外部狀態，方便單元測試與重複呼叫。

公開 API：
    compute_cost_loading(tasks, demands, rates, categories, wbs_of,
                          project_duration) -> CostResult

計算規則（與 SPEC 逐條對齊）：
    task_cost            = duration * Σ(qty * rates.get(res, 0))  對每任務的各資源需求。
    per_resource_cost     = {res: duration * qty * rate}          單一任務內各資源的成本。
    total_cost            = Σ task_cost                            全專案總成本。
    by_resource / by_category / by_wbs
                          = 依資源類別 / 資源大類 / WBS 節點的成本加總。
    cost_curve            每個任務的成本於 [es, ef) 區間「均勻攤銷」：
                              duration > 0 -> task_cost/duration 分攤至該區間每一天；
                              duration <= 0 -> 全額落在 es 當天（避免除以 0）。
                          point = {day, cost, cumulative}；cumulative 非遞減。
                          cumulative 以「各任務至當日已釋出的成本」直接加總
                          （最後一日必然完整釋出 task_cost），而非累加逐日
                          浮點切片 —— 確保曲線終點與 total_cost「精確相等」
                          （不受 task_cost/duration 除不盡的 IEEE-754 誤差影響）。
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable

from app.schemas.cost import CostCurvePoint, CostResult, CostTaskBreakdown


def compute_cost_loading(
    tasks: Iterable[Any],
    demands: dict[str, dict[str, int]] | None,
    rates: dict[str, float] | None,
    categories: dict[str, str] | None,
    wbs_of: dict[str, str | None] | None,
    project_duration: int,
) -> CostResult:
    """計算成本負載（純函式，無 DB）。

    tasks    可迭代的任務物件，須具備 .task_id .task_name .duration .es .ef
             （沿用已持久化的 CPM 欄位；不重新計算 CPM）。
    demands  {task_id: {resource_type: qty}} 各任務資源需求。
    rates    {resource_type: unit_cost} 各資源每單位每工作日的成本。
    categories {resource_type: category} 各資源所屬大類
             （labor / equipment / material / subcontract）。
    wbs_of   {task_id: wbs_code | None} 各任務所屬 WBS 節點代碼。
    project_duration 專案總工期（決定 cost_curve 的天數範圍）。
    """
    demands = demands or {}
    rates = rates or {}
    categories = categories or {}
    wbs_of = wbs_of or {}

    per_task: list[CostTaskBreakdown] = []
    by_resource: dict[str, float] = defaultdict(float)
    by_category: dict[str, float] = defaultdict(float)
    by_wbs: dict[str, float] = defaultdict(float)
    total_cost = 0.0

    # 攤銷規格 (task_cost, es, ef, duration)：僅收錄有成本的任務，
    # 且「維持與 total_cost 相同的累加順序」—— 見下方 cost_curve 說明。
    spreads: list[tuple[float, int, int, int]] = []

    for task in tasks:
        task_id = task.task_id
        duration = int(getattr(task, "duration", 0) or 0)
        demand = demands.get(task_id) or {}

        per_resource_cost: dict[str, float] = {}
        task_cost = 0.0
        for res, qty in demand.items():
            rate = float(rates.get(res, 0))
            cost = duration * float(qty) * rate
            per_resource_cost[res] = cost
            task_cost += cost
            by_resource[res] += cost
            by_category[categories.get(res, "labor")] += cost

        total_cost += task_cost
        wbs_code = wbs_of.get(task_id) or ""
        by_wbs[wbs_code] += task_cost

        per_task.append(
            CostTaskBreakdown(
                task_id=task_id,
                task_name=getattr(task, "task_name", "") or "",
                duration=duration,
                cost=task_cost,
                per_resource=per_resource_cost,
            )
        )

        if task_cost:
            es = int(getattr(task, "es", 0) or 0)
            ef = int(getattr(task, "ef", 0) or 0)
            spreads.append((task_cost, es, ef, duration))

    # cost_curve：每日的 cumulative 直接以「各任務至當日已釋出的成本」加總 ——
    #   day <  es                      -> 尚未開始，釋出 0；
    #   duration <= 0（es 當日起）      -> 全額 task_cost（避免除以 0）；
    #   [es, ef) 攤銷中                 -> task_cost * 已進行天數 / duration；
    #   攤銷完畢（day >= ef-1 或天數滿） -> 精確的 task_cost（不重播浮點切片）。
    # 因最後一日各任務均以「精確 task_cost」入帳、且累加順序與 total_cost 相同，
    # 曲線終點與 total_cost 位元級相等；rounding 單調性亦保證 cumulative 非遞減。
    # 逐日花費 cost 取相鄰 cumulative 之差（恆 >= 0）。
    cost_curve: list[CostCurvePoint] = []
    prev_cumulative = 0.0
    for day in range(max(0, int(project_duration)) + 1):
        cumulative = 0.0
        for t_cost, t_es, t_ef, t_dur in spreads:
            if day < t_es:
                continue
            days_elapsed = day - t_es + 1
            if t_dur <= 0 or days_elapsed >= t_dur or day >= t_ef - 1:
                cumulative += t_cost
            else:
                cumulative += t_cost * days_elapsed / t_dur
        cost_curve.append(
            CostCurvePoint(
                day=day, cost=cumulative - prev_cumulative, cumulative=cumulative
            )
        )
        prev_cumulative = cumulative

    return CostResult(
        total_cost=total_cost,
        by_resource=dict(by_resource),
        by_category=dict(by_category),
        by_wbs=dict(by_wbs),
        per_task=per_task,
        cost_curve=cost_curve,
    )
