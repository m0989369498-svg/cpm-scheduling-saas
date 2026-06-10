"""實獲值管理引擎單元測試 (Earned Value Management engine unit tests).

直接驗證 ``app.core.evm.compute_evm``（純函式，不接觸資料庫、無副作用）。
引擎依 PMI 標準計算 PV / EV / AC 及衍生指標 (SV/CV/SPI/CPI/EAC/ETC/VAC/TCPI)。

主要情境
--------
1. 人工驗算 (hand-computed)：「落後且超支」的兩任務專案。
       baseline: A(es0, dur5, budget50000)、B(es5, dur5, budget50000) -> BAC=100000
       progress: A 100% / ac55000、B 40% / ac30000；data_date=8
   逐項驗算 (PMI 公式)：
       EV = 50000*1.00 + 50000*0.40 = 50000 + 20000 = 70000
       PV : A pf=clamp((8-0)/5)=1.0 -> 50000
            B pf=clamp((8-5)/5)=0.6 -> 30000   => PV = 80000
       AC = 55000 + 30000 = 85000
       SV = EV-PV = -10000  (落後進度)
       CV = EV-AC = -15000  (成本超支)
       SPI = EV/PV = 70000/80000 = 0.875
       CPI = EV/AC = 70000/85000 = 0.82352941...
       EAC = BAC/CPI = 100000/0.82352941 = 121428.5714...
       ETC = EAC-AC ; VAC = BAC-EAC < 0
       TCPI = (BAC-EV)/(BAC-AC) = 30000/15000 = 2.0
       risk_flagged = True (SPI<0.9 且 CPI<0.9)

2. 健康情境 (on plan)：進度與計畫吻合、無超支 -> risk_flagged=False，
   且 SPI/CPI 接近 1.0、SV/CV 接近 0。

3. PV 曲線特性：pv_curve 隨日期單調不減 (non-decreasing)，
   每點 PV 皆落在 [0, BAC]，最終值收斂至 BAC。
"""

from __future__ import annotations

import pytest

from app.core.evm import compute_evm


# --------------------------------------------------------------------------- #
# 樣本：人工驗算情境 (落後 + 超支)
# --------------------------------------------------------------------------- #
def _behind_over_baseline() -> list[dict]:
    """baseline 任務：A(es0,dur5,budget50000) 接 B(es5,dur5,budget50000)。"""
    return [
        {"task_id": "A", "es": 0, "duration": 5, "budget": 50000.0},
        {"task_id": "B", "es": 5, "duration": 5, "budget": 50000.0},
    ]


def _behind_over_progress() -> dict[str, dict]:
    """progress：A 完工 100% 但 ac55000；B 完成 40% ac30000。"""
    return {
        "A": {"percent_complete": 100, "actual_cost": 55000.0},
        "B": {"percent_complete": 40, "actual_cost": 30000.0},
    }


def _behind_over_result():
    """以 data_date=8 計算落後+超支情境的 EVM 結果。"""
    return compute_evm(
        _behind_over_baseline(), _behind_over_progress(), data_date=8
    )


# --------------------------------------------------------------------------- #
# 情境 1：人工驗算 — 核心 EVM 量值
# --------------------------------------------------------------------------- #
def test_bac_is_sum_of_budgets():
    """BAC = 各任務預算總和 = 50000 + 50000 = 100000。"""
    result = _behind_over_result()
    assert result.bac == pytest.approx(100000.0)


def test_data_date_echoed():
    """回傳的 data_date 應等於輸入值。"""
    result = _behind_over_result()
    assert result.data_date == 8


def test_planned_value():
    """PV：A pf=1.0 ->50000、B pf=clamp((8-5)/5)=0.6 ->30000，合計 80000。"""
    result = _behind_over_result()
    assert result.pv == pytest.approx(80000.0)


def test_earned_value():
    """EV：50000*1.00 + 50000*0.40 = 70000。"""
    result = _behind_over_result()
    assert result.ev == pytest.approx(70000.0)


