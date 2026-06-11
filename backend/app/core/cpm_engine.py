"""要徑法引擎（Critical Path Method, CPM）。

純函式模組：不接觸資料庫、不依賴任何外部狀態，方便單元測試與重複呼叫。

公開 API：
    calculate_cpm(tasks)      前向 + 後向計算，回傳 {task_id: TaskResult}
    project_duration(task_map) 專案總工期（= 最大 ef）
    critical_path(task_map)   依拓樸順序回傳要徑上的 task_id 清單

相依模型（dependency model）：
    每個任務可帶 links（DependencyLink 清單），支援四種相依型態
    （FS / SS / FF / SF）與延時（lag_days，可為負值 = lead）。
    links 為 None 時，由 predecessors 推導為傳統 FS + lag 0
    （完全向下相容 / backward compatible）。

演算法：採用 Kahn 拓樸排序（topological sort，與相依型態無關）。
    前向掃描（forward pass）：
        es = max(0, max over 入邊約束)：
            FS: pred.ef + lag
            SS: pred.es + lag
            FF: pred.ef + lag - succ.duration
            SF: pred.es + lag - succ.duration
        ef = es + duration
    後向掃描（backward pass，反向拓樸順序）：
        每條出邊對前置任務（pred）給出 lf 上界（bound）：
            FS: succ.ls - lag
            SS: succ.ls - lag + pred.duration
            FF: succ.lf - lag
            SF: succ.lf - lag + pred.duration
        lf = min(各 bound，預設/上限為專案總工期)
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

from app.schemas.schedule import (
    DEP_TYPE_VALUES,
    DependencyLink,
    TaskDefinition,
    TaskResult,
)

# 約束邊（constraint edge）：(前置 task_id, 後繼 task_id, 相依型態, 延時天數)
_Edge = tuple[str, str, str, int]


def _build_node_map(tasks: list[TaskDefinition]) -> dict[str, TaskDefinition]:
    """建立 {task_id: TaskDefinition} 索引；驗證重複 task_id。"""
    node_map: dict[str, TaskDefinition] = {}
    for task in tasks:
        if task.task_id in node_map:
            raise ValueError(f"重複的 task_id（duplicate task_id）：{task.task_id}")
        node_map[task.task_id] = task
    return node_map


def _normalized_links(task: TaskDefinition) -> list[DependencyLink]:
    """取得任務的相依連結（dependency links）。

    links 為 None 時由 predecessors 推導為傳統 FS + lag 0（向下相容）；
    links 已提供時（含空清單）以 links 為準、忽略 predecessors。
    """
    if task.links is not None:
        return list(task.links)
    return [DependencyLink(predecessor_task_id=p) for p in task.predecessors]


def _collect_edges(
    tasks: list[TaskDefinition],
    node_map: dict[str, TaskDefinition],
) -> list[_Edge]:
    """收集並驗證所有約束邊（constraint edges）。

    驗證：自我相依（self-dependency）、未知前置任務（unknown predecessor）、
    不支援的相依型態（dep_type）。同一任務上完全相同的 (前置, 型態, 延時)
    三元組去重（與舊版 predecessors 去重行為一致）。
    """
    edges: list[_Edge] = []
    for task in tasks:
        seen: set[tuple[str, str, int]] = set()
        for link in _normalized_links(task):
            pred = link.predecessor_task_id
            dep_type = link.dep_type
            lag = link.lag_days
            if pred == task.task_id:
                raise ValueError(
                    f"任務不可將自己列為前置（self-dependency）：{task.task_id}"
                )
            if pred not in node_map:
                raise ValueError(
                    f"未知的前置任務（unknown predecessor）：{pred} "
                    f"（被 {task.task_id} 參照）"
                )
            if dep_type not in DEP_TYPE_VALUES:
                raise ValueError(
                    f"不支援的相依型態（unsupported dep_type）：{dep_type}"
                    f"（任務 {task.task_id}）"
                )
            key = (pred, dep_type, lag)
            if key in seen:
                continue
            seen.add(key)
            edges.append((pred, task.task_id, dep_type, lag))
    return edges


def _node_graph_from_edges(
    node_map: dict[str, TaskDefinition],
    edges: list[_Edge],
) -> tuple[dict[str, list[str]], dict[str, list[str]], dict[str, int]]:
    """由約束邊建立節點層級（node-level）相依圖。

    同一對 (pred, succ) 之間即使有多條不同型態的約束邊，
    在節點圖中只計一次（拓樸排序 / 入度以節點對為準）。
    """
    successors: dict[str, list[str]] = defaultdict(list)
    predecessors: dict[str, list[str]] = defaultdict(list)
    indegree: dict[str, int] = {tid: 0 for tid in node_map}

    seen_pairs: set[tuple[str, str]] = set()
    for pred, succ, _dep_type, _lag in edges:
        if (pred, succ) in seen_pairs:
            continue
        seen_pairs.add((pred, succ))
        successors[pred].append(succ)
        predecessors[succ].append(pred)
        indegree[succ] += 1

    return successors, predecessors, indegree


def _build_graph(
    tasks: list[TaskDefinition],
) -> tuple[
    dict[str, TaskDefinition],
    dict[str, list[str]],
    dict[str, list[str]],
    dict[str, int],
]:
    """建立任務索引與相依圖（節點層級；維持既有介面供外部模組使用）。

    回傳：
        node_map   {task_id: TaskDefinition}
        successors {task_id: [後繼 task_id, ...]}
        predecessors {task_id: [前置 task_id, ...]}（去重後）
        indegree   {task_id: 入度}
    同時驗證：重複 task_id、未知前置任務、自我相依、相依型態。
    相依來源：links（含型態/延時）優先；links 為 None 時以 predecessors
    推導為 FS + 0，行為與舊版完全一致。
    """
    node_map = _build_node_map(tasks)
    edges = _collect_edges(tasks, node_map)
    successors, predecessors, indegree = _node_graph_from_edges(node_map, edges)
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

    支援四種相依型態（FS/SS/FF/SF）與延時（lag，可為負）；
    沒有 links 的任務以 predecessors 視為 FS + 0（向下相容）。
    """
    if not tasks:
        return {}

    node_map = _build_node_map(tasks)
    edges = _collect_edges(tasks, node_map)
    successors, predecessors, indegree = _node_graph_from_edges(node_map, edges)
    order = _topological_order(node_map, successors, indegree)

    # 依方向索引約束邊，供前向 / 後向掃描使用
    in_edges: dict[str, list[tuple[str, str, int]]] = defaultdict(list)
    out_edges: dict[str, list[tuple[str, str, int]]] = defaultdict(list)
    for pred, succ, dep_type, lag in edges:
        in_edges[succ].append((pred, dep_type, lag))
        out_edges[pred].append((succ, dep_type, lag))

    # ---- 前向掃描（forward pass）：計算 es / ef ----
    es: dict[str, int] = {}
    ef: dict[str, int] = {}
    for tid in order:
        duration = node_map[tid].duration
        earliest = 0  # es 下限為 0：負 lag（lead）不得使任務早於專案開始
        for pred, dep_type, lag in in_edges.get(tid, ()):
            if dep_type == "FS":
                bound = ef[pred] + lag
            elif dep_type == "SS":
                bound = es[pred] + lag
            elif dep_type == "FF":
                bound = ef[pred] + lag - duration
            else:  # SF
                bound = es[pred] + lag - duration
            if bound > earliest:
                earliest = bound
        es[tid] = earliest
        ef[tid] = earliest + duration

    total_duration = max(ef.values(), default=0)

    # ---- 後向掃描（backward pass）：計算 lf / ls / float ----
    lf: dict[str, int] = {}
    ls: dict[str, int] = {}
    for tid in reversed(order):
        duration = node_map[tid].duration
        # lf 上限為專案總工期（終端任務即等於專案總工期）
        latest = total_duration
        for succ, dep_type, lag in out_edges.get(tid, ()):
            if dep_type == "FS":
                bound = ls[succ] - lag
            elif dep_type == "SS":
                bound = ls[succ] - lag + duration
            elif dep_type == "FF":
                bound = lf[succ] - lag
            else:  # SF
                bound = lf[succ] - lag + duration
            if bound < latest:
                latest = bound
        lf[tid] = latest
        ls[tid] = latest - duration

    # ---- 組裝結果（保留前置去重後的順序）----
    results: dict[str, TaskResult] = {}
    for tid in order:
        definition = node_map[tid]
        float_time = ls[tid] - es[tid]
        if definition.links is not None:
            # 有 links：predecessors 由節點圖重新推導（去重），links 複本回傳
            preds_out = list(predecessors.get(tid, []))
            links_out = [link.model_copy() for link in definition.links]
        else:
            # 舊式輸入：維持原 predecessors 內容與順序（向下相容）
            preds_out = list(definition.predecessors)
            links_out = None
        results[tid] = TaskResult(
            task_id=definition.task_id,
            task_name=definition.task_name,
            duration=definition.duration,
            predecessors=preds_out,
            links=links_out,
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
