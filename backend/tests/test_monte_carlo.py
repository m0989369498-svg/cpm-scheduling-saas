"""蒙地卡羅排程模擬引擎單元測試 (Monte Carlo schedule simulation tests).

直接驗證 ``app.core.monte_carlo.simulate_schedule``（純函式，不接觸資料庫）。
透過 ``random.seed(...)`` 固定亂數序列以確保結果可重現 (deterministic)。

測試標的為一條「嚴格線性鏈」(strictly linear chain)：
    L-01 -> L-02 -> L-03
每個任務皆有三點估計 (optimistic, most_likely, pessimistic)。

驗證重點（依契約）：
  - s_curve 為「累積機率」(cumulative probability)：機率值需單調不減
    (non-decreasing)，且全部落在 [0, 1]。
  - 百分位數滿足 p10 <= p50 <= p90。
  - 線性鏈中每個任務恆位於唯一路徑 -> criticality_index 必為 1.0。
  - on_time_probability 在給定 deadline 時須落在 [0, 1]；未給定時為 None。
  - 輸出 iterations 與輸入一致；mean/std 為非負浮點數。
"""

from __future__ import annotations

import random

from app.core.monte_carlo import simulate_schedule
from app.schemas.schedule import TaskDefinition


# --------------------------------------------------------------------------- #
# 樣本：嚴格線性鏈 L-01 -> L-02 -> L-03，附三點估計 (a, m, b)
# --------------------------------------------------------------------------- #
def _linear_chain() -> list[TaskDefinition]:
    return [
        TaskDefinition(task_id="L-01", task_name="基礎開挖", duration=5, predecessors=[]),
        TaskDefinition(
            task_id="L-02", task_name="結構施作", duration=3, predecessors=["L-01"]
        ),
        TaskDefinition(
            task_id="L-03", task_name="機電安裝", duration=2, predecessors=["L-02"]
        ),
    ]


def _risk_params() -> dict[str, tuple[int, int, int]]:
    """三點估計 (optimistic, most_likely, pessimistic)，皆滿足 b > a。"""
    return {
        "L-01": (3, 5, 9),
        "L-02": (2, 3, 7),
        "L-03": (1, 2, 5),
    }


def _run(iterations: int = 2000, deadline: int | None = None):
    """以固定 seed 執行模擬，確保可重現。"""
    random.seed(12345)
    return simulate_schedule(
        _linear_chain(), _risk_params(), iterations=iterations, deadline=deadline
    )


# --------------------------------------------------------------------------- #
# 基本輸出形狀
# --------------------------------------------------------------------------- #
def test_iterations_echoed():
    """回傳的 iterations 應等於輸入值。"""
    result = _run(iterations=1500)
    assert result.iterations == 1500


def test_mean_and_std_are_non_negative_floats():
    """mean / std 為非負浮點數。"""
    result = _run()
    assert isinstance(result.mean, float)
    assert isinstance(result.std, float)
    assert result.mean > 0.0
    assert result.std >= 0.0


# --------------------------------------------------------------------------- #
# S 曲線（累積機率）：單調不減且介於 [0, 1]
# --------------------------------------------------------------------------- #
def test_s_curve_is_cumulative_within_unit_interval():
    """s_curve 機率需單調不減 (non-decreasing) 且全部落在 [0, 1]。"""
    result = _run()
    assert len(result.s_curve) >= 1

    probs = [pt.probability for pt in result.s_curve]
    for p in probs:
        assert 0.0 <= p <= 1.0, f"累積機率 {p} 超出 [0,1]"

    for earlier, later in zip(probs, probs[1:]):
        assert later >= earlier, "累積機率必須單調不減 (non-decreasing)"


def test_s_curve_durations_are_sorted_and_reach_certainty():
    """s_curve 的 duration 由小到大排列；最大工期的累積機率應為 1.0。"""
    result = _run()
    durations = [pt.duration for pt in result.s_curve]
    assert durations == sorted(durations)
    # 最後一點 (最大工期) 的 P(duration <= d) 必為 1.0。
    assert result.s_curve[-1].probability == 1.0


# --------------------------------------------------------------------------- #
# 百分位數：p10 <= p50 <= p90
# --------------------------------------------------------------------------- #
def test_percentiles_monotonic():
    """百分位數需滿足 p10 <= p50 <= p90，且皆為整數。"""
    result = _run()
    assert isinstance(result.p10, int)
    assert isinstance(result.p50, int)
    assert isinstance(result.p90, int)
    assert result.p10 <= result.p50 <= result.p90


# --------------------------------------------------------------------------- #
# 要徑機率：線性鏈中所有任務 criticality_index == 1.0
# --------------------------------------------------------------------------- #
def test_linear_chain_all_tasks_fully_critical():
    """嚴格線性鏈中唯一路徑通過所有任務 -> 每個任務 criticality_index == 1.0。"""
    result = _run()
    index_by_id = {item.task_id: item.index for item in result.criticality}

    assert set(index_by_id.keys()) == {"L-01", "L-02", "L-03"}
    for task_id in ("L-01", "L-02", "L-03"):
        assert index_by_id[task_id] == 1.0, f"{task_id} 要徑指數應為 1.0"


# --------------------------------------------------------------------------- #
# 準時機率：給定 deadline 時落在 [0, 1]；未給定時為 None
# --------------------------------------------------------------------------- #
def test_on_time_probability_none_without_deadline():
    """未提供 deadline 時，on_time_probability 應為 None。"""
    result = _run(deadline=None)
    assert result.deadline is None
    assert result.on_time_probability is None


def test_on_time_probability_within_unit_interval_with_deadline():
    """提供 deadline 時，on_time_probability 應落在 [0, 1]。"""
    result = _run(deadline=12)
    assert result.deadline == 12
    assert result.on_time_probability is not None
    assert 0.0 <= result.on_time_probability <= 1.0


def test_generous_deadline_high_on_time_probability():
    """極寬鬆的 deadline（遠大於所有悲觀工期總和）-> 準時機率為 1.0。

    悲觀工期總和 = 9 + 7 + 5 = 21；取 deadline=100 必然準時。
    """
    result = _run(deadline=100)
    assert result.on_time_probability == 1.0


def test_impossible_deadline_zero_on_time_probability():
    """不可能達成的 deadline（小於所有樂觀工期總和）-> 準時機率為 0.0。

    樂觀工期總和 = 3 + 2 + 1 = 6；取 deadline=5 必然逾期。
    """
    result = _run(deadline=5)
    assert result.on_time_probability == 0.0


# --------------------------------------------------------------------------- #
# 可重現性 (determinism)：相同 seed -> 相同結果
# --------------------------------------------------------------------------- #
def test_deterministic_with_same_seed():
    """相同 seed 兩次執行應得到完全相同的統計結果。"""
    first = _run(iterations=1000)
    second = _run(iterations=1000)
    assert first.mean == second.mean
    assert (first.p10, first.p50, first.p90) == (second.p10, second.p50, second.p90)
