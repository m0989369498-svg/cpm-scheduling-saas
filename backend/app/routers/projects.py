"""專案路由（Projects router）。

職責：
  - 專案 CRUD（建立 / 查詢 / 更新中繼資料 / 軟刪除）
  - 回收桶（recycle bin）：/trash 清單、還原 (restore)、永久刪除 (purge)（admin）
  - 工作日曆：專案假日 (holidays) 管理 + day_dates（offset -> ISO 日期）輸出
  - 共用的 recompute_project() 助手：載入 DAG -> 跑 CPM -> 回寫 es/ef/ls/lf/float_time/is_critical -> 回傳 ProjectOut
  - PDF 報表串流（/report）

recompute_project() 為全系統共用的重算入口，create / add-task / update / duration / delete
等路徑都會呼叫它，確保 CPM 結果與 DB 一致，避免重複實作。
Batch 3：每次重算將 project.version +1（FEAT-3 樂觀併發）。

路由順序注意：/projects/trash 必須宣告於 /projects/{project_id} 之前，
否則 "trash" 會被當成 project_id 匹配。
"""

from __future__ import annotations

import functools
import io
import uuid
import logging
from datetime import date, datetime, timezone

import anyio
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import func, select, delete as sa_delete, update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from app.deps import verify_tenant, get_db, TenantContext, require_role
from app.models.orm import (
    Project,
    ProjectHoliday,
    Task,
    TaskDependency,
    WbsNode as WbsNodeOrm,
)
from app.schemas.schedule import (
    DependencyLink,
    HolidayEntry,
    ProjectCreate,
    ProjectOut,
    ProjectSummary,
    ProjectUpdate,
    TaskDefinition,
    TaskResult,
    WbsNode,
)
from app.core import workcal
from app.core.cpm_engine import calculate_cpm, project_duration, critical_path
from app.core import audit
from app.core.httputil import safe_filename
from app.automation import reports

logger = logging.getLogger("cpm.routers.projects")

router = APIRouter(prefix="/projects", tags=["projects"])

# FEAT-3 樂觀併發：版本衝突時回應的訊息（前端據此提示並重載專案）。
VERSION_CONFLICT_DETAIL = "版本衝突：專案已被其他人修改"


# ---------------------------------------------------------------------------
# 內部工具：載入 / 組裝 / 回寫
# ---------------------------------------------------------------------------
def version_conflict_response(
    project: Project, expected_version: int | None
) -> JSONResponse | None:
    """FEAT-3 樂觀併發檢查（optimistic concurrency check）。

    expected_version 為 None（未提供）=> 不檢查（向下相容），回 None。
    與當前 project.version 相符 => 回 None（放行）。
    不符 => 回 409 JSONResponse，body 形狀（契約）：
      {"detail": "版本衝突：專案已被其他人修改", "current_version": N}
    呼叫端（端點）應 `if conflict is not None: return conflict`。
    """
    if expected_version is None:
        return None
    current = int(project.version or 0)
    if int(expected_version) == current:
        return None
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={"detail": VERSION_CONFLICT_DETAIL, "current_version": current},
    )


async def _get_project_or_404(
    db: AsyncSession,
    project_id: str,
    tenant_id: str | None = None,
    *,
    include_deleted: bool = False,
) -> Project:
    """取得專案 ORM 物件，找不到回 404。

    租戶範圍：在 PostgreSQL 由 RLS (app.current_tenant) 強制隔離；在 sqlite
    (dev 原生模式，無 RLS) 則改以「應用層查詢條件」隔離 —— 故當呼叫端提供
    tenant_id 時，明確加上 ``Project.tenant_id == tenant_id``，確保兩種後端
    皆只回傳當前租戶的專案 (避免 sqlite 下跨租戶讀取)。

    軟刪除（FEAT-4）：預設過濾 ``deleted_at IS NULL``（已進回收桶的專案視同
    不存在 -> 404）；僅回收桶相關端點（restore / purge）以 include_deleted=True
    取得已刪除專案。
    """
    stmt = select(Project).where(Project.project_id == project_id)
    if tenant_id is not None:
        stmt = stmt.where(Project.tenant_id == tenant_id)
    if not include_deleted:
        stmt = stmt.where(Project.deleted_at.is_(None))
    result = await db.execute(stmt)
    project = result.scalar_one_or_none()
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Project '{project_id}' not found",
        )
    return project


async def _load_tasks(db: AsyncSession, project_id: str) -> list[Task]:
    """載入專案所有任務（依 task_id 排序，輸出穩定）。"""
    result = await db.execute(
        select(Task).where(Task.project_id == project_id).order_by(Task.task_id)
    )
    return list(result.scalars().all())


async def _load_dependencies(db: AsyncSession, project_id: str) -> list[TaskDependency]:
    """載入專案所有相依關係。"""
    result = await db.execute(
        select(TaskDependency).where(TaskDependency.project_id == project_id)
    )
    return list(result.scalars().all())


