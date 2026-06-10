"""蒙地卡羅工期風險模擬引擎（Monte Carlo, PERT-Beta）。

純函式模組：不接觸資料庫、不依賴外部狀態。重用 ``app.core.cpm_engine``
對每一次抽樣的工期重跑 CPM，據以估計專案工期的機率分佈與各任務的
關鍵性指數（criticality index）。

僅使用標準函式庫 ``random``（可設定亂數種子以利測試重現），不依賴 numpy。

公開 API：
    simulate_schedule(tasks, risk, iterations=1000, deadline=None)
        -> SimulationResult

抽樣模型（PERT-Beta）：
    若任務具三點估計 (a, m, b) 且 b > a：
        alpha = 1 + 4 * (m - a) / (b - a)
        beta  = 1 + 4 * (b - m) / (b - a)
        dur   = round(a + random.betavariate(alpha, beta) * (b - a))
    否則（無風險參數或 b == a）：採用任務的基礎工期 duration。

統計輸出：
    s_curve  對 [min..max] 每個工期 d，機率 = P(模擬工期 <= d)（累積、非遞減）。
    mean/std 樣本平均與（母體）標準差。
    p10/p50/p90  以最近秩次法（nearest-rank）取百分位數。
    criticality[t]      = (t 落在要徑的次數) / N。
    on_time_probability = P(模擬工期 <= deadline)，無 deadline 時為 None。
"""

from __future__ import annotations

import math
import random
from collections import defaultdict

from app.core.cpm_engine import calculate_cpm
from app.schemas.analytics import (
    CriticalityItem,
    SCurvePoint,
    SimulationResult,
)
from app.schemas.schedule import TaskDefinition


def _sample_duration(
    rng: random.Random,
    base_duration: int,
    params: tuple[int, int, int] | None,
) -> int:
    """依 PERT-Beta 對單一任務抽樣工期；無有效參數時回傳基礎工期。

    params 為 (optimistic a, most_likely m, pessimistic b)。
    當 b <= a（退化分佈）時無法定義 Beta 形狀參數，直接回傳基礎工期。
    """
    if params is None:
        return base_duration

    a, m, b = params
    if b <= a:
        # 退化：樂觀 == 悲觀，或資料不合理 -> 不抽樣。
        return base_duration

    # 夾擠最可能值至 [a, b]，避免不合理輸入造成負的形狀參數。
    m = min(max(m, a), b)
    span = b - a
    alpha = 1.0 + 4.0 * (m - a) / span
    beta = 1.0 + 4.0 * (b - m) / span
    sampled = a + rng.betavariate(alpha, beta) * span
    dur = round(sampled)
    return max(dur, 0)


def _percentile(sorted_values: list[int], pct: float) -> int:
    """最近秩次法（nearest-rank）百分位數。

    pct 為 0..100。sorted_values 必須已由小到大排序且非空。
    """
    if not sorted_values:
        return 0
    n = len(sorted_values)
    # rank = ceil(pct/100 * n)，夾擠至 [1, n]，再轉 0-based 索引。
    rank = math.ceil(pct / 100.0 * n)
    rank = min(max(rank, 1), n)
    return sorted_values[rank - 1]


def simulate_schedule(
    tasks: list[TaskDefinition],
    risk: dict[str, tuple[int, int, int]],
    iterations: int = 1000,
    deadline: int | None = None,
) -> SimulationResult:
    """執行蒙地卡羅工期模擬。

    參數：
        tasks       任務定義清單（提供相依結構與基礎工期）。
        risk        {task_id: (a, m, b)} 三點估計；缺漏者退回基礎工期。
        iterations  模擬次數（< 1 時夾擠為 1）。
        deadline    合約期限（天）；提供時計算準時完成機率。
    回傳：
        SimulationResult。

    決定性：本函式使用模組級 ``random``（透過 random.Random 實例包裝
    全域狀態），故呼叫端可先 ``random.seed(...)`` 取得可重現結果。
    """
    risk = risk or {}
    iterations = max(int(iterations), 1)

    # 包裝全域 random 狀態：呼叫端 random.seed(...) 即可重現。
    rng = random.Random()
    rng.setstate(random.getstate())

    task_ids = [t.task_id for t in tasks]

    # 空專案：無任何任務 -> 工期恆為 0。
    if not tasks:
        on_time = None if deadline is None else 1.0
        return SimulationResult(
            iterations=iterations,
            mean=0.0,
            std=0.0,
            p10=0,
            p50=0,
            p90=0,
            s_curve=[SCurvePoint(duration=0, probability=1.0)],
            criticality=[],
            deadline=deadline,
            on_time_probability=on_time,
        )

    base_duration = {t.task_id: t.duration for t in tasks}

    durations: list[int] = []
    critical_counts: dict[str, int] = defaultdict(int)

    for _ in range(iterations):
        sampled_tasks: list[TaskDefinition] = []
        for t in tasks:
            dur = _sample_duration(rng, base_duration[t.task_id], risk.get(t.task_id))
            # 以複本套用抽樣工期，保留其餘欄位與相依結構。
            sampled_tasks.append(
                TaskDefinition(
                    task_id=t.task_id,
                    task_name=t.task_name,
                    duration=dur,
                    predecessors=list(t.predecessors),
                    status=t.status,
                )
            )

        results = calculate_cpm(sampled_tasks)
        proj_dur = max((r.ef for r in results.values()), default=0)
        durations.append(proj_dur)

        for tid, res in results.items():
            if res.is_critical:
                critical_counts[tid] += 1

    # 將全域 random 狀態回寫，使外部觀察到的消耗與本次抽樣一致。
    random.setstate(rng.getstate())

    n = len(durations)
    durations_sorted = sorted(durations)

    mean = sum(durations) / n
    variance = sum((d - mean) ** 2 for d in durations) / n  # 母體變異數
    std = math.sqrt(variance)

    p10 = _percentile(durations_sorted, 10)
    p50 = _percentile(durations_sorted, 50)
    p90 = _percentile(durations_sorted, 90)

    # ---- S 曲線：對 [min..max] 每個工期計算累積機率 P(dur <= d) ----
    d_min = durations_sorted[0]
    d_max = durations_sorted[-1]
    # 預先統計各工期出現次數，再做前綴和，避免 O(range * N)。
    freq: dict[int, int] = defaultdict(int)
    for d in durations:
        freq[d] += 1
    s_curve: list[SCurvePoint] = []
    cumulative = 0
    for d in range(d_min, d_max + 1):
        cumulative += freq.get(d, 0)
        prob = cumulative / n
        # 數值保險：夾擠至 [0, 1]。
        prob = min(max(prob, 0.0), 1.0)
        s_curve.append(SCurvePoint(duration=d, probability=prob))

    # ---- 關鍵性指數：依任務原始順序輸出 ----
    criticality = [
        CriticalityItem(task_id=tid, index=critical_counts.get(tid, 0) / n)
        for tid in task_ids
    ]

    # ---- 準時完成機率 ----
    if deadline is None:
        on_time_probability = None
    else:
        on_time_count = sum(1 for d in durations if d <= deadline)
        on_time_probability = on_time_count / n

    return SimulationResult(
        iterations=n,
        mean=mean,
        std=std,
        p10=p10,
        p50=p50,
        p90=p90,
        s_curve=s_curve,
        criticality=criticality,
        deadline=deadline,
        on_time_probability=on_time_probability,
    )
