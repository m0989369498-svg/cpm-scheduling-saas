"""資源撫平引擎（Resource-Constrained Scheduling, RCS）。

純函式模組：不接觸資料庫、不依賴外部狀態，方便單元測試與重複呼叫。
重用 ``app.core.cpm_engine`` 進行要徑（CPM）計算。

公開 API：
    level_resources(tasks, demands, limits) -> LevelingResult

演算法（序列式啟發法 / serial heuristic）：
    1. 以原始任務跑一次 CPM，得到每個任務的 es..ef、float（總時差）。
    2. 建立逐日資源負載：對每個任務，於 [es, ef) 區間的每一天，
       將其各項資源需求累加到當日負載。
    3. 由前往後掃描每一天；若該日任一資源總需求 > 上限：
         - 在「當日進行中且 float > 0（非要徑、可移動）」的任務中，
           挑選「總時差最小」者（最接近要徑、最該優先處理）。
         - 對該任務施加一天的合成延遲（synthetic delay）：
           提高其有效最早開始（effective earliest start）+1 天。
         - 以更新後的延遲重跑 CPM，回到步驟 2 重新評估。
       若該日無可移動任務（皆為要徑或 float == 0），則此衝突無法化解，
       記錄為 unresolved，繼續往後掃描。
    4. 重複直到沒有可化解的超載日，或達到反覆運算上限（避免無窮迴圈）。

設計重點：
    - 要徑（float == 0）任務永不被推遲，以維持專案最短工期不被無謂拉長。
    - 合成延遲以「每任務釋放偏移（release offset）」建模：直接委派
      cpm_engine.calculate_cpm(tasks, release_offsets=...)，於前向掃描時對
      該任務的 es 取 max(依賴推導 es, offset)。與正規 CPM 共用同一引擎，
      故完整保留相依型態（FS/SS/FF/SF）、延時（lag）與活動限制
      （constraint_type/constraint_day）語義 —— 撫平後的排程與
      recompute_project / cost / dcma 所見的排程基準一致。
"""

from __future__ import annotations

from collections import defaultdict

from app.core.cpm_engine import calculate_cpm
from app.schemas.analytics import DayLoad, LevelingResult
from app.schemas.schedule import TaskDefinition, TaskResult

# 反覆運算上限保護：避免任何意外情況造成無窮迴圈。
_MAX_ITERATIONS = 10_000


def _cpm_with_offsets(
    tasks: list[TaskDefinition],
    offsets: dict[str, int],
) -> dict[str, TaskResult]:
    """執行帶有「個別最早開始下限（release offset）」的 CPM 計算。

    offsets[task_id] 代表該任務被人為推遲的天數下限：其 es 至少為
    （依賴推導 es）與 offset 兩者取大值。此即合成延遲的建模方式，
    可在不變更相依結構的前提下，將非要徑任務往後挪移。

    直接委派 cpm_engine.calculate_cpm（單一正確實作）：相依型態
    （FS/SS/FF/SF）、延時（lag_days）與活動限制皆與正規 CPM 完全一致。
    回傳 {task_id: TaskResult}。
    """
    return calculate_cpm(tasks, release_offsets=offsets or None)


def _capacity(
    rtype: str,
    day: int,
    limits: dict[str, int],
    availability: dict[str, list[int]] | None,
) -> int:
    """回傳指定資源於指定日的可用產能 (Pro Batch D FEATURE D3)。

    availability 提供且該資源有逐日產能清單、且 day 落在清單範圍內時，
    以該清單值為準 (供每資源專屬工作日曆使用，非其工作日時產能可為 0)；
    否則退回專案層級的純量上限 limits.get(rtype, 0) (向下相容 —— availability
    為 None 時行為與批次前完全一致)。
    """
    if availability:
        day_list = availability.get(rtype)
        if day_list is not None and 0 <= day < len(day_list):
            return day_list[day]
    return limits.get(rtype, 0)