async def _load_holidays(db: AsyncSession, project_id: str) -> list[ProjectHoliday]:
    """載入專案所有例外假日（依日期排序，輸出穩定）。"""
    result = await db.execute(
        select(ProjectHoliday)
        .where(ProjectHoliday.project_id == project_id)
        .order_by(ProjectHoliday.holiday_date)
    )
    return list(result.scalars().all())


async def _load_holiday_dates(db: AsyncSession, project_id: str) -> set[date]:
    """載入專案例外假日的日期集合（供工作日曆換算）。"""
    return {h.holiday_date for h in await _load_holidays(db, project_id)}


async def _load_wbs_nodes(db: AsyncSession, project_id: str) -> list[WbsNodeOrm]:
    """載入專案 WBS 節點（依 sort_order、wbs_code 排序，輸出穩定）。"""
    result = await db.execute(
        select(WbsNodeOrm)
        .where(WbsNodeOrm.project_id == project_id)
        .order_by(WbsNodeOrm.sort_order, WbsNodeOrm.wbs_code)
    )
    return list(result.scalars().all())


def _wbs_nodes_to_schema(rows: list[WbsNodeOrm]) -> list[WbsNode]:
    """WBS 節點 ORM 列 -> Pydantic（GET /wbs 與 ProjectOut.wbs 共用）。"""
    return [
        WbsNode(
            wbs_code=r.wbs_code,
            name=r.name or "",
            parent_code=r.parent_code,
            sort_order=int(r.sort_order or 0),
        )
        for r in rows
    ]


def _validate_wbs_tree(nodes: list[WbsNode]) -> None:
    """驗證 WBS 扁平清單（寫入前）：失敗一律 422，且呼叫端不得寫入任何列。

    規則：
      1) wbs_code 於清單內唯一。
      2) parent_code 為 None，或參照清單內某個 wbs_code（不可懸空指向清單外）。
      3) 不得構成循環（cycle）：由任一節點沿 parent_code 往上追溯不得重見已走過節點。
    """
    codes = [n.wbs_code for n in nodes]
    if len(set(codes)) != len(codes):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="WBS 代碼重複（duplicate wbs_code）",
        )
    code_set = set(codes)
    parent_map: dict[str, str | None] = {}
    for n in nodes:
        if n.parent_code is not None and n.parent_code not in code_set:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"parent_code 參照不存在的節點（unknown parent_code）："
                    f"{n.parent_code}（節點 {n.wbs_code}）"
                ),
            )
        parent_map[n.wbs_code] = n.parent_code

    for start in codes:
        visited: set[str] = set()
        current: str | None = start
        while current is not None:
            if current in visited:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"WBS 階層存在循環（cycle detected）：{start}",
                )
            visited.add(current)
            current = parent_map.get(current)


async def _task_aggregates(
    db: AsyncSession, project_ids: list[str]
) -> dict[str, tuple[int, int]]:
    """以「單一」彙總查詢取得每專案的 (task_count, max_ef)。

    PERF-1: 取代逐專案 hydrate 全部 Task ORM 列（僅為了 count / max(ef)），
    專案數 N 時由 N 次查詢降為 1 次 GROUP BY。回傳 {project_id: (count, max_ef)}；
    無任務的專案不在結果中（呼叫端以 (0, 0) 預設）。
    """
    if not project_ids:
        return {}
    result = await db.execute(
        select(
            Task.project_id,
            func.count().label("task_count"),
            func.max(Task.ef).label("max_ef"),
        )
        .where(Task.project_id.in_(project_ids))
        .group_by(Task.project_id)
    )
    return {
        row.project_id: (int(row.task_count or 0), int(row.max_ef or 0))
        for row in result.all()
    }


def _dependency_maps(
    deps: list[TaskDependency],
) -> tuple[dict[str, list[str]], dict[str, list[DependencyLink]]]:
    """由相依 ORM 列組出兩種視圖（FEAT-1）：

    pred_map  {task_id: [predecessor_task_id, ...]}  傳統前置清單（向下相容）。
    links_map {task_id: [DependencyLink, ...]}       帶 dep_type / lag_days 的連結。
    """
    pred_map: dict[str, list[str]] = {}
    links_map: dict[str, list[DependencyLink]] = {}
    for d in deps:
        pred_map.setdefault(d.task_id, []).append(d.predecessor_task_id)
        links_map.setdefault(d.task_id, []).append(
            DependencyLink(
                predecessor_task_id=d.predecessor_task_id,
                dep_type=(d.dep_type or "FS"),
                lag_days=int(d.lag_days or 0),
            )
        )
    return pred_map, links_map


