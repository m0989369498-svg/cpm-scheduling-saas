"""相依類型 + 延時 引擎測試 (Dependency types + lag — engine-direct, no DB).

Batch 3 / FEAT-1：cpm_engine 支援四種相依類型 (dependency types) 與延時 (lag)：
    FS (Finish-to-Start)  succ.es >= pred.ef + lag
    SS (Start-to-Start)   succ.es >= pred.es + lag
    FF (Finish-to-Finish) succ.ef >= pred.ef + lag  (=> succ.es >= pred.ef + lag - succ.duration)
    SF (Start-to-Finish)  succ.ef >= pred.es + lag  (=> succ.es >= pred.es + lag - succ.duration)
lag_days 可為負 (lead / 提前)。

本檔直接呼叫 calculate_cpm (純函式、不接觸資料庫)，逐一驗證契約中的人工驗算
(hand-computed) 情境：

    (a) FS+lag : A(dur2) -FS+3-> B(dur2)  => B.es=5、專案總工期 7
    (b) SS+2   : A(dur5) -SS+2-> B(dur3)  => B.es=2、B.ef=5、總工期 5、兩者皆要徑
    (c) FF+0   : A(dur4) -FF+0-> B(dur2)  => B.ef>=4 -> B.es=2
    (d) SF+10  : A(dur3) -SF+10-> B(dur4) => B.ef>=A.es+10=10 -> B.es=6、總工期 10
    (e) 舊式 predecessors-only 輸入仍以 FS+0 行為 (回歸防護：舊 3 任務樣本總工期 10)
    (f) 以 links 構成的循環 (cycle) 拋出 ValueError

向後相容 (backward compatible)：TaskDefinition.links 為 None 時，由 predecessors
推導為 FS+0 —— 既有行為完全不變。
"""

from __future__ import annotations

import pytest

from app.core.cpm_engine import calculate_cpm, project_duration
from app.schemas.schedule import DependencyLink, TaskDefinition


def _link(pred: str, dep_type: str = "FS", lag_days: int = 0) -> DependencyLink:
    """組一條相依連結 (dependency link) 的小工具。"""
    return DependencyLink(
        predecessor_task_id=pred, dep_type=dep_type, lag_days=lag_days
    )


# --------------------------------------------------------------------------- #
# (a) FS + lag：A(dur2) -FS+3-> B(dur2)
#     前向：B.es = A.ef + 3 = 2 + 3 = 5，B.ef = 7，總工期 = 7。
#     後向：B (sink) lf=7、ls=5；A 之 FS 界限 = B.ls - lag = 5-3 = 2 -> A.lf=2、ls=0。
#     兩者 float=0 -> 皆為要徑。
# --------------------------------------------------------------------------- #
def test_fs_with_lag():
    tasks = [
        TaskDefinition(task_id="A", task_name="前置", duration=2),
        TaskDefinition(
            task_id="B",
            task_name="後續",
            duration=2,
            links=[_link("A", "FS", 3)],
        ),
    ]
    result = calculate_cpm(tasks)

    assert result["A"].es == 0
    assert result["A"].ef == 2
    assert result["B"].es == 5
    assert result["B"].ef == 7
    assert project_duration(result) == 7

    # 後向驗算：A.lf = B.ls - lag = 5 - 3 = 2
    assert result["B"].ls == 5
    assert result["B"].lf == 7
    assert result["A"].lf == 2
    assert result["A"].ls == 0
    assert result["A"].is_critical is True
    assert result["B"].is_critical is True


# --------------------------------------------------------------------------- #
# (b) SS + 2：A(dur5) -SS+2-> B(dur3)
#     前向：B.es = A.es + 2 = 2，B.ef = 5，總工期 = 5。
#     後向：B (sink) lf=5、ls=2；A 之 SS 界限 = B.ls - lag + A.duration
#           = (2-2)+5 = 5 -> A.lf = min(5, 總工期 5) = 5、ls=0。
#     A float=0、B float=0 -> 兩者皆要徑。
# --------------------------------------------------------------------------- #
def test_ss_with_lag_both_critical():
    tasks = [
        TaskDefinition(task_id="A", task_name="開挖", duration=5),
        TaskDefinition(
            task_id="B",
            task_name="排水",
            duration=3,
            links=[_link("A", "SS", 2)],
        ),
    ]
    result = calculate_cpm(tasks)

    assert result["B"].es == 2
    assert result["B"].ef == 5
    assert project_duration(result) == 5

    assert result["A"].ls == 0
    assert result["A"].lf == 5
    assert result["A"].float_time == 0
    assert result["B"].float_time == 0
    assert result["A"].is_critical is True
    assert result["B"].is_critical is True


