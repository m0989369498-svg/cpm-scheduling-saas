"""CPM 引擎單元測試 (Critical Path Method engine unit tests).

驗證 forward/backward pass 計算的 ES/EF/LS/LF/float/is_critical，
以樣本 payload 中的 T-01..T-03 線性相依鏈為基準：
    T-01 (工期3) -> T-02 (工期5) -> T-03 (工期2)
預期 project_duration = 10，且三個任務全部位於要徑 (critical path)。
此外驗證：相依環 (cycle) 與未知前置任務 (unknown predecessor) 皆拋出 ValueError。
"""

import pytest

from app.core.cpm_engine import (
    calculate_cpm,
    critical_path,
    project_duration,
)
from app.schemas.schedule import TaskDefinition


def _sample_tasks() -> list[TaskDefinition]:
    """樣本相依鏈：T-01 -> T-02 -> T-03 (與 sample_payload.json 對齊)。"""
    return [
        TaskDefinition(task_id="T-01", task_name="基礎開挖", duration=3, predecessors=[]),
        TaskDefinition(
            task_id="T-02", task_name="結構施作", duration=5, predecessors=["T-01"]
        ),
        TaskDefinition(
            task_id="T-03", task_name="機電安裝", duration=2, predecessors=["T-02"]
        ),
    ]


def test_calculate_cpm_returns_result_for_each_task():
    result = calculate_cpm(_sample_tasks())
    assert set(result.keys()) == {"T-01", "T-02", "T-03"}


def test_forward_pass_es_ef():
    """前推 (forward pass)：ES 為前置任務 EF 最大值，EF = ES + duration。"""
    result = calculate_cpm(_sample_tasks())

    assert result["T-01"].es == 0
    assert result["T-01"].ef == 3

    assert result["T-02"].es == 3
    assert result["T-02"].ef == 8

    assert result["T-03"].es == 8
    assert result["T-03"].ef == 10


def test_backward_pass_ls_lf():
    """後推 (backward pass)：在線性要徑上 LS=ES、LF=EF。"""
    result = calculate_cpm(_sample_tasks())

    assert result["T-03"].lf == 10
    assert result["T-03"].ls == 8

    assert result["T-02"].lf == 8
    assert result["T-02"].ls == 3

    assert result["T-01"].lf == 3
    assert result["T-01"].ls == 0


def test_float_time_zero_on_critical_chain():
    """純線性鏈上每個任務寬裕時間 (float) 皆為 0。"""
    result = calculate_cpm(_sample_tasks())
    for task_id in ("T-01", "T-02", "T-03"):
        assert result[task_id].float_time == 0, f"{task_id} float 應為 0"


def test_all_tasks_are_critical():
    """T-01、T-02、T-03 全部位於要徑 (is_critical=True)。"""
    result = calculate_cpm(_sample_tasks())
    assert result["T-01"].is_critical is True
    assert result["T-02"].is_critical is True
    assert result["T-03"].is_critical is True


def test_project_duration_is_ten():
    """專案總工期 = 最大 EF = 10 天。"""
    result = calculate_cpm(_sample_tasks())
    assert project_duration(result) == 10


def test_critical_path_ordered():
    """要徑為依序排列的 critical task_id 清單。"""
    result = calculate_cpm(_sample_tasks())
    path = critical_path(result)
    assert path == ["T-01", "T-02", "T-03"]


def test_empty_input_returns_empty_dict():
    """空輸入應回傳空 dict 而非報錯。"""
    assert calculate_cpm([]) == {}


def test_project_duration_empty_is_zero():
    """無任務時專案工期為 0。"""
    assert project_duration({}) == 0


def test_parallel_branch_float_and_critical():
    """並行分支：較短分支應具有正的 float 且非要徑。

        T-A (5) ─┐
                 ├─> T-D (2)
        T-B (2) ─┘ (T-C 為 T-B 後置，工期 1)

    路徑 A->D = 7、B->C->D = 5；要徑為 A->D，C 應有 float=2。
    """
    tasks = [
        TaskDefinition(task_id="T-A", duration=5, predecessors=[]),
        TaskDefinition(task_id="T-B", duration=2, predecessors=[]),
        TaskDefinition(task_id="T-C", duration=1, predecessors=["T-B"]),
        TaskDefinition(task_id="T-D", duration=2, predecessors=["T-A", "T-C"]),
    ]
    result = calculate_cpm(tasks)

    assert project_duration(result) == 7
    assert result["T-A"].is_critical is True
    assert result["T-D"].is_critical is True
    # T-C 在較短分支上，具有寬裕時間
    assert result["T-C"].float_time > 0
    assert result["T-C"].is_critical is False


def test_cycle_raises_value_error():
    """相依環 (cycle) 必須拋出 ValueError (拓樸排序無法消化全部節點)。"""
    tasks = [
        TaskDefinition(task_id="T-01", duration=3, predecessors=["T-03"]),
        TaskDefinition(task_id="T-02", duration=5, predecessors=["T-01"]),
        TaskDefinition(task_id="T-03", duration=2, predecessors=["T-02"]),
    ]
    with pytest.raises(ValueError):
        calculate_cpm(tasks)


def test_unknown_predecessor_raises_value_error():
    """前置任務指向不存在的 task_id 必須拋出 ValueError。"""
    tasks = [
        TaskDefinition(task_id="T-01", duration=3, predecessors=["DOES-NOT-EXIST"]),
    ]
    with pytest.raises(ValueError):
        calculate_cpm(tasks)