def _build_task_definitions(
    tasks: list[Task], deps: list[TaskDependency]
) -> list[TaskDefinition]:
    """將 ORM 任務 + 相依 組成 CPM 引擎用的 TaskDefinition 清單。

    FEAT-1：同時填入 predecessors（推導，向下相容）與 links（dep_type/lag_days），
    使 CPM 引擎（以及重用本助手的 resource_leveling / monte_carlo）取得完整
    的相依語義。
    Batch 5：一併帶入 wbs_code（WBS 歸屬，不影響計算）與 constraint_type /
    constraint_day（活動限制，供引擎套用；皆為 None 時行為不變）。
    """
    pred_map, links_map = _dependency_maps(deps)

    definitions: list[TaskDefinition] = []
    for t in tasks:
        definitions.append(
            TaskDefinition(
                task_id=t.task_id,
                task_name=t.task_name or "",
                duration=t.duration or 0,
                predecessors=pred_map.get(t.task_id, []),
                links=links_map.get(t.task_id, []),
                status=t.status or "PENDING",
                wbs_code=t.wbs_code,
                constraint_type=t.constraint_type,
                constraint_day=t.constraint_day,
            )
        )
    return definitions


def _to_project_out(
    project: Project,
    tasks: list[Task],
    deps: list[TaskDependency],
    task_results: dict[str, TaskResult],
    duration: int,
    holidays: set[date] | None = None,
    wbs_nodes: list[WbsNodeOrm] | None = None,
) -> ProjectOut:
    """組裝 ProjectOut（合併 CPM 計算結果）。

    FEAT-1：任務一併輸出 links（前端據此繪製依賴箭頭 / 編輯依賴）。
    FEAT-2：start_date 已設定時輸出 day_dates（offset 0..duration 的 ISO 日期，
            以 work_days 工作日曆 + 例外假日換算）。
    FEAT-3：輸出 version 供客戶端樂觀併發。
    Batch 5：輸出 wbs（專案 WBS 節點扁平清單）；任務一併輸出 wbs_code /
             constraint_type / constraint_day / constraint_violated。
    """
    pred_map, links_map = _dependency_maps(deps)

    out_tasks: list[TaskResult] = []
    for t in tasks:
        res = task_results.get(t.task_id)
        if res is not None:
            # 以引擎結果為準，但補回相依與名稱/狀態/WBS 歸屬（皆非 CPM 計算欄位，
            # 以 DB 為準，與 predecessors/status 相同處理方式）。
            res.task_name = t.task_name or ""
            res.predecessors = pred_map.get(t.task_id, [])
            res.links = links_map.get(t.task_id, [])
            res.status = t.status or "PENDING"
            res.wbs_code = t.wbs_code
            res.constraint_type = t.constraint_type
            res.constraint_day = t.constraint_day
            out_tasks.append(res)
        else:
            out_tasks.append(
                TaskResult(
                    task_id=t.task_id,
                    task_name=t.task_name or "",
                    duration=t.duration or 0,
                    predecessors=pred_map.get(t.task_id, []),
                    links=links_map.get(t.task_id, []),
                    status=t.status or "PENDING",
                    wbs_code=t.wbs_code,
                    constraint_type=t.constraint_type,
                    constraint_day=t.constraint_day,
                    es=t.es or 0,
                    ef=t.ef or 0,
                    ls=t.ls or 0,
                    lf=t.lf or 0,
                    float_time=t.float_time or 0,
                    is_critical=bool(t.is_critical),
                    constraint_violated=bool(t.constraint_violated),
                )
            )

    # FEAT-2：開工日期已設定時，輸出每個 offset 對應的 ISO 日期。
    work_days = project.work_days or "1111110"
    day_dates_out: list[str] | None = None
    if project.start_date is not None:
        day_dates_out = [
            d.isoformat()
            for d in workcal.day_dates(
                project.start_date, duration, work_days, holidays or set()
            )
        ]

    return ProjectOut(
        project_id=project.project_id,
        tenant_id=project.tenant_id,
        project_name=project.project_name,
        region=project.region,
        project_duration=duration,
        tasks=out_tasks,
        start_date=project.start_date,
        work_days=work_days,
        version=int(project.version or 0),
        day_dates=day_dates_out,
        wbs=_wbs_nodes_to_schema(wbs_nodes or []),
    )