# --------------------------------------------------------------------------- #
# (c) FF + 0：A(dur4) -FF-> B(dur2)
#     B.ef >= A.ef + 0 = 4 -> B.es = max(0, 4 - 2) = 2、B.ef = 4。
# --------------------------------------------------------------------------- #
def test_ff_zero_lag():
    tasks = [
        TaskDefinition(task_id="A", task_name="主體", duration=4),
        TaskDefinition(
            task_id="B",
            task_name="收尾",
            duration=2,
            links=[_link("A", "FF", 0)],
        ),
    ]
    result = calculate_cpm(tasks)

    assert result["B"].es == 2
    assert result["B"].ef == 4
    assert project_duration(result) == 4


# --------------------------------------------------------------------------- #
# (d) SF + 10：A(dur3) -SF+10-> B(dur4)
#     B.ef >= A.es + 10 = 10 -> B.es = 10 - 4 = 6、B.ef = 10，總工期 = 10。
# --------------------------------------------------------------------------- #
def test_sf_with_lag():
    tasks = [
        TaskDefinition(task_id="A", task_name="拆除", duration=3),
        TaskDefinition(
            task_id="B",
            task_name="夜班交接",
            duration=4,
            links=[_link("A", "SF", 10)],
        ),
    ]
    result = calculate_cpm(tasks)

    assert result["B"].es == 6
    assert result["B"].ef == 10
    assert project_duration(result) == 10


# --------------------------------------------------------------------------- #
# (e) 回歸防護 (regression guard)：舊式 predecessors-only 輸入 (links=None)
#     必須維持 FS+0 行為 —— 與既有 test_cpm_engine 的 3 任務樣本完全一致。
#     T-01(3) -> T-02(5) -> T-03(2)：總工期 10、全要徑。
# --------------------------------------------------------------------------- #
def test_legacy_predecessors_only_behaves_as_fs_zero():
    tasks = [
        TaskDefinition(task_id="T-01", task_name="基礎開挖", duration=3, predecessors=[]),
        TaskDefinition(
            task_id="T-02", task_name="結構施作", duration=5, predecessors=["T-01"]
        ),
        TaskDefinition(
            task_id="T-03", task_name="機電安裝", duration=2, predecessors=["T-02"]
        ),
    ]
    result = calculate_cpm(tasks)

    assert project_duration(result) == 10

    assert result["T-01"].es == 0 and result["T-01"].ef == 3
    assert result["T-02"].es == 3 and result["T-02"].ef == 8
    assert result["T-03"].es == 8 and result["T-03"].ef == 10

    # 線性要徑：LS=ES、LF=EF、float=0、全要徑。
    for tid in ("T-01", "T-02", "T-03"):
        assert result[tid].float_time == 0, f"{tid} float 應為 0"
        assert result[tid].is_critical is True, f"{tid} 應位於要徑"


# --------------------------------------------------------------------------- #
# (e-bis) 等價性：links 提供 FS+0 與 predecessors-only 輸入結果必須完全一致。
# --------------------------------------------------------------------------- #
def test_links_fs_zero_equivalent_to_predecessors():
    via_preds = calculate_cpm(
        [
            TaskDefinition(task_id="X", duration=3),
            TaskDefinition(task_id="Y", duration=5, predecessors=["X"]),
        ]
    )
    via_links = calculate_cpm(
        [
            TaskDefinition(task_id="X", duration=3),
            TaskDefinition(task_id="Y", duration=5, links=[_link("X", "FS", 0)]),
        ]
    )
    for tid in ("X", "Y"):
        assert via_links[tid].es == via_preds[tid].es
        assert via_links[tid].ef == via_preds[tid].ef
        assert via_links[tid].ls == via_preds[tid].ls
        assert via_links[tid].lf == via_preds[tid].lf
        assert via_links[tid].float_time == via_preds[tid].float_time
        assert via_links[tid].is_critical == via_preds[tid].is_critical


# --------------------------------------------------------------------------- #
# (f) 以 links 構成循環 (cycle) -> ValueError (拓樸排序無法消化所有節點)。
# --------------------------------------------------------------------------- #
def test_cycle_via_links_raises_value_error():
    tasks = [
        TaskDefinition(task_id="A", duration=2, links=[_link("B", "SS", 0)]),
        TaskDefinition(task_id="B", duration=2, links=[_link("A", "SS", 0)]),
    ]
    with pytest.raises(ValueError):
        calculate_cpm(tasks)


# --------------------------------------------------------------------------- #
# 補充：dep_type 驗證 —— 僅允許 {FS, SS, FF, SF}
# (pydantic ValidationError 為 ValueError 子類，故以 ValueError 斷言相容兩種實作)。
# --------------------------------------------------------------------------- #
def test_dependency_link_rejects_unknown_dep_type():
    with pytest.raises(ValueError):
        DependencyLink(predecessor_task_id="A", dep_type="XX", lag_days=0)
