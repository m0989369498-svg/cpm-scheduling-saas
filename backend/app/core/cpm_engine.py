"""要徑法引擎（Critical Path Method, CPM）。

純函式模組：不接觸資料庫、不依賴任何外部狀態，方便單元測試與重複呼叫。

公開 API：
    calculate_cpm(tasks)      前向 + 後向計算，回傳 {task_id: TaskResult}
    project_duration(task_map) 專案總工期（= 最大 ef）
    critical_path(task_map)   依拓樸順序回傳要徑上的 task_id 清單

演算法：採用 Kahn 拓樸排序（topological sort）。
    前向掃描（forward pass）：
        es = max(前置任務的 ef)，無前置者 es = 0
        ef = es + duration
    後向掃描（backward pass，反向拓樸順序）：
        終端任務（無後繼）lf = 專案總工期
        其餘 lf = min(後繼任務的 ls)
        ls = lf - duration
        float_time = ls - es
        is_critical = (float_time == 0)

健全性（robustness）：
    - 偵測循環相依（cycle）：拓樸排序消化的節點數 < N 時拋出 ValueError
    - 偵測未知前置任務（unknown predecessor）：拋出 ValueError
    - 空輸入回傳 {}
"""

from __future__ import annotations

from collections import defaultdict, deque

from app.schemas.schedule import TaskDefinition, TaskResult


def _build_graph(
    tasks: list[TaskDefinition],
) -> tuple[
    dict[str, TaskDefinition],
    dict[str, list[str]],
    dict[str, list[str]],
    dict[str, int],
]:
    """建立任務索引與相依圖。

    回傳：
        node_map   {task_id: TaskDefinition}
        successors {task_id: [後繼 task_id, ...]}
        predecessors {task_id: [前置 task_id, ...]}（去重後）
        indegree   {task_id: 入度}
    同時驗證：重複 task_id、未知前置任務。
    """
    node_map: dict[str, TaskDefinition] = {}
    for task in tasks:
        if task.task_id in node_map:
            raise ValueError(f"重複的 task_id（duplicate task_id）：{task.task_id}")
        node_map[task.task_id] = task

    successors: dict[str, list[str]] = defaultdict(list)
    predecessors: dict[str, list[str]] = defaultdict(list)
    indegree: dict[str, int] = {tid: 0 for tid in node_map}

    for task in tasks:
        # 去重，避免同一前置被重複計入入度而破壞拓樸排序
        seen: set[str] = set()
        for pred in task.predecessors:
            if pred == task.task_id:
                raise ValueError(
                    f"任務不可將自己列為前置（self-dependency）：{task.task_id}"
                )
            if pred not in node_map:
                raise ValueError(
                    f"未知的前置任務（unknown predecessor）：{pred} "
                    f"（被 {task.task_id} 參照）"
                )
            if pred in seen:
                continue
            seen.add(pred)
            successors[pred].append(task.task_id)
            predecessors[task.task_id].append(pred)
            indegree[task.task_id] += 1

    return node_map, successors, predecessors, indegree


def _topological_order(
    node_map: dict[str, TaskDefinition],
    successors: dict[str, list[str]],
    indegree: dict[str, int],
) -> list[str]:
    """Kahn 拓樸排序；若無法消化所有節點代表存在循環相依。"""
    # 以複本操作，保留原始入度供後續使用
    degree = dict(indegree)
    queue: deque[str] = deque(tid for tid, d in degree.items() if d == 0)
    order: list[str] = []

    while queue:
        current = queue.popleft()
        order.append(current)
        for nxt in successors[current]:
            degree[nxt] -= 1
            if degree[nxt] == 0:
                queue.append(nxt)

    if len(order) < len(node_map):
        # 拓樸排序未消化全部節點 => 圖中存在環
        remaining = sorted(set(node_map) - set(order))
        raise ValueError(
            "偵測到循環相依（cycle detected），涉及任務："
            + ", ".join(remaining)
        )

    return order


def calculate_cpm(tasks: list[TaskDefinition]) -> dict[str, TaskResult]:
    """執行 CPM 前向 + 後向計算，回傳每個任務的計算結果。

    參數 tasks：任務定義清單（TaskDefinition）。
    回傳：{task_id: TaskResult}。空輸入回傳 {}。
    """
    if not tasks:
        return {}

    node_map, successors, predecessors, indegree = _build_graph(tasks)
    order = _topological_order(node_map, successors, indegree)

    # ---- 前向掃描（forward pass）：計算 es / ef ----
    es: dict[str, int] = {}
    ef: dict[str, int] = {}
    for tid in order:
        preds = predecessors[tid]
        es[tid] = max((ef[p] for p in preds), default=0)
        ef[tid] = es[tid] + node_map[tid].duration

    total_duration = max(ef.values(), default=0)

    # ---- 後向掃描（backward pass）：計算 lf / ls / float ----
    lf: dict[str, int] = {}
    ls: dict[str, int] = {}
    for tid in reversed(order):
        succs = successors[tid]
        if not succs:
            # 終端任務（sink）：最晚完成 = 專案總工期
            lf[tid] = total_duration
        else:
            lf[tid] = min(ls[s] for s in succs)
        ls[tid] = lf[tid] - node_map[tid].duration

    # ---- 組裝結果（保留前置去重後的順序）----
    results: dict[str, TaskResult] = {}
    for tid in order:
        definition = node_map[tid]
        float_time = ls[tid] - es[tid]
        results[tid] = TaskResult(
            task_id=definition.task_id,
            task_name=definition.task_name,
            duration=definition.duration,
            predecessors=list(definition.predecessors),
            status=definition.status,
            es=es[tid],
            ef=ef[tid],
            ls=ls[tid],
            lf=lf[tid],
            float_time=float_time,
            is_critical=(float_time == 0),
        )

    return results


def project_duration(task_map: dict[str, TaskResult]) -> int:
    """回傳專案總工期（= 所有任務 ef 的最大值）。空圖回傳 0。"""
    return max((r.ef for r in task_map.values()), default=0)


def critical_path(task_map: dict[str, TaskResult]) -> list[str]:
    """回傳要徑（critical path）上的 task_id 清單，依拓樸/時間順序排列。

    要徑定義為 float_time == 0 的任務。為了得到合理且穩定的順序，
    依 (es, ef, task_id) 排序。
    """
    critical = [r for r in task_map.values() if r.is_critical]
    critical.sort(key=lambda r: (r.es, r.ef, r.task_id))
    return [r.task_id for r in critical]
