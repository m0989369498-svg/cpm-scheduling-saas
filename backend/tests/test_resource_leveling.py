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

from app.core.cpm_engine import calculate_cpm
from app.core.resource_leveling import level_resources
from app.schemas.schedule import DependencyLink, TaskDefinition


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


# --------------------------------------------------------------------------- #
# 情境 3：相依型態 (SS/FF/SF) + 延時 (lag) + 活動限制 —— 撫平引擎的內部
# CPM 必須與 calculate_cpm 完全一致（回歸：Pro Batch D 修正撫平引擎僅支援
# FS+0 的缺陷；否則 availability 陣列（依 calculate_cpm 工期建立）與撫平
# 實際落點錯位，資源日曆會在關鍵日被繞過）。
# --------------------------------------------------------------------------- #
def _typed_link_tasks() -> list[TaskDefinition]:
    # A dur10 (無 crane 需求)；B 以 SS+3 依賴 A（正確語義：B.es = A.es + 3 = 3）。
    # C 以 FS+2 依賴 A（C.es = A.ef + 2 = 12）。
    return [
        TaskDefinition(task_id="A", task_name="主體", duration=10, links=[]),
        TaskDefinition(
            task_id="B",
            task_name="平行起步",
            duration=4,
            links=[DependencyLink(predecessor_task_id="A", dep_type="SS", lag_days=3)],
        ),
        TaskDefinition(
            task_id="C",
            task_name="延時收尾",
            duration=1,
            links=[DependencyLink(predecessor_task_id="A", dep_type="FS", lag_days=2)],
        ),
    ]


def test_leveling_honors_dep_types_and_lags():
    """無資源衝突時，撫平結果的 es/ef 必須與 calculate_cpm 逐一相同
    （SS/lag 不得被降級為 FS+0）。"""
    tasks = _typed_link_tasks()
    demands = {"B": {"crane": 1}}
    limits = {"crane": 5}  # 充裕上限 -> 不觸發任何推遲

    expected = calculate_cpm(tasks)
    result = level_resources(tasks, demands, limits)
    by_id = _result_by_id(result)

    for tid in ("A", "B", "C"):
        assert (by_id[tid].es, by_id[tid].ef) == (
            expected[tid].es,
            expected[tid].ef,
        ), f"{tid} 的排程須與 calculate_cpm 相同"

    # SS+3 的正確落點：B 於 day 3 開始（而非 A 完成後的 day 10）。
    assert (by_id["B"].es, by_id["B"].ef) == (3, 7)
    # FS+2 延時：C 於 day 12 開始。
    assert (by_id["C"].es, by_id["C"].ef) == (12, 13)
    assert result.original_duration == 13
    assert result.leveled_duration == 13
    assert result.extended is False


def test_leveling_honors_activity_constraints():
    """SNET 活動限制（constraint_type/constraint_day）亦須在撫平引擎中生效。"""
    tasks = [
        TaskDefinition(task_id="A", task_name="前段", duration=2, predecessors=[]),
        TaskDefinition(
            task_id="B",
            task_name="限制起步",
            duration=3,
            predecessors=["A"],
            constraint_type="SNET",
            constraint_day=5,
        ),
    ]
    expected = calculate_cpm(tasks)
    result = level_resources(tasks, {"B": {"crane": 1}}, {"crane": 5})
    by_id = _result_by_id(result)

    assert (by_id["B"].es, by_id["B"].ef) == (expected["B"].es, expected["B"].ef)
    assert by_id["B"].es == 5  # SNET day5 生效（依賴推導僅為 day2）
    assert result.leveled_duration == 8


def test_leveling_with_ss_link_still_resolves_conflicts():
    """SS 依賴下的資源衝突仍能以推遲可移動任務化解（撫平邏輯 + 型態語義並存）。"""
    tasks = [
        TaskDefinition(task_id="A", task_name="主體", duration=10, links=[]),
        TaskDefinition(
            task_id="B",
            task_name="平行工項",
            duration=4,
            links=[DependencyLink(predecessor_task_id="A", dep_type="SS", lag_days=3)],
        ),
    ]
    demands = {"A": {"crane": 1}, "B": {"crane": 1}}
    limits = {"crane": 1}  # day3..6 A+B 同日需求 2 > 1 -> B (有 float=3) 被推遲

    result = level_resources(tasks, demands, limits)
    by_id = _result_by_id(result)

    # 要徑 A 不動；B 自 SS 推導的 es=3 (float=3) 被逐日推遲直到 float 用盡
    # (es=6, ef=10)。啟發法「永不展延要徑 / 不推遲 float<=0 任務」-> 剩餘
    # day6..9 衝突無法化解，如實記入 unresolved 與 over_capacity_days。
    assert (by_id["A"].es, by_id["A"].ef) == (0, 10)
    assert by_id["B"].es == 6, "B 應被推遲至 float 用盡處 (SS 語義下的極限)"
    assert result.leveled_duration == result.original_duration == 10
    assert result.extended is False
    assert result.over_capacity_days == [6, 7, 8, 9]
    assert result.unresolved == ["A", "B"]