# ---------------------------------------------------------------------------
# 共用重算助手（全系統共用）
# ---------------------------------------------------------------------------
async def recompute_project(
    db: AsyncSession,
    project: Project,
    *,
    notify: bool = True,
) -> ProjectOut:
    """載入專案 DAG -> 跑 CPM -> 回寫每個任務的 es/ef/ls/lf/float_time/is_critical -> 回傳 ProjectOut。

    這是 create / add-task / update / duration / delete 等路徑共用的重算入口。
    CPM 為純函式（無 DB），此處負責持久化結果。

    FEAT-3：每次重算將 project.version +1（任何排程寫入路徑都視為一次修改），
    供客戶端以 expected_version 做樂觀併發比對。

    notify: 已保留作呼叫端相容；FIX-4 後不再於請求交易內阻斷式發送通知 (no-op)。
    """
    tasks = await _load_tasks(db, project.project_id)
    deps = await _load_dependencies(db, project.project_id)

    # FEAT-3：版本 +1（含「刪到清空」等空專案路徑，仍屬一次修改）。
    project.version = int(project.version or 0) + 1

    # FEAT-2：開工日期已設定時，載入例外假日供 day_dates 換算。
    holidays: set[date] = set()
    if project.start_date is not None:
        holidays = await _load_holiday_dates(db, project.project_id)

    # Batch 5：載入 WBS 節點供 ProjectOut.wbs 輸出。
    wbs_nodes = await _load_wbs_nodes(db, project.project_id)

    definitions = _build_task_definitions(tasks, deps)

    # 空專案：不需計算
    if not definitions:
        await db.flush()
        return _to_project_out(
            project, tasks, deps, {}, 0, holidays=holidays, wbs_nodes=wbs_nodes
        )

    try:
        task_results = calculate_cpm(definitions)
    except ValueError as exc:
        # 環路或未知前置任務 -> 422（資料無法構成有效 DAG）
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"CPM calculation failed: {exc}",
        )

    duration = project_duration(task_results)

    # PERF-2 回寫 CPM 結果：
    #   (a) skip-no-op —— 僅收集 es/ef/ls/lf/float_time/is_critical「實際變動」的列；
    #   (b) 以「單一」bulk UPDATE（SQLAlchemy 2.0 executemany-by-pk）持久化，
    #       取代逐列 attribute 指派（unit-of-work 每列發一句 UPDATE）。
    # in-session 物件以 set_committed_value 同步新值（不標記 dirty，避免
    # unit-of-work 重複 UPDATE），使同一 session 後續讀取（identity map）正確。
    changed: list[dict] = []
    for t in tasks:
        res = task_results.get(t.task_id)
        if res is None:
            continue
        if (
            (t.es or 0) == int(res.es)
            and (t.ef or 0) == int(res.ef)
            and (t.ls or 0) == int(res.ls)
            and (t.lf or 0) == int(res.lf)
            and (t.float_time or 0) == int(res.float_time)
            and bool(t.is_critical) == bool(res.is_critical)
            and bool(t.constraint_violated) == bool(res.constraint_violated)
        ):
            continue
        changed.append(
            {
                "id": t.id,
                "es": int(res.es),
                "ef": int(res.ef),
                "ls": int(res.ls),
                "lf": int(res.lf),
                "float_time": int(res.float_time),
                "is_critical": bool(res.is_critical),
                # Batch 5 FEAT-2：限制衝突（float_time < 0）隨重算持久化，
                # 與 is_critical 同模式，讀取路徑無須重算即可得知。
                "constraint_violated": bool(res.constraint_violated),
            }
        )
        set_committed_value(t, "es", int(res.es))
        set_committed_value(t, "ef", int(res.ef))
        set_committed_value(t, "ls", int(res.ls))
        set_committed_value(t, "lf", int(res.lf))
        set_committed_value(t, "float_time", int(res.float_time))
        set_committed_value(t, "is_critical", bool(res.is_critical))
        set_committed_value(t, "constraint_violated", bool(res.constraint_violated))

    if changed:
        await db.execute(sa_update(Task), changed)

    await db.flush()

    project_out = _to_project_out(
        project, tasks, deps, task_results, duration, holidays=holidays,
        wbs_nodes=wbs_nodes,
    )

    # 安全修正 (FIX-4): 移除每次編輯都阻斷請求交易的 best-effort 通知。
    # 原本 await notify (LINE / 釘釘 httpx, 10s timeout) 是在 get_db 的
    # session.begin() 交易內進行, 會延長交易並把外部 I/O 失敗風險帶進主流程,
    # 且逐編輯通知為雜訊。有意義的風險通知改由 risk_listener 以背景任務發送。
    # 保留 notify 參數僅為呼叫端相容 (目前為 no-op)。
    return project_out


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@router.get("", response_model=list[ProjectSummary])
async def list_projects(
    limit: int | None = Query(
        default=None, ge=1, description="選配分頁: 回傳專案數上限 (預設全部)。"
    ),
    offset: int | None = Query(
        default=None, ge=0, description="選配分頁: 起始偏移 (預設 0)。"
    ),
    ctx: TenantContext = Depends(verify_tenant),
    db: AsyncSession = Depends(get_db),
) -> list[ProjectSummary]:
    """列出（當前 tenant 可見的）所有專案摘要。

    租戶隔離：PostgreSQL 由 RLS 處理；sqlite (無 RLS) 改以查詢條件
    ``Project.tenant_id == ctx.tenant_id`` 隔離，確保兩種後端皆只列出當前
    租戶的專案 (避免 sqlite dev 模式下跨租戶外洩)。明確過濾於 PostgreSQL 為
    冗餘但無害 (與 RLS 結果一致)。

    軟刪除（FEAT-4）：排除已進回收桶（deleted_at 非 NULL）的專案。
    PERF-1：task_count / project_duration 以單一 GROUP BY 彙總查詢取得
    （不 hydrate Task ORM 列）；選配 limit/offset 分頁（預設全部 = 原行為）。
    """
    stmt = (
        select(Project)
        .where(
            Project.tenant_id == ctx.tenant_id,
            Project.deleted_at.is_(None),
        )
        .order_by(Project.created_at)
    )
    if offset is not None:
        stmt = stmt.offset(offset)
    if limit is not None:
        stmt = stmt.limit(limit)
    result = await db.execute(stmt)
    projects = list(result.scalars().all())

    aggregates = await _task_aggregates(db, [p.project_id for p in projects])

    summaries: list[ProjectSummary] = []
    for p in projects:
        task_count, max_ef = aggregates.get(p.project_id, (0, 0))
        summaries.append(
            ProjectSummary(
                project_id=p.project_id,
                project_name=p.project_name,
                region=p.region,
                tenant_id=p.tenant_id,
                task_count=task_count,
                project_duration=max_ef,
            )
        )
    return summaries


