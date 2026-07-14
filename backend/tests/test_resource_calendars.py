"""每資源專屬工作日曆測試 (per-resource calendars)。Pro Batch D FEATURE D3。

涵蓋：
  1. level_resources(..., availability=...)：每資源專屬工作日曆降低特定日
     產能，使原本不衝突的排程變成衝突，並據此推遲可移動任務。
  2. 回歸測試 (regression-critical)：availability=None（省略參數 / 明確傳
     None）與批次前行為「逐位元組相同」(bit-identical) —— 沿用
     test_resource_leveling.py 的衝突情境，直接比對整個 LevelingResult。
  3. ResourceCalendar (schema) 的 work_days 驗證 (7 碼 0/1 字串)。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.resource_leveling import level_resources
from app.schemas.analytics import ResourceCalendar
from app.schemas.schedule import TaskDefinition


def _result_by_id(result) -> dict:
    return {t.task_id: t for t in result.tasks}


# --------------------------------------------------------------------------- #
# 1. availability 降低特定日產能 -> 原本不衝突的排程變成衝突
# --------------------------------------------------------------------------- #
def _calendar_tasks() -> list[TaskDefinition]:
    return [
        TaskDefinition(task_id="CRIT", task_name="要徑基準", duration=5, predecessors=[]),
        TaskDefinition(task_id="X", task_name="可移動X", duration=2, predecessors=[]),
        TaskDefinition(task_id="Y", task_name="可移動Y", duration=2, predecessors=[]),
    ]


def _calendar_demands() -> dict[str, dict[str, int]]:
    return {"X": {"crane": 1}, "Y": {"crane": 1}}


def _calendar_limits() -> dict[str, int]:
    return {"crane": 3}


def test_availability_none_no_conflict_at_scalar_limit():
    """limits=3 時 X+Y 同日需求最多 2 <= 3，純量上限下無衝突 -> 兩者皆不被推遲。"""
    result = level_resources(
        _calendar_tasks(), _calendar_demands(), _calendar_limits(), availability=None
    )
    by_id = _result_by_id(result)
    assert by_id["X"].es == 0
    assert by_id["Y"].es == 0
    assert result.leveled_duration == result.original_duration == 5


def test_availability_reduces_day0_capacity_forces_push():
    """day0 專屬產能降至 1 (< X+Y 需求 2) -> 較小 task_id 的可移動任務 (X) 被推遲；
    day1 以後恢復完整產能 (3)，一次推遲即可化解，不展延總工期。"""
    availability = {"crane": [1, 3, 3, 3, 3]}
    result = level_resources(
        _calendar_tasks(), _calendar_demands(), _calendar_limits(), availability
    )
    by_id = _result_by_id(result)

    assert by_id["X"].es == 1, "day0 產能降至 1 (< 需求 2)，X 應被推遲一天"
    assert by_id["Y"].es == 0, "Y 保持原位 (X 被推遲即可化解衝突)"
    assert by_id["CRIT"].es == 0 and by_id["CRIT"].is_critical is True

    # 化解在原工期內完成，不展延。
    assert result.leveled_duration == result.original_duration == 5
    assert result.extended is False
    assert result.over_capacity_days == []
    assert result.unresolved == []


def test_availability_unlisted_resource_falls_back_to_scalar_limit():
    """availability 僅列出 crane；未列出的資源 (若存在需求) 應退回 limits 純量上限。

    以 crane 之外新增 manpower 需求，manpower 未出現在 availability 中，
    純量上限充足時應完全不受影響 (與 availability=None 情境一致)。
    """
    tasks = _calendar_tasks()
    demands = {
        "X": {"crane": 1, "manpower": 5},
        "Y": {"crane": 1, "manpower": 5},
    }
    limits = {"crane": 3, "manpower": 20}
    availability = {"crane": [1, 3, 3, 3, 3]}  # 未列出 manpower

    result = level_resources(tasks, demands, limits, availability)
    by_id = _result_by_id(result)
    # manpower 需求 (5+5=10 <= 20) 從未超載；crane 邏輯與前一測試相同 -> X 仍被推遲。
    assert by_id["X"].es == 1
    assert by_id["Y"].es == 0


# --------------------------------------------------------------------------- #
# 2. 回歸測試：availability=None（省略 / 明確 None）與批次前行為完全一致
# --------------------------------------------------------------------------- #
def _conflict_tasks() -> list[TaskDefinition]:
    """與 test_resource_leveling.py 相同的衝突情境 (逐位元組回歸比對用)。"""
    return [
        TaskDefinition(task_id="BACK1", task_name="要徑前段", duration=4, predecessors=[]),
        TaskDefinition(
            task_id="BACK2", task_name="要徑後段", duration=4, predecessors=["BACK1"]
        ),
        TaskDefinition(task_id="MID", task_name="並行中工項", duration=2, predecessors=[]),
        TaskDefinition(task_id="SHORT", task_name="並行短工項", duration=1, predecessors=[]),
    ]


def _conflict_demands() -> dict[str, dict[str, int]]:
    return {
        "BACK1": {"crane": 1},
        "MID": {"crane": 1},
        "SHORT": {"crane": 1},
    }


def _conflict_limits() -> dict[str, int]:
    return {"crane": 1}


def test_availability_omitted_matches_explicit_none():
    """省略 availability 參數 (預設 None) 與明確傳入 None -> 完全相同結果。"""
    result_omitted = level_resources(
        _conflict_tasks(), _conflict_demands(), _conflict_limits()
    )
    result_explicit_none = level_resources(
        _conflict_tasks(), _conflict_demands(), _conflict_limits(), availability=None
    )
    assert result_omitted.model_dump() == result_explicit_none.model_dump()


def test_availability_none_regression_matches_pre_batch_behavior():
    """availability=None 時，撫平結果須與批次前（無 availability 概念）逐位元組相同：
    人工驗算的關鍵斷言 (與 test_resource_leveling.py 完全一致)。
    """
    result = level_resources(
        _conflict_tasks(), _conflict_demands(), _conflict_limits(), availability=None
    )
    by_id = _result_by_id(result)

    assert by_id["BACK1"].es == 0 and by_id["BACK1"].ef == 4
    assert by_id["BACK1"].is_critical is True
    assert by_id["BACK2"].es == 4 and by_id["BACK2"].ef == 8
    assert by_id["BACK2"].is_critical is True
    assert by_id["MID"].es > 0

    assert result.original_duration == 8
    assert result.leveled_duration == 8
    assert result.extended is False
    assert result.over_capacity_days == []
    assert result.unresolved == []


# --------------------------------------------------------------------------- #
# 3. ResourceCalendar schema —— work_days 驗證
# --------------------------------------------------------------------------- #
def test_resource_calendar_accepts_valid_work_days():
    cal = ResourceCalendar(resource_type="crane", work_days="1111100")
    assert cal.work_days == "1111100"


def test_resource_calendar_default_work_days():
    cal = ResourceCalendar(resource_type="manpower")
    assert cal.work_days == "1111110"


@pytest.mark.parametrize("bad", ["11111", "11111111", "111111a", "abcdefg"])
def test_resource_calendar_rejects_invalid_work_days(bad):
    with pytest.raises(ValidationError):
        ResourceCalendar(resource_type="crane", work_days=bad)
