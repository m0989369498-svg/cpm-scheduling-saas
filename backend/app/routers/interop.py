"""互通匯入/匯出路由 (Interop router) —— Pro Batch A：P6 XER + MS Project MSPDI。

職責：
  POST /projects/import
      多檔上傳 (multipart)：解碼 (utf-8 -> cp950 -> gbk 容錯) -> 依副檔名 /
      內容自動判斷格式 (或由 format 參數指定 xer|mspdi) -> 呼叫純函式解析器
      (app.interop.xer.parse_xer / app.interop.mspdi.parse_mspdi) -> 在「同一
      交易」內：重用 POST /projects 的建立路徑建立專案殼 -> 覆寫 WBS 清單 ->
      寫入任務 + 相依 + 活動限制 -> 重算 CPM -> 稽核 PROJECT_IMPORT -> 回傳
      {project, report}。
  GET /projects/{pid}/export.xer
  GET /projects/{pid}/export.mspdi.xml
      唯讀匯出：載入專案 (任務/相依/WBS/活動限制) -> 組成 InteropProject ->
      於工作執行緒呼叫純函式產生器 (generate_xer / generate_mspdi) -> 以
      StreamingResponse 附件回傳 -> 稽核 PROJECT_EXPORT。viewer 角色亦可存取
      (不掛 require_role)，與 exports.py 既有慣例一致。

設計重點：
  * 解析器 / 產生器為 app.interop 下的「純函式」(無 DB / 無 FastAPI 依賴)；
    本路由僅負責 I/O、格式判斷、與既有 projects 服務路徑的串接。
  * 全程重用 app.routers.projects 既有的 _get_project_or_404 / recompute_project /
    create_project / set_project_wbs 等既有助手 (而非重造)，確保與既有 CPM /
    WBS / 稽核行為完全一致。
  * 匯入為單一交易 (all-or-nothing)：get_db 依賴本身即以
    `async with session.begin(): yield session` 包覆整個請求，此處沿用同一
    db session、不另開交易，任何一步失敗即整體 rollback。
  * task_id 於檔案內重複 -> 422 (寫入任何列之前即檢查)。
  * 匯入檔案大小上限 10MB (MAX_IMPORT_BYTES，可於測試以 monkeypatch 覆寫)。
"""

from __future__ import annotations

import functools
import io
import logging
from datetime import date

import anyio
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit, workcal
from app.core.httputil import safe_filename
from app.deps import TenantContext, get_db, require_role, verify_tenant
from app.interop import InteropLink, InteropProject, InteropTask, InteropWbsNode
from app.interop.mspdi import generate_mspdi, parse_mspdi
from app.interop.xer import generate_xer, parse_xer
from app.models.orm import Task, TaskDependency, TaskProgress
from app.routers.progress import _load_progress
from app.routers.projects import (
    _dependency_maps,
    _get_project_or_404,
    _load_dependencies,
    _load_holiday_dates,
    _load_tasks,
    _load_wbs_nodes,
    create_project as _create_project,
    recompute_project,
    set_project_wbs as _set_project_wbs,
)
from app.schemas.schedule import ProjectCreate, ProjectOut
from app.schemas.schedule import WbsNode as WbsNodeSchema

logger = logging.getLogger("cpm.routers.interop")

router = APIRouter(prefix="/projects", tags=["interop"])

# 匯入檔案大小上限 (bytes)；測試以 monkeypatch 覆寫此模組層級常數模擬超量。
MAX_IMPORT_BYTES = 10 * 1024 * 1024  # 10 MB

DEFAULT_HOURS_PER_DAY = 8.0

# P6 / MS Project 匯入專案的預設工作日曆：週一至週五 (5 日工作制)。
# 與「手動建立專案」的營造業預設 '1111110' (含週六) 刻意不同 —— 匯入來源
# (P6 / MSP) 慣例上多為標準 5 日曆，此處遵循 SPEC 明確指定的匯入預設值。
DEFAULT_IMPORT_WORK_DAYS = "1111100"