# 注意：/trash 必須宣告於 /{project_id} 之前（FastAPI 依宣告順序匹配路由）。
@router.get("/trash", response_model=list[ProjectSummary])
async def list_trash(
    ctx: TenantContext = Depends(verify_tenant),
    db: AsyncSession = Depends(get_db),
    _role: None = Depends(require_role("admin")),
) -> list[ProjectSummary]:
    """回收桶（FEAT-4，admin 限定）：列出已軟刪除的專案摘要。

    PERF-1：task_count / project_duration 以單一 GROUP BY 彙總查詢取得。
    """
    result = await db.execute(
        select(Project)
        .where(
            Project.tenant_id == ctx.tenant_id,
            Project.deleted_at.is_not(None),
        )
        .order_by(Project.deleted_at.desc())
    )
    projects = list(result.scalars().all())

    aggregates = await _task_aggregates(db, [p.project_id for p in projects])

    summaries: list[ProjectSummary] = []
    for p in projects:
        task_count, max_ef = aggregates.get(p.project_id, (0, 0))
        summaries.append(
            ProjectSummary(
                project_id=p.project_id,
                project_name=p.project_name,
                region=p.region,
                tenant_id=p.tenant_id,
                task_count=task_count,
                project_duration=max_ef,
            )
        )
    return summaries


@router.post("", response_model=ProjectOut, status_code=status.HTTP_201_CREATED)
async def create_project(
    payload: ProjectCreate,
    ctx: TenantContext = Depends(verify_tenant),
    db: AsyncSession = Depends(get_db),
    _role: None = Depends(require_role("editor")),
) -> ProjectOut:
    """建立專案：持久化任務 + 相依，跑 CPM，回寫結果。

    FEAT-1：schedule_data 內任務若提供 links（dep_type/lag_days），以 links 為準
    （predecessors 被忽略並由 links 重新推導）；否則 predecessors 視為 FS + 0。
    FEAT-2：可選 start_date（開工日期）與 work_days（工作日曆）。
    """
    project_id = payload.project_id or f"PRJ-{uuid.uuid4().hex[:12].upper()}"

    # 重複檢查（同 tenant 下 project_id 唯一）
    existing = await db.execute(select(Project).where(Project.project_id == project_id))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Project '{project_id}' already exists",
        )

    project = Project(
        project_id=project_id,
        tenant_id=ctx.tenant_id,
        project_name=payload.project_name,
        region=payload.region or ctx.region,
        start_date=payload.start_date,
        work_days=payload.work_days or "1111110",
        version=0,
    )
    db.add(project)
    await db.flush()

    # 寫入任務與相依
    for td in payload.schedule_data:
        db.add(
            Task(
                project_id=project_id,
                tenant_id=ctx.tenant_id,
                task_id=td.task_id,
                task_name=td.task_name or "",
                duration=td.duration,
                status=td.status or "PENDING",
                # Batch 5：WBS 歸屬（選填、容許懸空）+ 活動限制（選填）。
                wbs_code=td.wbs_code,
                constraint_type=td.constraint_type,
                constraint_day=td.constraint_day,
            )
        )
    await db.flush()

    for td in payload.schedule_data:
        if td.links is not None:
            # links 提供時為準（predecessors 被忽略）；去重以符合 UNIQUE 約束。
            seen: set[str] = set()
            for link in td.links:
                if link.predecessor_task_id in seen:
                    continue
                seen.add(link.predecessor_task_id)
                db.add(
                    TaskDependency(
                        project_id=project_id,
                        tenant_id=ctx.tenant_id,
                        task_id=td.task_id,
                        predecessor_task_id=link.predecessor_task_id,
                        dep_type=link.dep_type or "FS",
                        lag_days=int(link.lag_days or 0),
                    )
                )
        else:
            for pred in dict.fromkeys(td.predecessors):
                db.add(
                    TaskDependency(
                        project_id=project_id,
                        tenant_id=ctx.tenant_id,
                        task_id=td.task_id,
                        predecessor_task_id=pred,
                        dep_type="FS",
                        lag_days=0,
                    )
                )
    await db.flush()

    project_out = await recompute_project(db, project)

    # 稽核 (best-effort): 失敗僅記錄, 絕不中斷主要操作。
    try:
        await audit.log_action(
            db,
            ctx,
            "PROJECT_CREATE",
            {
                "project_id": project_id,
                "project_name": payload.project_name,
                "region": project.region,
                "task_count": len(payload.schedule_data),
                "start_date": (
                    payload.start_date.isoformat() if payload.start_date else None
                ),
                "work_days": project.work_days,
            },
        )
    except Exception as exc:  # noqa: BLE001 - 稽核失敗不可中斷主要操作
        logger.warning("audit PROJECT_CREATE failed (ignored): %s", exc)

    return project_out