def test_actual_cost():
    """AC：55000 + 30000 = 85000。"""
    result = _behind_over_result()
    assert result.ac == pytest.approx(85000.0)


def test_schedule_variance_negative():
    """SV = EV - PV = 70000 - 80000 = -10000 (落後進度)。"""
    result = _behind_over_result()
    assert result.sv == pytest.approx(-10000.0)


def test_cost_variance_negative():
    """CV = EV - AC = 70000 - 85000 = -15000 (成本超支)。"""
    result = _behind_over_result()
    assert result.cv == pytest.approx(-15000.0)


def test_spi_below_one():
    """SPI = EV/PV = 70000/80000 = 0.875。"""
    result = _behind_over_result()
    assert result.spi == pytest.approx(0.875)


def test_cpi_below_one():
    """CPI = EV/AC = 70000/85000 = 0.8235294117..."""
    result = _behind_over_result()
    assert result.cpi == pytest.approx(70000.0 / 85000.0)


def test_eac():
    """EAC = BAC/CPI = 100000 / (70000/85000) = 121428.5714..."""
    result = _behind_over_result()
    expected_eac = 100000.0 / (70000.0 / 85000.0)
    assert result.eac == pytest.approx(expected_eac)
    assert result.eac == pytest.approx(121428.5714285, rel=1e-6)


def test_etc():
    """ETC = EAC - AC。"""
    result = _behind_over_result()
    expected_eac = 100000.0 / (70000.0 / 85000.0)
    assert result.etc == pytest.approx(expected_eac - 85000.0)


def test_vac_negative():
    """VAC = BAC - EAC < 0 (預估完工超出原預算)。"""
    result = _behind_over_result()
    expected_eac = 100000.0 / (70000.0 / 85000.0)
    assert result.vac == pytest.approx(100000.0 - expected_eac)
    assert result.vac < 0.0


def test_tcpi():
    """TCPI = (BAC-EV)/(BAC-AC) = (100000-70000)/(100000-85000) = 2.0。"""
    result = _behind_over_result()
    assert result.tcpi == pytest.approx((100000.0 - 70000.0) / (100000.0 - 85000.0))
    assert result.tcpi == pytest.approx(2.0)


def test_risk_flagged_true_when_behind_and_over_budget():
    """SPI<0.9 且 CPI<0.9 -> risk_flagged 為 True。"""
    result = _behind_over_result()
    assert result.risk_flagged is True


# --------------------------------------------------------------------------- #
# 情境 1：per_task 明細
# --------------------------------------------------------------------------- #
def test_per_task_breakdown():
    """per_task 明細應逐項對齊人工驗算 (planned_pct/pv/ev/ac)。"""
    result = _behind_over_result()
    by_id = {row.task_id if hasattr(row, "task_id") else row["task_id"]: row
             for row in result.per_task}

    assert set(by_id.keys()) == {"A", "B"}

    def _get(row, field):
        return getattr(row, field) if hasattr(row, field) else row[field]

    task_a = by_id["A"]
    assert _get(task_a, "budget") == pytest.approx(50000.0)
    assert _get(task_a, "planned_pct") == 100          # round(1.0*100)
    assert _get(task_a, "percent_complete") == 100
    assert _get(task_a, "pv") == pytest.approx(50000.0)
    assert _get(task_a, "ev") == pytest.approx(50000.0)
    assert _get(task_a, "ac") == pytest.approx(55000.0)

    task_b = by_id["B"]
    assert _get(task_b, "budget") == pytest.approx(50000.0)
    assert _get(task_b, "planned_pct") == 60           # round(0.6*100)
    assert _get(task_b, "percent_complete") == 40
    assert _get(task_b, "pv") == pytest.approx(30000.0)
    assert _get(task_b, "ev") == pytest.approx(20000.0)
    assert _get(task_b, "ac") == pytest.approx(30000.0)