# 檔案來源字串的長度上限 —— 與 models/orm.py 的欄位長度一致
# (Task.task_id/TaskDependency 100、Task.wbs_code/WbsNode.wbs_code 60、
# Task.task_name/WbsNode.name/Project.project_name 255)。超長時「截斷 + 警告」
# 而非讓 Postgres 在 flush 時拋 DataError -> 500 (SQLite 不驗證 VARCHAR 長度，
# 僅靠測試無法暴露；此處為唯一防線)。
MAX_TASK_ID_LEN = 100
MAX_WBS_CODE_LEN = 60
MAX_NAME_LEN = 255


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------
class ImportReport(BaseModel):
    """匯入結果摘要 (計數 + 警告清單)。"""

    format: str
    tasks: int = 0
    wbs: int = 0
    links: int = 0
    constraints: int = 0
    # Pro Batch E (FEATURE E3)：帶有實績 (percent_complete>0 或 actual_start /
    # actual_finish) 的任務數，這些任務已 upsert 至 task_progress。
    actuals: int = 0
    warnings: list[str] = Field(default_factory=list)


class ImportResult(BaseModel):
    """POST /projects/import 回應：新建專案的完整輸出 + 匯入報表。"""

    project: ProjectOut
    report: ImportReport


# ---------------------------------------------------------------------------
# 內部工具
# ---------------------------------------------------------------------------
def _field(obj: object, name: str, default: object = None) -> object:
    """容錯欄位存取：obj 可能是 dict 或具屬性的物件 (dataclass / pydantic)。

    interop 解析器對「巢狀葉節點」(wbs 項目 / dependency link 項目) 的具體
    表示型別未強制規定 (SPEC 僅以 ``{key, ...}`` 結構描述)，此處以 dict-or-
    attribute 雙路徑存取保持與任一實作相容。
    """
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _set_field(obj: object, name: str, value: object) -> None:
    """_field 的寫入對偶：dict 以鍵寫入，其餘以屬性寫入。"""
    if isinstance(obj, dict):
        obj[name] = value
    else:
        setattr(obj, name, value)


def _clip(value: object, limit: int, warnings: list[str], context: str) -> str | None:
    """截斷超長字串並記警告；None 原樣通過。"""
    if value is None:
        return None
    text = str(value)
    if len(text) <= limit:
        return text
    warnings.append(
        f"{context} 超過 {limit} 字元上限，已截斷 (truncated)：{text[:30]}…"
    )
    return text[:limit]


def _sanitize_interop_lengths(interop: InteropProject) -> None:
    """把檔案來源字串截到 ORM 欄位長度內 (超長 -> 截斷 + 警告，絕不 500)。

    task_id / predecessor_task_id 以「同一規則」截斷，確保相依參照在截斷後
    仍互相一致；截斷導致 task_id 相撞時由呼叫端既有的重複檢查回 422。
    在重複 task_id 檢查「之前」呼叫。
    """
    w = interop.warnings
    interop.name = _clip(interop.name, MAX_NAME_LEN, w, "專案名稱") or ""

    for node in interop.wbs or []:
        code = _field(node, "wbs_code")
        _set_field(node, "wbs_code", _clip(code, MAX_WBS_CODE_LEN, w, "WBS 代碼"))
        parent = _field(node, "parent_code")
        if parent is not None:
            _set_field(
                node, "parent_code", _clip(parent, MAX_WBS_CODE_LEN, w, "WBS 父節點代碼")
            )
        name = _field(node, "name")
        if name is not None:
            _set_field(node, "name", _clip(name, MAX_NAME_LEN, w, "WBS 名稱"))

    for it in interop.tasks or []:
        it.task_id = _clip(it.task_id, MAX_TASK_ID_LEN, w, "任務代碼 (task_id)") or ""
        it.task_name = _clip(it.task_name, MAX_NAME_LEN, w, "任務名稱") or ""
        if it.wbs_code is not None:
            it.wbs_code = _clip(it.wbs_code, MAX_WBS_CODE_LEN, w, "任務 WBS 代碼")
        for link in it.links or []:
            pred = _field(link, "predecessor_task_id")
            _set_field(
                link,
                "predecessor_task_id",
                _clip(pred, MAX_TASK_ID_LEN, w, "前置任務代碼"),
            )


