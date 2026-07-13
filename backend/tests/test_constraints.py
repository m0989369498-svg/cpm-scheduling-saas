"""活動限制條件（activity constraints, P6-style）引擎測試 — engine-direct, no DB.

Pro Batch B / FEATURE 2：cpm_engine 支援單一任務的限制條件（constraint_type +
constraint_day，工作日 offset，與 es/ef 同一軸）：

    SNET  Start No Earlier Than   前向：es = max(es, day)
    FNET  Finish No Earlier Than  前向：es = max(es, day - duration)
    MSO   Mandatory Start On      前向：es = max(es, day)；後向：lf = min(lf, day + duration)
    MFO   Mandatory Finish On     前向：es = max(es, day - duration)；後向：lf = min(lf, day)
    FNLT  Finish No Later Than    後向：lf = min(lf, day)
    SNLT  Start No Later Than     後向：lf = min(lf, day + duration)

前向套用後 ef = es + duration；後向套用後 ls = lf - duration。
float_time = ls - es（可能為負 = 限制條件衝突）；is_critical = float_time <= 0
（由舊版 == 0 放寬，但未帶限制條件時 float 永不為負，行為與舊版逐位元相同）；
constraint_violated = float_time < 0。

本檔直接呼叫 calculate_cpm（純函式、不接觸資料庫），逐一驗證契約中的人工驗算
(hand-computed) 情境：

    (a) SNET  : A(dur3) 無前置，SNET day5   -> es=5、ef=8、專案總工期=8、float=0
    (b) FNLT  : A(dur5) -FS-> B(dur3)，B 帶 FNLT day6
                -> B.es=5、B.ef=8，但 B.lf=6 -> B.ls=3、float=-2（違反 / 要徑）；
                A 反向亦受牽動：A.lf=min(B.ls)=3 -> A.float=-2（違反 / 要徑）
    (c) MSO   : A(dur2) 無前置，MSO day4    -> es=4、ls=4、float=0（未違反）
    (d) FNET  : A(dur4) 無前置，FNET day6   -> es=2
    (e) 舊式（無任何限制條件）：3 任務鏈 5/3/2 樣本 -> es 0/5/8、總工期 10、
        全數要徑、皆未違反（回歸防護：逐位元與舊版相同）
"""

from __future__ import annotations

from app.core.cpm_engine import calculate_cpm, project_duration
from app.schemas.schedule import TaskDefinition


# --------------------------------------------------------------------------- #
# (a) SNET：A(dur3) 單獨、SNET day5
#     前向：es = max(0, 5) = 5，ef = 5+3 = 8，專案總工期 = 8。
#     後向：A 為 sink，lf 預設 = 總工期 8；SNET 不影響後向 -> lf=8，ls=8-3=5。
#     float = ls-es = 0 -> 要徑、未違反。
# --------------------------------------------------------------------------- #
def test_snet_pushes_earliest_start():
    tasks = [
        TaskDefinition(
            task_id="A",
            task_name="限制-SNET",
            duration=3,
            constraint_type="SNET",
            constraint_day=5,
        ),
    ]
    result = calculate_cpm(tasks)

    assert result["A"].es == 5
    assert result["A"].ef == 8
    assert project_duration(result) == 8
    assert result["A"].float_time == 0
    assert result["A"].is_critical is True
    assert result["A"].constraint_violated is False


