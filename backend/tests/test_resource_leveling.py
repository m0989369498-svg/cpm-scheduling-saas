"""資源撫平引擎單元測試 (Resource Leveling engine unit tests).

直接驗證 ``app.core.resource_leveling.level_resources``（純函式，不接觸資料庫）。

涵蓋情境：
  1. 有限資源衝突：兩條並行的「非要徑」任務在同一天爭用同一資源，
     超出資源上限 (max_capacity)。撫平演算法應：
       - 推遲「總時差 (total float) 較小」的可移動任務 (float>0)；
       - 永遠不推遲要徑任務 (float==0，受保護)；
       - 在既有工期內仍有空檔可容納時，不展延總工期 (extended=False)，
         且 extended 旗標與 (leveled_duration > original_duration) 一致。
  2. 無衝突情境：所有任務的資源需求皆在上限內，撫平後工期與各任務 ES/EF
     維持不變 (durations / 時程不被更動)。

設計說明（情境 1 的人工驗算）
------------------------------------------------------------------
資源 "crane"，上限 = 1。

  task    dur  preds        crane
  BACK1    4   []            1
  BACK2    4   [BACK1]       0
  MID      2   []            1
  SHORT    1   []            1

僅依工期的 CPM：
  BACK1: ES0 EF4 ; BACK2: ES4 EF8 -> 專案工期 = 8 (要徑 BACK1->BACK2)
  MID  : ES0 EF2 (佔用第 0,1 天) ; SHORT: ES0 EF1 (佔用第 0 天)
後推 (total=8)：
  BACK1 float0、BACK2 float0  (要徑，受保護)
  MID   float4 ; SHORT float7

第 0 天 crane 需求 = BACK1(1)+MID(1)+SHORT(1) = 3 > 上限 1 -> 超載。
可移動者中 float 較小者為 MID(4) < SHORT(7)，故 MID 先被推遲。
由於 BACK1 在第 0~3 天獨佔 crane，MID/SHORT 只能落在第 4~7 天 (BACK2 為
crane0)；該窗共 4 個 crane-day，足以容納 MID(2)+SHORT(1)=3 個 crane-day，
因此衝突可在「不展延總工期」下解決：leveled_duration 仍為 8、extended=False。
"""

from __future__ import annotations

from app.core.resource_leveling import level_resources
from app.schemas.schedule import TaskDefinition


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _result_by_id(result) -> dict:
    """將 LevelingResult.tasks (list[TaskResult]) 轉為 {task_id: TaskResult}。"""
    return {t.task_id: t for t in result.tasks}


# --------------------------------------------------------------------------- #
# 情境 1：資源衝突 -> 推遲小 float 任務、保護要徑、可在原工期內解決
# --------------------------------------------------------------------------- #
def _conflict_tasks() -> list[TaskDefinition]:
    return [
        TaskDefinition(task_id="BACK1", task_name="要徑前段", duration=4, predecessors=[]),
        TaskDefinition(
            task_id="BACK2", task_name="要徑後段", duration=4, predecessors=["BACK1"]
        ),
        TaskDefinition(task_id="MID", task_name="並行中工項", duration=2, predecessors=[]),
        TaskDefinition(task_id="SHORT", task_name="並行短工項", duration=1, predecessors=[]),
    ]


def _conflict_demands() -> dict[str, dict[str, int]]:
    # BACK1 也需要 crane（要徑任務佔用資源，但永不被推遲）。
    return {
        "BACK1": {"crane": 1},
        "MID": {"crane": 1},
        "SHORT": {"crane": 1},
    }


def _conflict_limits() -> dict[str, int]:
    return {"crane": 1}


def test_original_duration_is_eight():
    """撫平前的原始 CPM 工期應為 8 (要徑 BACK1->BACK2)。"""
    result = level_resources(_conflict_tasks(), _conflict_demands(), _conflict_limits())
    assert result.original_duration == 8


def test_critical_tasks_are_never_pushed():
    """要徑任務 (float==0) 永不被推遲：ES/EF 與要徑身分維持不變。"""
    result = level_resources(_conflict_tasks(), _conflict_demands(), _conflict_limits())
    by_id = _result_by_id(result)

    # BACK1: 原 ES0 EF4，撫平後不得改變
    assert by_id["BACK1"].es == 0
    assert by_id["BACK1"].ef == 4
    assert by_id["BACK1"].is_critical is True

    # BACK2: 原 ES4 EF8，撫平後不得改變
    assert by_id["BACK2"].es == 4
    assert by_id["BACK2"].ef == 8
    assert by_id["BACK2"].is_critical is True


def test_smaller_float_task_is_pushed():
    """衝突解決時，total float 較小的可移動任務 (MID) 會被往後推遲。

    MID 原 ES0；撫平後其 ES 必須 > 0（被推離第 0 天的資源衝突）。
    """
    result = level_resources(_conflict_tasks(), _conflict_demands(), _conflict_limits())
    by_id = _result_by_id(result)
    assert by_id["MID"].es > 0, "較小 float 的 MID 應被推遲 (ES 增加)"


def test_conflict_resolved_within_original_duration():
    """既有工期內有足夠空檔 -> 撫平後不展延、extended 旗標正確。"""
    result = level_resources(_conflict_tasks(), _conflict_demands(), _conflict_limits())

    # extended 旗標必須與工期是否增加一致（契約一致性）。
    assert result.extended == (result.leveled_duration > result.original_duration)

    # 本情境可於原工期 8 內解決 -> 不展延。
    assert result.leveled_duration == result.original_duration == 8
    assert result.extended is False


def test_conflict_timeline_has_no_remaining_over_capacity():
    """成功撫平後，回傳的 over_capacity_days 應為空（無殘留超載日）。"""
    result = level_resources(_conflict_tasks(), _conflict_demands(), _conflict_limits())
    assert result.over_capacity_days == []
    # 既有窗格足以容納，故無無法解決的衝突。
    assert result.unresolved == []


# --------------------------------------------------------------------------- #
# 情境 2：無衝突 -> 時程完全不變
# --------------------------------------------------------------------------- #
def _no_conflict_tasks() -> list[TaskDefinition]:
    # 線性鏈，逐一進行，任一天最多只有一個任務在進行。
    return [
        TaskDefinition(task_id="N-01", task_name="開挖", duration=3, predecessors=[]),
        TaskDefinition(
            task_id="N-02", task_name="結構", duration=5, predecessors=["N-01"]
        ),
        TaskDefinition(
            task_id="N-03", task_name="機電", duration=2, predecessors=["N-02"]
        ),
    ]


def _no_conflict_demands() -> dict[str, dict[str, int]]:
    # 每天最多一個任務在跑，需求恆 <= 上限。
    return {
        "N-01": {"crane": 2, "manpower": 10},
        "N-02": {"crane": 1, "manpower": 15},
        "N-03": {"crane": 2, "manpower": 8},
    }


def _no_conflict_limits() -> dict[str, int]:
    return {"crane": 2, "manpower": 20}


def test_no_conflict_durations_unchanged():
    """無資源衝突：撫平後總工期與原始 CPM 工期相同，且不展延。"""
    result = level_resources(
        _no_conflict_tasks(), _no_conflict_demands(), _no_conflict_limits()
    )
    assert result.original_duration == 10  # 3 + 5 + 2 線性鏈
    assert result.leveled_duration == 10
    assert result.extended is False


def test_no_conflict_schedule_is_identical():
    """無衝突：每個任務的 ES/EF 維持原始 CPM 結果（時程不被更動）。"""
    result = level_resources(
        _no_conflict_tasks(), _no_conflict_demands(), _no_conflict_limits()
    )
    by_id = _result_by_id(result)

    assert (by_id["N-01"].es, by_id["N-01"].ef) == (0, 3)
    assert (by_id["N-02"].es, by_id["N-02"].ef) == (3, 8)
    assert (by_id["N-03"].es, by_id["N-03"].ef) == (8, 10)

    # 既無超載日、亦無未解決衝突。
    assert result.over_capacity_days == []
    assert result.unresolved == []