async def _read_upload_capped(file: UploadFile) -> bytes:
    """串流讀取上傳檔案，超過 MAX_IMPORT_BYTES 立即 413 (不整檔載入記憶體)。"""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_IMPORT_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=(
                    "匯入檔案超過大小上限 (import file exceeds size limit): "
                    f"{MAX_IMPORT_BYTES} bytes"
                ),
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _decode_bytes(raw: bytes) -> str:
    """解碼上傳內容：utf-8 -> cp950 -> gbk 依序嘗試，皆失敗則 422。"""
    for encoding in ("utf-8", "cp950", "gbk"):
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail="無法解碼匯入檔案內容 (unable to decode file; tried utf-8/cp950/gbk)",
    )


def _detect_format(fmt: str | None, filename: str, text: str) -> str:
    """判斷匯入格式：明確指定 (xer|mspdi) 或 auto 依副檔名 / 內容判斷。"""
    normalized = (fmt or "auto").strip().lower()
    if normalized in ("xer", "mspdi"):
        return normalized
    if normalized != "auto":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"不支援的匯入格式 (unsupported import format): {fmt}",
        )

    lower_name = (filename or "").lower()
    stripped = text.lstrip()
    if lower_name.endswith(".xer") or stripped.startswith("ERMHDR"):
        return "xer"
    if "<Project" in text:
        return "mspdi"
    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail="無法自動判斷匯入格式 (cannot auto-detect import format); 請指定 format=xer|mspdi",
    )


async def _build_interop_project(
    db: AsyncSession,
    project_id: str,
    ctx: TenantContext,
    hours_per_day: float,
) -> InteropProject:
    """由已持久化的專案資料組成 InteropProject (供匯出產生器使用)。

    含專案「實際」行事曆 (work_days + 例外假日)：匯出產生器據此換算所有
    工作日偏移 -> 日期 (Start/Finish/限制日期)，確保匯出檔案的日期與系統
    內顯示的日期一致 (而非硬編碼的預設 5 日曆)。
    """
    project = await _get_project_or_404(db, project_id, ctx.tenant_id)
    tasks = await _load_tasks(db, project_id)
    deps = await _load_dependencies(db, project_id)
    wbs_rows = await _load_wbs_nodes(db, project_id)
    holidays = await _load_holiday_dates(db, project_id)
    _pred_map, links_map = _dependency_maps(deps)

    # Pro Batch E (FEATURE E3)：載入實績 (task_progress)，供匯出往返還原
    # percent_complete / actual_start / actual_finish。
    progress_rows = await _load_progress(db, project_id)
    progress_by_task = {r.task_id: r for r in progress_rows}
    work_days = project.work_days or "1111100"

    wbs_list = [
        InteropWbsNode(
            wbs_code=n.wbs_code,
            name=n.name or "",
            parent_code=n.parent_code,
            sort_order=int(n.sort_order or 0),
        )
        for n in wbs_rows
    ]

    interop_tasks: list[InteropTask] = []
    for t in tasks:
        links = links_map.get(t.task_id, [])
        progress = progress_by_task.get(t.task_id)
        percent_complete = 0
        actual_start: date | None = None
        actual_finish: date | None = None
        if progress is not None:
            percent_complete = int(progress.percent_complete or 0)
            if project.start_date is not None:
                if progress.actual_start_day is not None:
                    actual_start = workcal.offset_to_date(
                        project.start_date, progress.actual_start_day, work_days, holidays
                    )
                if progress.actual_finish_day is not None:
                    actual_finish = workcal.offset_to_date(
                        project.start_date, progress.actual_finish_day, work_days, holidays
                    )
        interop_tasks.append(
            InteropTask(
                task_id=t.task_id,
                task_name=t.task_name or "",
                duration_days=int(t.duration or 0),
                wbs_code=t.wbs_code,
                status=t.status or "PENDING",
                constraint_type=t.constraint_type,
                constraint_day=t.constraint_day,
                links=[
                    InteropLink(
                        predecessor_task_id=link.predecessor_task_id,
                        dep_type=link.dep_type,
                        lag_days=int(link.lag_days or 0),
                    )
                    for link in links
                ],
                percent_complete=percent_complete,
                actual_start=actual_start,
                actual_finish=actual_finish,
            )
        )

    return InteropProject(
        name=project.project_name,
        start_date=project.start_date,
        hours_per_day=hours_per_day,
        wbs=wbs_list,
        tasks=interop_tasks,
        warnings=[],
        work_days=project.work_days or "1111110",
        holidays=holidays,
    )