# --------------------------------------------------------------------------- #
# 情境 2：健康情境 (on plan) — risk_flagged 為 False
# --------------------------------------------------------------------------- #
def _healthy_baseline() -> list[dict]:
    return [
        {"task_id": "A", "es": 0, "duration": 5, "budget": 50000.0},
        {"task_id": "B", "es": 5, "duration": 5, "budget": 50000.0},
    ]


def _healthy_progress() -> dict[str, dict]:
    """進度與計畫吻合、成本與實獲值相符 (CPI=SPI=1)。

    data_date=10 (專案結束)：A、B 皆 100% 完工，actual_cost 等於各自預算。
    -> PV=EV=AC=BAC=100000。
    """
    return {
        "A": {"percent_complete": 100, "actual_cost": 50000.0},
        "B": {"percent_complete": 100, "actual_cost": 50000.0},
    }


def test_healthy_case_on_plan():
    """on-plan 情境：PV=EV=AC=BAC，SV/CV=0，SPI/CPI=1.0。"""
    result = compute_evm(_healthy_baseline(), _healthy_progress(), data_date=10)

    assert result.bac == pytest.approx(100000.0)
    assert result.pv == pytest.approx(100000.0)
    assert result.ev == pytest.approx(100000.0)
    assert result.ac == pytest.approx(100000.0)
    assert result.sv == pytest.approx(0.0)
    assert result.cv == pytest.approx(0.0)
    assert result.spi == pytest.approx(1.0)
    assert result.cpi == pytest.approx(1.0)


def test_healthy_case_not_risk_flagged():
    """健康情境 (SPI>=0.9 且 CPI>=0.9) -> risk_flagged 為 False。"""
    result = compute_evm(_healthy_baseline(), _healthy_progress(), data_date=10)
    assert result.risk_flagged is False


def test_healthy_case_eac_equals_bac_when_on_budget():
    """CPI=1.0 時 EAC=BAC、VAC=0 (預估完工恰等於原預算)。"""
    result = compute_evm(_healthy_baseline(), _healthy_progress(), data_date=10)
    assert result.eac == pytest.approx(100000.0)
    assert result.vac == pytest.approx(0.0)


# --------------------------------------------------------------------------- #
# 情境 3：PV 曲線 — 單調不減、落在 [0, BAC]、收斂至 BAC
# --------------------------------------------------------------------------- #
def test_pv_curve_is_non_decreasing_within_zero_to_bac():
    """pv_curve 隨日期單調不減 (non-decreasing) 且每點 PV 落在 [0, BAC]。"""
    result = _behind_over_result()
    assert len(result.pv_curve) >= 1

    bac = result.bac

    def _pt(point, field):
        return getattr(point, field) if hasattr(point, field) else point[field]

    pvs = [_pt(point, "pv") for point in result.pv_curve]
    days = [_pt(point, "day") for point in result.pv_curve]

    # 每點 PV 介於 [0, BAC]
    for value in pvs:
        assert 0.0 - 1e-9 <= value <= bac + 1e-9, f"PV {value} 超出 [0, {bac}]"

    # 單調不減
    for earlier, later in zip(pvs, pvs[1:]):
        assert later >= earlier - 1e-9, "PV 曲線必須單調不減 (non-decreasing)"

    # 日期由小到大排列
    assert days == sorted(days)


def test_pv_curve_spans_project_duration_and_converges_to_bac():
    """pv_curve 涵蓋 0..project_duration；最終 (最大工期) 之 PV 收斂至 BAC。"""
    result = _behind_over_result()

    def _pt(point, field):
        return getattr(point, field) if hasattr(point, field) else point[field]

    days = [_pt(point, "day") for point in result.pv_curve]
    # 專案總工期 = max(es+duration) = max(0+5, 5+5) = 10 -> 涵蓋 0..10
    assert days[0] == 0
    assert days[-1] == 10

    # 計畫結束時所有預算皆應「依計畫」獲得 -> 末點 PV == BAC。
    assert _pt(result.pv_curve[-1], "pv") == pytest.approx(result.bac)
