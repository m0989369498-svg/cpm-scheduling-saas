"""排程計算路由（Schedule router）。

提供無狀態（不落 DB）的 CPM 計算端點，供前端「重新計算」或試算使用。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from app.deps import verify_tenant, TenantContext
from app.schemas.schedule import TaskDefinition, TaskResult
from app.core.cpm_engine import calculate_cpm

router = APIRouter(prefix="/schedule", tags=["schedule"])


@router.post("/calculate", response_model=list[TaskResult])
async def calculate(
    tasks: list[TaskDefinition],
    ctx: TenantContext = Depends(verify_tenant),
) -> list[TaskResult]:
    """無狀態 CPM 計算：輸入任務清單，回傳含 es/ef/ls/lf/float/critical 的結果。

    不需資料庫；僅驗證 tenant 標頭存在。輸入需可構成有效 DAG，
    否則（環路 / 未知前置任務）回 422。
    """
    if not tasks:
        return []

    try:
        result_map = calculate_cpm(tasks)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"CPM calculation failed: {exc}",
        )

    # 依輸入順序回傳，並補回前置任務 / 名稱 / 狀態（引擎可能不保留這些欄位）
    ordered: list[TaskResult] = []
    for td in tasks:
        res = result_map.get(td.task_id)
        if res is None:
            continue
        res.task_name = td.task_name
        res.predecessors = list(td.predecessors)
        res.status = td.status
        res.duration = td.duration
        ordered.append(res)
    return ordered