# ---------------------------------------------------------------------------
# Endpoints —— 匯入
# ---------------------------------------------------------------------------
@router.post("/import", response_model=ImportResult, status_code=status.HTTP_201_CREATED)
async def import_project(
    file: UploadFile = File(...),
    format: str | None = Form(None),
    project_id: str | None = Form(None),
    hours_per_day: float = Form(DEFAULT_HOURS_PER_DAY),
    ctx: TenantContext = Depends(verify_tenant),
    db: AsyncSession = Depends(get_db),
    _role: None = Depends(require_role("editor")),
) -> ImportResult:
    """匯入 P6 XER 或 MS Project MSPDI XML，建立新專案 (editor+)。

    單一交易 (all-or-nothing)：任何步驟失敗 (解析錯誤 / task_id 重複 /
    CPM 計算失敗) 皆使整個請求交易 rollback，不留下部分寫入的專案。
    """
    raw = await _read_upload_capped(file)
    text = _decode_bytes(raw)
    fmt = _detect_format(format, file.filename or "", text)

    try:
        # 解析為 CPU 密集的同步純函式 (大檔案可達數十萬列)，與匯出端一致
        # 以工作執行緒執行，避免阻塞整個 event loop (所有租戶的並行請求)。
        if fmt == "xer":
            interop = await anyio.to_thread.run_sync(
                functools.partial(parse_xer, text, hours_per_day=hours_per_day)
            )
        else:
            interop = await anyio.to_thread.run_sync(parse_mspdi, text)
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"匯入解析失敗 (import parse failed): {exc}",
        )
    except Exception as exc:  # noqa: BLE001 - 任意解析錯誤一律視為 422 (壞檔)
        logger.warning("interop parse failed (%s): %s", fmt, exc)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"匯入解析失敗 (import parse failed): {exc}",
        )

    # 檔案來源字串截斷至 ORM 欄位長度 (超長 -> 截斷 + 警告，絕不 DataError 500)。
    # 須先於重複檢查：截斷後相撞的 task_id 應以 422 呈現而非資料庫錯誤。
    _sanitize_interop_lengths(interop)

    # task_id 於檔案內重複 -> 422，且尚未寫入任何列。
    task_ids = [t.task_id for t in interop.tasks]
    if len(task_ids) != len(set(task_ids)):
        dup = sorted({tid for tid in task_ids if task_ids.count(tid) > 1})
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"匯入檔案內任務代碼重複 (duplicate task_id in import file): {dup}",
        )

    project_name = (interop.name or file.filename or "Imported Project").strip()
    project_name = project_name or "Imported Project"
    # 檔名 fallback 也可能超長 (interop.name 已於 _sanitize_interop_lengths 截斷)。
    if len(project_name) > MAX_NAME_LEN:
        interop.warnings.append(
            f"專案名稱 (取自檔名) 超過 {MAX_NAME_LEN} 字元上限，已截斷 (truncated)"
        )
        project_name = project_name[:MAX_NAME_LEN]
    start_date: date = interop.start_date or date.today()

    # (1) 重用 POST /projects 的建立路徑，先建立專案殼 (無任務)。
    shell_payload = ProjectCreate(
        project_id=project_id,
        project_name=project_name,
        region=ctx.region,
        start_date=start_date,
        work_days=DEFAULT_IMPORT_WORK_DAYS,
        schedule_data=[],
    )
    project_out = await _create_project(shell_payload, ctx=ctx, db=db, _role=None)
    resolved_project_id = project_out.project_id

    # (2) 覆寫 WBS 清單 (重用 PUT /projects/{pid}/wbs 的驗證 + 寫入邏輯)。
    wbs_count = len(interop.wbs)
    if interop.wbs:
        wbs_payload = [
            WbsNodeSchema(
                wbs_code=str(_field(w, "wbs_code")),
                name=str(_field(w, "name", "") or ""),
                parent_code=_field(w, "parent_code"),
                sort_order=int(_field(w, "sort_order", 0) or 0),
            )
            for w in interop.wbs
        ]
        await _set_project_wbs(resolved_project_id, wbs_payload, ctx=ctx, db=db, _role=None)

    # (3) 寫入任務 (含 wbs_code / 活動限制)。
    for it in interop.tasks:
        db.add(
            Task(
                project_id=resolved_project_id,
                tenant_id=ctx.tenant_id,
                task_id=it.task_id,
                task_name=it.task_name or "",
                duration=int(it.duration_days or 0),
                status=it.status or "PENDING",
                wbs_code=it.wbs_code,
                constraint_type=it.constraint_type,
                constraint_day=it.constraint_day,
            )
        )
    await db.flush()

    # (4) 寫入相依 (links)；同一 predecessor 於單一任務內去重 (符合 UNIQUE 約束)。
    links_count = 0
    constraints_count = 0
    for it in interop.tasks:
        if it.constraint_type is not None:
            constraints_count += 1
        seen_preds: set[str] = set()
        for link in it.links or []:
            pred = _field(link, "predecessor_task_id")
            if not pred or pred in seen_preds:
                continue
            seen_preds.add(pred)
            db.add(
                TaskDependency(
                    project_id=resolved_project_id,
                    tenant_id=ctx.tenant_id,
                    task_id=it.task_id,
                    predecessor_task_id=pred,
                    dep_type=str(_field(link, "dep_type", "FS") or "FS"),
                    lag_days=int(_field(link, "lag_days", 0) or 0),
                )
            )
            links_count += 1
    await db.flush()

    # (5) 重算 CPM (含限制違反判定)。
    project = await _get_project_or_404(db, resolved_project_id, ctx.tenant_id)
    final_out = await recompute_project(db, project)

    # (6) 實績 (actuals) upsert 至 task_progress (Pro Batch E FEATURE E3)。
    # 僅針對「帶有實績」的任務 (percent_complete>0 或 actual_start/actual_finish
    # 任一有值)；budget 保持 0 (匯入檔案不攜帶預算，需另以 PUT /progress 設定)。
    actuals_count = 0
    for it in interop.tasks:
        has_actuals = bool(
            (it.percent_complete or 0) > 0 or it.actual_start or it.actual_finish
        )
        if not has_actuals:
            continue
        actuals_count += 1
        actual_start_day = (
            workcal.date_to_offset(
                start_date, it.actual_start, DEFAULT_IMPORT_WORK_DAYS, set()
            )
            if it.actual_start
            else None
        )
        actual_finish_day = (
            workcal.date_to_offset(
                start_date, it.actual_finish, DEFAULT_IMPORT_WORK_DAYS, set()
            )
            if it.actual_finish
            else None
        )
        db.add(
            TaskProgress(
                project_id=resolved_project_id,
                tenant_id=ctx.tenant_id,
                task_id=it.task_id,
                budget=0.0,
                percent_complete=max(0, min(100, int(it.percent_complete or 0))),
                actual_cost=0.0,
                actual_start_day=actual_start_day,
                actual_finish_day=actual_finish_day,
            )
        )
    await db.flush()

    # 稽核 (best-effort): 失敗僅記錄, 絕不中斷主要操作。
    try:
        await audit.log_action(
            db,
            ctx,
            "PROJECT_IMPORT",
            {
                "project_id": resolved_project_id,
                "format": fmt,
                "filename": file.filename,
                "tasks": len(interop.tasks),
                "wbs": wbs_count,
                "links": links_count,
                "constraints": constraints_count,
                "actuals": actuals_count,
                "warnings": list(interop.warnings or []),
            },
        )
    except Exception as exc:  # noqa: BLE001 - 稽核失敗不可中斷主要操作
        logger.warning("audit PROJECT_IMPORT failed (ignored): %s", exc)

    report = ImportReport(
        format=fmt,
        tasks=len(interop.tasks),
        wbs=wbs_count,
        links=links_count,
        constraints=constraints_count,
        actuals=actuals_count,
        warnings=list(interop.warnings or []),
    )
    return ImportResult(project=final_out, report=report)