@router.get("/{project_id}", response_model=ProjectOut)
async def get_project(
    project_id: str,
    ctx: TenantContext = Depends(verify_tenant),
    db: AsyncSession = Depends(get_db),
) -> ProjectOut:
    """載入專案並以「已持久化」的 CPM 結果欄位回應（PERF-4）。

    recompute_project() 於所有寫入路徑都會持久化 es/ef/ls/lf/float_time/
    is_critical，故讀取端點直接以 Task 列組裝回應，「不」重跑 calculate_cpm。
    僅當結果「看似未算過」（每個任務 es==0 且 ef==0、且存在 duration>0 者）
    時重算一次後回應（兼容歷史資料 / 外部直寫）。
    """
    project = await _get_project_or_404(db, project_id, ctx.tenant_id)
    tasks = await _load_tasks(db, project_id)
    deps = await _load_dependencies(db, project_id)

    needs_recompute = (
        bool(tasks)
        and all((t.es or 0) == 0 and (t.ef or 0) == 0 for t in tasks)
        and any((t.duration or 0) > 0 for t in tasks)
    )
    if needs_recompute:
        return await recompute_project(db, project, notify=False)

    duration = max((t.ef or 0 for t in tasks), default=0)

    holidays: set[date] = set()
    if project.start_date is not None:
        holidays = await _load_holiday_dates(db, project_id)

    wbs_nodes = await _load_wbs_nodes(db, project_id)

    # task_results 傳空 dict -> _to_project_out 直接以 DB 持久化欄位組裝 TaskResult。
    return _to_project_out(
        project, tasks, deps, {}, duration, holidays=holidays, wbs_nodes=wbs_nodes
    )


@router.put("/{project_id}", response_model=ProjectOut)
async def update_project(
    project_id: str,
    payload: ProjectUpdate,
    ctx: TenantContext = Depends(verify_tenant),
    db: AsyncSession = Depends(get_db),
    _role: None = Depends(require_role("editor")),
) -> ProjectOut:
    """更新專案中繼資料（名稱 / 區域 / 開工日期 / 工作日曆）。

    FEAT-3：payload.expected_version 提供且不符當前版本 -> 409 版本衝突；
    更新成功時 version +1。
    FEAT-2：start_date / work_days 僅於 payload 明確提供時才變更
    （以 model_fields_set 區分「未提供」與「提供 None / 預設值」，向下相容）。
    """
    project = await _get_project_or_404(db, project_id, ctx.tenant_id)

    conflict = version_conflict_response(project, payload.expected_version)
    if conflict is not None:
        return conflict

    before = {
        "project_name": project.project_name,
        "region": project.region,
        "start_date": (
            project.start_date.isoformat() if project.start_date else None
        ),
        "work_days": project.work_days,
    }
    project.project_name = payload.project_name
    project.region = payload.region or project.region
    if "start_date" in payload.model_fields_set:
        project.start_date = payload.start_date
    if "work_days" in payload.model_fields_set:
        project.work_days = payload.work_days
    # FEAT-3：中繼資料更新亦使版本 +1。
    project.version = int(project.version or 0) + 1
    await db.flush()

    # 稽核 (best-effort): 失敗僅記錄, 絕不中斷主要操作。
    try:
        await audit.log_action(
            db,
            ctx,
            "PROJECT_UPDATE",
            {
                "project_id": project_id,
                "before": before,
                "after": {
                    "project_name": project.project_name,
                    "region": project.region,
                    "start_date": (
                        project.start_date.isoformat() if project.start_date else None
                    ),
                    "work_days": project.work_days,
                },
            },
        )
    except Exception as exc:  # noqa: BLE001 - 稽核失敗不可中斷主要操作
        logger.warning("audit PROJECT_UPDATE failed (ignored): %s", exc)

    # 不需重算 CPM，但回傳完整 ProjectOut（含既有結果）
    return await get_project(project_id, ctx=ctx, db=db)