def _build_timeline(
    results: dict[str, TaskResult],
    demands: dict[str, dict[str, int]],
    limits: dict[str, int],
    availability: dict[str, list[int]] | None = None,
) -> tuple[list[DayLoad], list[int]]:
    """依目前排程建立逐日資源負載時間軸，並標記超載日。

    每個任務於 [es, ef) 區間佔用資源；duration == 0 的任務不佔用任何一天。
    回傳 (timeline, over_capacity_days)。
    """
    total_duration = max((r.ef for r in results.values()), default=0)

    # day -> resource_type -> total demand
    day_loads: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for tid, res in results.items():
        need = demands.get(tid)
        if not need:
            continue
        for day in range(res.es, res.ef):  # [es, ef) -> ef - es == duration 天
            for rtype, amount in need.items():
                if amount:
                    day_loads[day][rtype] += amount

    timeline: list[DayLoad] = []
    over_capacity_days: list[int] = []
    for day in range(total_duration):
        loads = {k: v for k, v in day_loads.get(day, {}).items() if v}
        over = any(
            amount > _capacity(rtype, day, limits, availability)
            for rtype, amount in loads.items()
        )
        timeline.append(DayLoad(day=day, loads=loads, over=over))
        if over:
            over_capacity_days.append(day)

    return timeline, over_capacity_days


def _first_over_day(
    results: dict[str, TaskResult],
    demands: dict[str, dict[str, int]],
    limits: dict[str, int],
    availability: dict[str, list[int]] | None = None,
) -> int | None:
    """回傳第一個超載工作日的索引；若無超載則回傳 None。"""
    total_duration = max((r.ef for r in results.values()), default=0)
    for day in range(total_duration):
        loads: dict[str, int] = defaultdict(int)
        for tid, res in results.items():
            if res.es <= day < res.ef:
                need = demands.get(tid)
                if not need:
                    continue
                for rtype, amount in need.items():
                    loads[rtype] += amount
        if any(
            amount > _capacity(rtype, day, limits, availability)
            for rtype, amount in loads.items()
        ):
            return day
    return None


def _movable_task_on_day(
    results: dict[str, TaskResult],
    demands: dict[str, dict[str, int]],
    day: int,
) -> str | None:
    """在指定超載日中，挑選最適合推遲的任務。

    候選條件：
      - 當日進行中（es <= day < ef）。
      - float > 0（非要徑，可移動）。
      - 對該日的任一超載資源確實有需求（推遲它才有意義）。
    選擇：在候選中取「總時差（float_time）最小」者；同分時以 es 較大、
    task_id 較大者為次序，確保決策具決定性（deterministic）。
    回傳 task_id；若無可移動任務則回傳 None。
    """
    candidates: list[TaskResult] = []
    for tid, res in results.items():
        if res.float_time <= 0:
            continue  # 要徑或零時差：永不推遲
        if not (res.es <= day < res.ef):
            continue
        if not demands.get(tid):
            continue  # 無資源需求，推遲它無助於化解衝突
        candidates.append(res)

    if not candidates:
        return None

    # 總時差最小者優先（最接近要徑）；以 (float, -es, task_id) 取得穩定排序。
    candidates.sort(key=lambda r: (r.float_time, -r.es, r.task_id))
    return candidates[0].task_id