# ---------------------------------------------------------------------------
# Endpoints —— 匯出 (唯讀，viewer 亦可)
# ---------------------------------------------------------------------------
@router.get("/{project_id}/export.xer")
async def export_xer(
    project_id: str,
    hours_per_day: float = Query(default=DEFAULT_HOURS_PER_DAY, gt=0),
    ctx: TenantContext = Depends(verify_tenant),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """匯出專案為 P6 XER (text/plain 附件)。viewer 角色亦可 (唯讀)。"""
    interop = await _build_interop_project(db, project_id, ctx, hours_per_day)

    # XER 產生為 CPU 密集的同步作業，以工作執行緒執行避免阻塞 event loop。
    xer_text = await anyio.to_thread.run_sync(
        functools.partial(generate_xer, interop, hours_per_day=hours_per_day)
    )

    try:
        await audit.log_action(
            db, ctx, "PROJECT_EXPORT", {"project_id": project_id, "format": "xer"}
        )
    except Exception as exc:  # noqa: BLE001 - 稽核失敗不可中斷主要操作
        logger.warning("audit PROJECT_EXPORT(xer) failed (ignored): %s", exc)

    filename = safe_filename(f"{project_id}.xer")
    return StreamingResponse(
        io.BytesIO(xer_text.encode("utf-8")),
        media_type="text/plain",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{project_id}/export.mspdi.xml")
async def export_mspdi(
    project_id: str,
    ctx: TenantContext = Depends(verify_tenant),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """匯出專案為 MS Project MSPDI XML (application/xml 附件)。viewer 角色亦可。"""
    interop = await _build_interop_project(db, project_id, ctx, DEFAULT_HOURS_PER_DAY)

    # MSPDI XML 產生為 CPU 密集的同步作業，以工作執行緒執行避免阻塞 event loop。
    xml_text = await anyio.to_thread.run_sync(functools.partial(generate_mspdi, interop))

    try:
        await audit.log_action(
            db, ctx, "PROJECT_EXPORT", {"project_id": project_id, "format": "mspdi"}
        )
    except Exception as exc:  # noqa: BLE001 - 稽核失敗不可中斷主要操作
        logger.warning("audit PROJECT_EXPORT(mspdi) failed (ignored): %s", exc)

    filename = safe_filename(f"{project_id}.mspdi.xml")
    return StreamingResponse(
        io.BytesIO(xml_text.encode("utf-8")),
        media_type="application/xml",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