@router.delete("/{project_id}")
async def delete_project(
    project_id: str,
    ctx: TenantContext = Depends(verify_tenant),
    db: AsyncSession = Depends(get_db),
    _role: None = Depends(require_role("editor")),
) -> dict:
    """刪除專案（FEAT-4 軟刪除）：標記 deleted_at / deleted_by，進回收桶。

    資料（任務 / 相依 / 進度 / 基準線）全數保留；admin 可由 /projects/trash
    還原 (restore) 或永久刪除 (purge)。所有讀取路徑均排除已軟刪除專案。
    """
    project = await _get_project_or_404(db, project_id, ctx.tenant_id)
    project_name = project.project_name

    project.deleted_at = datetime.now(timezone.utc)
    project.deleted_by = ctx.sub or None
    await db.flush()

    # 稽核 (best-effort): 失敗僅記錄, 絕不中斷主要操作。
    try:
        await audit.log_action(
            db,
            ctx,
            "PROJECT_DELETE",
            {"project_id": project_id, "project_name": project_name, "soft": True},
        )
    except Exception as exc:  # noqa: BLE001 - 稽核失敗不可中斷主要操作
        logger.warning("audit PROJECT_DELETE failed (ignored): %s", exc)

    return {"ok": True}


@router.post("/{project_id}/restore")
async def restore_project(
    project_id: str,
    ctx: TenantContext = Depends(verify_tenant),
    db: AsyncSession = Depends(get_db),
    _role: None = Depends(require_role("admin")),
) -> dict:
    """還原回收桶內的專案（FEAT-4，admin 限定）：清除 deleted_at / deleted_by。"""
    project = await _get_project_or_404(
        db, project_id, ctx.tenant_id, include_deleted=True
    )
    project.deleted_at = None
    project.deleted_by = None
    await db.flush()

    # 稽核 (best-effort): 失敗僅記錄, 絕不中斷主要操作。
    try:
        await audit.log_action(
            db,
            ctx,
            "PROJECT_RESTORE",
            {"project_id": project_id, "project_name": project.project_name},
        )
    except Exception as exc:  # noqa: BLE001 - 稽核失敗不可中斷主要操作
        logger.warning("audit PROJECT_RESTORE failed (ignored): %s", exc)

    return {"ok": True}


@router.delete("/{project_id}/purge")
async def purge_project(
    project_id: str,
    ctx: TenantContext = Depends(verify_tenant),
    db: AsyncSession = Depends(get_db),
    _role: None = Depends(require_role("admin")),
) -> dict:
    """永久刪除專案（FEAT-4，admin 限定）：硬刪除 + 連帶清理。

    任務經 ON DELETE CASCADE 連帶刪除；相依 / 假日（sqlite dev 無 FK 強制）
    手動清理。此操作不可復原。
    """
    project = await _get_project_or_404(
        db, project_id, ctx.tenant_id, include_deleted=True
    )
    project_name = project.project_name

    await db.execute(
        sa_delete(TaskDependency).where(TaskDependency.project_id == project_id)
    )
    await db.execute(
        sa_delete(ProjectHoliday).where(ProjectHoliday.project_id == project_id)
    )
    await db.delete(project)
    await db.flush()

    # 稽核 (best-effort): 失敗僅記錄, 絕不中斷主要操作。
    try:
        await audit.log_action(
            db,
            ctx,
            "PROJECT_PURGE",
            {"project_id": project_id, "project_name": project_name},
        )
    except Exception as exc:  # noqa: BLE001 - 稽核失敗不可中斷主要操作
        logger.warning("audit PROJECT_PURGE failed (ignored): %s", exc)

    return {"ok": True}


# ---------------------------------------------------------------------------
# Endpoints —— 專案假日（FEAT-2 工作日曆）
# ---------------------------------------------------------------------------
@router.get("/{project_id}/holidays", response_model=list[HolidayEntry])
async def get_project_holidays(
    project_id: str,
    ctx: TenantContext = Depends(verify_tenant),
    db: AsyncSession = Depends(get_db),
) -> list[HolidayEntry]:
    """列出專案例外假日（依日期排序）。"""
    await _get_project_or_404(db, project_id, ctx.tenant_id)
    rows = await _load_holidays(db, project_id)
    return [
        HolidayEntry(holiday_date=r.holiday_date, name=r.name or "") for r in rows
    ]