# --------------------------------------------------------------------------- #
# (b) FNLT 衝突：A(dur5，無前置) -FS-> B(dur3)，B 帶 FNLT day6。
#     前向（不受 FNLT 影響，FNLT 僅作用於後向）：
#         A.es=0、A.ef=5；B.es=max(0, A.ef+0)=5、B.ef=8。
#     專案總工期 = max(ef) = 8。
#     後向：
#         B 為 sink，lf 預設=8；FNLT: lf=min(8,6)=6 -> B.ls=6-3=3。
#         B.float = ls-es = 3-5 = -2 -> 違反、要徑（float<=0）。
#         A 之 FS 界限 = B.ls - lag = 3-0 = 3 -> A.lf=min(8,3)=3 -> A.ls=3-5=-2。
#         A.float = ls-es = -2-0 = -2 -> 違反、要徑。
# --------------------------------------------------------------------------- #
def test_fnlt_conflict_produces_negative_float_and_propagates_upstream():
    tasks = [
        TaskDefinition(task_id="A", task_name="前置", duration=5),
        TaskDefinition(
            task_id="B",
            task_name="限制-FNLT",
            duration=3,
            predecessors=["A"],
            constraint_type="FNLT",
            constraint_day=6,
        ),
    ]
    result = calculate_cpm(tasks)

    task_b = result["B"]
    assert task_b.es == 5
    assert task_b.ef == 8
    assert task_b.lf == 6
    assert task_b.ls == 3
    assert task_b.float_time == -2
    assert task_b.is_critical is True
    assert task_b.constraint_violated is True

    task_a = result["A"]
    assert task_a.es == 0
    assert task_a.ef == 5
    assert task_a.lf == 3
    assert task_a.ls == -2
    assert task_a.float_time == -2
    assert task_a.is_critical is True
    assert task_a.constraint_violated is True

    assert project_duration(result) == 8


# --------------------------------------------------------------------------- #
# (c) MSO 釘住：A(dur2) 無前置，MSO day4。
#     前向：es=max(0,4)=4，ef=6。
#     後向：sink lf 預設=總工期=6；MSO 後向界限=day+duration=4+2=6 -> lf=min(6,6)=6、
#           ls=6-2=4。
#     float = ls-es = 0 -> 未違反。
# --------------------------------------------------------------------------- #
def test_mso_pins_start_without_violation():
    tasks = [
        TaskDefinition(
            task_id="A",
            task_name="限制-MSO",
            duration=2,
            constraint_type="MSO",
            constraint_day=4,
        ),
    ]
    result = calculate_cpm(tasks)

    assert result["A"].es == 4
    assert result["A"].ls == 4
    assert result["A"].ef == 6
    assert result["A"].float_time == 0
    assert result["A"].is_critical is True
    assert result["A"].constraint_violated is False


# --------------------------------------------------------------------------- #
# (d) FNET：A(dur4) 無前置，FNET day6。
#     前向：es = max(0, day-duration) = max(0, 6-4) = 2，ef = 2+4 = 6。
# --------------------------------------------------------------------------- #
def test_fnet_sets_earliest_start_from_finish_bound():
    tasks = [
        TaskDefinition(
            task_id="A",
            task_name="限制-FNET",
            duration=4,
            constraint_type="FNET",
            constraint_day=6,
        ),
    ]
    result = calculate_cpm(tasks)

    assert result["A"].es == 2
    assert result["A"].ef == 6


# --------------------------------------------------------------------------- #
# (e) 舊式（legacy）回歸防護：無任何限制條件的 3 任務鏈
#     T-01(5) -> T-02(3) -> T-03(2)，總工期 10 —— 逐位元與舊版行為相同：
#     es 0/5/8，全數 float=0（皆要徑），constraint_violated 全數 False。
# --------------------------------------------------------------------------- #
def test_legacy_no_constraints_bit_identical_to_pre_batch_b():
    tasks = [
        TaskDefinition(task_id="T-01", task_name="基地開挖", duration=5),
        TaskDefinition(
            task_id="T-02", task_name="一樓鋼筋綁紮", duration=3,
            predecessors=["T-01"],
        ),
        TaskDefinition(
            task_id="T-03", task_name="一樓混凝土澆置", duration=2,
            predecessors=["T-02"],
        ),
    ]
    result = calculate_cpm(tasks)

    assert result["T-01"].es == 0
    assert result["T-01"].ef == 5
    assert result["T-02"].es == 5
    assert result["T-02"].ef == 8
    assert result["T-03"].es == 8
    assert result["T-03"].ef == 10
    assert project_duration(result) == 10

    for task_id in ("T-01", "T-02", "T-03"):
        res = result[task_id]
        assert res.float_time == 0
        assert res.is_critical is True
        assert res.constraint_violated is False