def level_resources(
    tasks: list[TaskDefinition],
    demands: dict[str, dict[str, int]],
    limits: dict[str, int],
    availability: dict[str, list[int]] | None = None,
) -> LevelingResult:
    """資源撫平主程序。

    參數：
        tasks    任務定義清單（TaskDefinition）。
        demands  {task_id: {resource_type: amount}} 各任務資源需求。
        limits   {resource_type: max_capacity} 各資源每日上限。
        availability  選填（Pro Batch D FEATURE D3）：{resource_type: [day0產能, day1產能, ...]}。
                 提供時，該資源逐日的可用產能以此清單為準 (供每資源專屬工作
                 日曆使用)；未列出的資源或超出清單範圍的日子，退回 limits 的
                 純量上限。**None（預設，既有所有呼叫端）與批次前行為完全一致**
                 （regression-critical：不得改變既有結果）。
    回傳：
        LevelingResult（含撫平後任務、時間軸、超載日與未解衝突）。

    若無資源需求或無上限，則退化為單純 CPM（不做任何推遲）。
    要徑任務永不被推遲；反覆運算受 _MAX_ITERATIONS 上限保護。
    """
    demands = demands or {}
    limits = limits or {}

    # 原始 CPM（無任何延遲），用以取得原始工期。
    base_results = _cpm_with_offsets(tasks, {})
    original_duration = max((r.ef for r in base_results.values()), default=0)

    # 每任務的累積釋放偏移（人為延遲天數）。
    offsets: dict[str, int] = {}
    results = base_results

    # 無資源需求時無須撫平，直接組裝結果。
    if demands and limits:
        unresolved_set: set[str] = set()
        skip_days: set[int] = set()  # 已判定無法化解的超載日，避免重複處理

        for _ in range(_MAX_ITERATIONS):
            day = _first_over_day(results, demands, limits, availability)
            # 跳過已知無法化解的日子，往後找下一個可處理的超載日。
            while day is not None and day in skip_days:
                day = _next_over_day_after(results, demands, limits, day, availability)
            if day is None:
                break

            tid = _movable_task_on_day(results, demands, day)
            if tid is None:
                # 此日無可移動任務 -> 衝突無法化解；標記後跳過。
                skip_days.add(day)
                _record_unresolved(
                    results, demands, limits, day, unresolved_set, availability
                )
                continue

            # 對選中的任務施加一天合成延遲，重算 CPM。
            offsets[tid] = offsets.get(tid, 0) + 1
            results = _cpm_with_offsets(tasks, offsets)
            # 任務移動後排程改變，先前標記的無解日可能已不同 -> 清空重評估。
            skip_days.clear()
        else:
            # 觸及反覆運算上限（理論上不應發生）：保守地以現況收斂。
            pass

        unresolved = sorted(unresolved_set)
    else:
        unresolved = []

    timeline, over_capacity_days = _build_timeline(
        results, demands, limits, availability
    )
    leveled_duration = max((r.ef for r in results.values()), default=0)

    # 依時間順序輸出任務結果，利於前端甘特圖呈現。
    ordered = sorted(results.values(), key=lambda r: (r.es, r.ef, r.task_id))

    return LevelingResult(
        original_duration=original_duration,
        leveled_duration=leveled_duration,
        extended=leveled_duration > original_duration,
        tasks=ordered,
        timeline=timeline,
        over_capacity_days=over_capacity_days,
        unresolved=unresolved,
    )


def _next_over_day_after(
    results: dict[str, TaskResult],
    demands: dict[str, dict[str, int]],
    limits: dict[str, int],
    after_day: int,
    availability: dict[str, list[int]] | None = None,
) -> int | None:
    """回傳嚴格大於 after_day 的下一個超載工作日；無則回傳 None。"""
    total_duration = max((r.ef for r in results.values()), default=0)
    for day in range(after_day + 1, total_duration):
        loads: dict[str, int] = defaultdict(int)
        for tid, res in results.items():
            if res.es <= day < res.ef:
                need = demands.get(tid)
                if not need:
                    continue
                for rtype, amount in need.items():
                    loads[rtype] += amount
        if any(
            amount > _capacity(rtype, day, limits, availability)
            for rtype, amount in loads.items()
        ):
            return day
    return None


def _record_unresolved(
    results: dict[str, TaskResult],
    demands: dict[str, dict[str, int]],
    limits: dict[str, int],
    day: int,
    unresolved_set: set[str],
    availability: dict[str, list[int]] | None = None,
) -> None:
    """將造成指定超載日（且無法移動）的任務記入 unresolved 集合。

    收錄條件：當日進行中、對超載資源有需求的任務。即使是要徑任務，
    其導致的衝突仍需讓使用者知道（屬於必須以加班 / 加派資源解決者）。
    """
    over_types = set()
    loads: dict[str, int] = defaultdict(int)
    for tid, res in results.items():
        if res.es <= day < res.ef:
            need = demands.get(tid)
            if not need:
                continue
            for rtype, amount in need.items():
                loads[rtype] += amount
    for rtype, amount in loads.items():
        if amount > _capacity(rtype, day, limits, availability):
            over_types.add(rtype)

    for tid, res in results.items():
        if res.es <= day < res.ef:
            need = demands.get(tid)
            if need and over_types.intersection(need.keys()):
                unresolved_set.add(tid)