@router.put("/{project_id}/holidays", response_model=list[HolidayEntry])
async def set_project_holidays(
    project_id: str,
    payload: list[HolidayEntry],
    ctx: TenantContext = Depends(verify_tenant),
    db: AsyncSession = Depends(get_db),
    _role: None = Depends(require_role("editor")),
) -> list[HolidayEntry]:
    """以替換式 upsert 覆寫專案例外假日清單（editor+）。

    先清空既有假日再寫入 payload（同日期去重，符合 UNIQUE(project_id,
    holiday_date)）；tenant_id 一律取自 ctx（寫入隔離，絕不信任輸入）。
    回傳更新後的完整假日清單（依日期排序）。
    """
    await _get_project_or_404(db, project_id, ctx.tenant_id)

    await db.execute(
        sa_delete(ProjectHoliday).where(ProjectHoliday.project_id == project_id)
    )
    seen: set[date] = set()
    for entry in payload:
        if entry.holiday_date in seen:
            continue
        seen.add(entry.holiday_date)
        db.add(
            ProjectHoliday(
                project_id=project_id,
                tenant_id=ctx.tenant_id,
                holiday_date=entry.holiday_date,
                name=entry.name or "",
            )
        )
    await db.flush()

    # 稽核 (best-effort): 失敗僅記錄, 絕不中斷主要操作。
    try:
        await audit.log_action(
            db,
            ctx,
            "HOLIDAYS_UPDATE",
            {
                "project_id": project_id,
                "count": len(seen),
                "dates": sorted(d.isoformat() for d in seen),
            },
        )
    except Exception as exc:  # noqa: BLE001 - 稽核失敗不可中斷主要操作
        logger.warning("audit HOLIDAYS_UPDATE failed (ignored): %s", exc)

    rows = await _load_holidays(db, project_id)
    return [
        HolidayEntry(holiday_date=r.holiday_date, name=r.name or "") for r in rows
    ]


# ---------------------------------------------------------------------------
# Endpoints —— WBS 階層（Batch 5 FEAT-1，work breakdown structure）
# ---------------------------------------------------------------------------
@router.get("/{project_id}/wbs", response_model=list[WbsNode])
async def get_project_wbs(
    project_id: str,
    ctx: TenantContext = Depends(verify_tenant),
    db: AsyncSession = Depends(get_db),
) -> list[WbsNode]:
    """列出專案 WBS 節點（扁平清單，依 sort_order/wbs_code 排序；前端負責建樹）。"""
    await _get_project_or_404(db, project_id, ctx.tenant_id)
    rows = await _load_wbs_nodes(db, project_id)
    return _wbs_nodes_to_schema(rows)


@router.put("/{project_id}/wbs", response_model=list[WbsNode])
async def set_project_wbs(
    project_id: str,
    payload: list[WbsNode],
    ctx: TenantContext = Depends(verify_tenant),
    db: AsyncSession = Depends(get_db),
    _role: None = Depends(require_role("editor")),
) -> list[WbsNode]:
    """以替換式 upsert 覆寫專案 WBS 節點清單（editor+）。

    驗證失敗（代碼重複 / parent_code 懸空 / 成環）一律 422，且「不」寫入任何列
    （先驗證、後刪除、後插入）。tenant_id 一律取自 ctx（絕不信任輸入）。
    回傳更新後的完整 WBS 清單。
    """
    await _get_project_or_404(db, project_id, ctx.tenant_id)
    _validate_wbs_tree(payload)

    await db.execute(sa_delete(WbsNodeOrm).where(WbsNodeOrm.project_id == project_id))
    seen: set[str] = set()
    for entry in payload:
        if entry.wbs_code in seen:
            continue
        seen.add(entry.wbs_code)
        db.add(
            WbsNodeOrm(
                project_id=project_id,
                tenant_id=ctx.tenant_id,
                wbs_code=entry.wbs_code,
                name=entry.name or "",
                parent_code=entry.parent_code,
                sort_order=int(entry.sort_order or 0),
            )
        )
    await db.flush()

    # 稽核 (best-effort): 失敗僅記錄, 絕不中斷主要操作。
    try:
        await audit.log_action(
            db,
            ctx,
            "WBS_UPDATE",
            {"project_id": project_id, "count": len(seen)},
        )
    except Exception as exc:  # noqa: BLE001 - 稽核失敗不可中斷主要操作
        logger.warning("audit WBS_UPDATE failed (ignored): %s", exc)

    rows = await _load_wbs_nodes(db, project_id)
    return _wbs_nodes_to_schema(rows)


@router.get("/{project_id}/report")
async def project_report(
    project_id: str,
    ctx: TenantContext = Depends(verify_tenant),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """產生工期 PDF 報表（StreamingResponse, application/pdf）。"""
    project = await _get_project_or_404(db, project_id, ctx.tenant_id)
    project_out = await get_project(project_id, ctx=ctx, db=db)

    # reportlab 產生 PDF 為 CPU 密集的同步作業, 以工作執行緒執行避免阻塞 event loop。
    pdf_bytes = await anyio.to_thread.run_sync(
        functools.partial(reports.generate_schedule_pdf, project_out, project.region)
    )
    filename = safe_filename(f"schedule_{project_id}.pdf")
    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )
