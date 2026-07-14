"""成本負載引擎單元測試 (Cost Loading engine unit tests)。Pro Batch D FEATURE D1。

直接驗證 ``app.core.cost.compute_cost_loading``（純函式，不接觸資料庫）。

涵蓋情境：
  1. 基本成本計算與 by_resource / by_category / by_wbs 彙總（人工驗算）。
  2. cost_curve：累積花費單調不減 (non-decreasing)、且在 [es, ef) 區間均勻攤銷。
  3. 零費率 (rate=0) -> 該資源成本恆為 0。
  4. 零工期 (duration<=0) -> 全額成本落在 es 當天（避免除以 0）。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.cost import compute_cost_loading


@dataclass
class _Task:
    """最小任務物件：僅具備 compute_cost_loading 所需的鴨子型別欄位。"""

    task_id: str
    task_name: str
    duration: int
    es: int
    ef: int


# --------------------------------------------------------------------------- #
# 情境 1：基本成本計算 + 彙總（人工驗算）
# --------------------------------------------------------------------------- #
def _basic_tasks() -> list[_Task]:
    # T-01：dur5，es0 ef5，crane1+manpower10；T-02：dur3，es5 ef8，manpower15。
    return [
        _Task("T-01", "基地開挖", 5, 0, 5),
        _Task("T-02", "一樓鋼筋", 3, 5, 8),
    ]


def _basic_demands() -> dict[str, dict[str, int]]:
    return {
        "T-01": {"crane": 1, "manpower": 10},
        "T-02": {"manpower": 15},
    }


def _basic_rates() -> dict[str, float]:
    return {"crane": 3000.0, "manpower": 250.0}


def _basic_categories() -> dict[str, str]:
    return {"crane": "equipment", "manpower": "labor"}


def _basic_wbs_of() -> dict[str, str | None]:
    return {"T-01": "WBS-1", "T-02": None}


def test_task_cost_hand_computed():
    """T-01 cost = 5*(1*3000 + 10*250) = 5*5500 = 27500；
    T-02 cost = 3*(15*250) = 3*3750 = 11250。"""
    result = compute_cost_loading(
        _basic_tasks(), _basic_demands(), _basic_rates(), _basic_categories(),
        _basic_wbs_of(), project_duration=8,
    )
    by_id = {t.task_id: t for t in result.per_task}
    assert by_id["T-01"].cost == 27500.0
    assert by_id["T-02"].cost == 11250.0
    assert by_id["T-01"].per_resource == {"crane": 15000.0, "manpower": 12500.0}
    assert by_id["T-02"].per_resource == {"manpower": 11250.0}


def test_total_cost_is_sum_of_task_costs():
    result = compute_cost_loading(
        _basic_tasks(), _basic_demands(), _basic_rates(), _basic_categories(),
        _basic_wbs_of(), project_duration=8,
    )
    assert result.total_cost == 27500.0 + 11250.0


def test_rollup_by_resource():
    """crane 僅 T-01 使用 -> 15000；manpower 兩任務合計 12500+11250=23750。"""
    result = compute_cost_loading(
        _basic_tasks(), _basic_demands(), _basic_rates(), _basic_categories(),
        _basic_wbs_of(), project_duration=8,
    )
    assert result.by_resource == {"crane": 15000.0, "manpower": 23750.0}


def test_rollup_by_category():
    """equipment (crane) = 15000；labor (manpower) = 23750。"""
    result = compute_cost_loading(
        _basic_tasks(), _basic_demands(), _basic_rates(), _basic_categories(),
        _basic_wbs_of(), project_duration=8,
    )
    assert result.by_category == {"equipment": 15000.0, "labor": 23750.0}


def test_rollup_by_wbs_none_maps_to_empty_string():
    """T-01 歸屬 WBS-1 (27500)；T-02 無 WBS (None -> '') (11250)。"""
    result = compute_cost_loading(
        _basic_tasks(), _basic_demands(), _basic_rates(), _basic_categories(),
        _basic_wbs_of(), project_duration=8,
    )
    assert result.by_wbs == {"WBS-1": 27500.0, "": 11250.0}


# --------------------------------------------------------------------------- #
# 情境 2：cost_curve —— 均勻攤銷 + 累積單調不減
# --------------------------------------------------------------------------- #
def test_cost_curve_spreads_uniformly_and_cumulative_is_monotonic():
    result = compute_cost_loading(
        _basic_tasks(), _basic_demands(), _basic_rates(), _basic_categories(),
        _basic_wbs_of(), project_duration=8,
    )
    by_day = {p.day: p for p in result.cost_curve}

    # T-01 (27500 / 5 = 5500/day) 佔 day 0..4；T-02 (11250/3 = 3750/day) 佔 day 5..7。
    for day in range(5):
        assert by_day[day].cost == 5500.0
    for day in range(5, 8):
        assert by_day[day].cost == 3750.0
    assert by_day[8].cost == 0.0  # project_duration 當日 (無任務佔用)

    # 累積值單調不減，且最終收斂至 total_cost。
    cumulative_values = [p.cumulative for p in result.cost_curve]
    assert cumulative_values == sorted(cumulative_values)
    assert cumulative_values[-1] == result.total_cost

    # 曲線長度 = project_duration + 1 (day 0..project_duration 含端點)。
    assert len(result.cost_curve) == 9


def test_cost_curve_cumulative_reconciles_exactly_with_total():
    """除不盡的費率 (IEEE-754 無法精確表示 task_cost/duration) 下，
    曲線終點 cumulative 仍須與 total_cost「精確相等」(== 而非近似)，
    且逐日 cost 非負、cumulative 單調不減。

    回歸：舊實作以 task_cost/duration 逐日切片再累加，333.33*6 天情境會
    短少 ~2e-13 -> cost_curve[-1].cumulative != total_cost。
    """
    tasks = [
        _Task("T-01", "除不盡費率A", 6, 0, 6),
        _Task("T-02", "除不盡費率B", 7, 2, 9),
    ]
    demands = {"T-01": {"manpower": 1}, "T-02": {"welder": 3}}
    rates = {"manpower": 333.33, "welder": 142.857}
    result = compute_cost_loading(tasks, demands, rates, {}, {}, 9)

    assert result.cost_curve[-1].cumulative == result.total_cost  # 精確相等
    cumulative_values = [p.cumulative for p in result.cost_curve]
    assert cumulative_values == sorted(cumulative_values)  # 單調不減
    assert all(p.cost >= 0.0 for p in result.cost_curve)  # 逐日花費非負
    # 各任務完整釋出：任一任務結束後的 cumulative 不再受該任務影響。
    assert result.total_cost == 6 * 333.33 + 7 * 3 * 142.857


# --------------------------------------------------------------------------- #
# 情境 3：零費率 -> 該資源成本恆為 0
# --------------------------------------------------------------------------- #
def test_zero_rate_yields_zero_cost_for_that_resource():
    tasks = [_Task("T-01", "無費率示範", 4, 0, 4)]
    demands = {"T-01": {"unrated": 5}}
    rates: dict[str, float] = {}  # unrated 無費率 -> rates.get(res,0) = 0
    categories: dict[str, str] = {}
    wbs_of: dict[str, str | None] = {}

    result = compute_cost_loading(tasks, demands, rates, categories, wbs_of, 4)
    assert result.total_cost == 0.0
    assert result.per_task[0].cost == 0.0
    assert result.per_task[0].per_resource == {"unrated": 0.0}
    # 零成本任務不產生任何非零花費，曲線每日皆為 0。
    assert all(p.cost == 0.0 for p in result.cost_curve)


# --------------------------------------------------------------------------- #
# 情境 4：duration<=0 -> 全額成本落在 es 當天 (避免除以 0)
# --------------------------------------------------------------------------- #
def test_zero_duration_task_contributes_no_cost():
    """duration=0 -> task_cost = 0 * Σ(qty*rate) = 0 (公式恆成立，milestone 無成本)；
    cost_curve 因此無任何攤銷落點，全日花費為 0。"""
    tasks = [_Task("MILESTONE", "里程碑", 0, 3, 3)]
    demands = {"MILESTONE": {"manpower": 2}}
    rates = {"manpower": 100.0}
    categories = {"manpower": "labor"}
    wbs_of: dict[str, str | None] = {}

    result = compute_cost_loading(tasks, demands, rates, categories, wbs_of, 5)
    assert result.per_task[0].cost == 0.0
    assert result.total_cost == 0.0
    assert all(p.cost == 0.0 for p in result.cost_curve)
