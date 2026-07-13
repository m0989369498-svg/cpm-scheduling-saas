"""P6 (Primavera) XER 格式匯入/匯出 —— 純函式，無 DB / 無 FastAPI 依賴。

XER 是 tab 分隔的純文字格式：
    ERMHDR\t...                 檔頭 (匯出工具/日期等中繼資料，讀取時略過)
    %T\t<table>                 資料表起始 (例：%T\tTASK)
    %F\tf1\tf2\t...             欄位名稱 (該資料表接下來各列的欄位順序)
    %R\tv1\tv2\t...             資料列 (依 %F 的順序取值)
    ... 下一個 %T 開始下一張表，直到檔案結尾

本模組只讀取/寫出下列資料表，其餘一律「忽略但不視為錯誤」
(warnings-not-errors)：
    PROJECT   proj_id (目標專案) / proj_short_name (專案名稱) /
              plan_start_date (開工日期) / clndr_id (預設日曆)
    CALENDAR  day_hr_cnt (每日工時；以 PROJECT.clndr_id 對應的日曆為準，
              找不到時退回檔案中第一個可用日曆並記警告)
    PROJWBS   wbs_id / parent_wbs_id / wbs_short_name / wbs_name
              （代表整個專案本身的根節點不會產生 WBS 節點）
    TASK      task_id / task_code(->our task_id) / task_name / wbs_id /
              target_drtn_hr_cnt(->duration_days) / status_code /
              cstr_type + cstr_date(->constraint_type/constraint_day)
    TASKPRED  task_id(後繼) / pred_task_id(前置) / pred_type / lag_hr_cnt

多專案檔案 (multi-project XER)：以「第一個 PROJECT 資料列」為目標專案，
PROJWBS / TASK / TASKPRED 中 proj_id 屬於其他專案的資料列一律略過並記
警告（不靜默合併）；資料列缺 proj_id 欄位時視為屬於目標專案（寬容）。

所有數值/日期/列舉解析失敗時，皆記錄一筆 warning 並套用合理預設值，
絕不因單一資料列壞掉而讓整份檔案匯入失敗 (resilience)。
"""

from __future__ import annotations

from datetime import date, datetime

from app.core.workcal import date_to_offset, offset_to_date
from app.interop import InteropLink, InteropProject, InteropTask, InteropWbsNode

__all__ = ["parse_xer", "generate_xer"]

# ---------------------------------------------------------------------------
# 對照表 (mapping tables)
# ---------------------------------------------------------------------------

_XER_TO_STATUS = {
    "TK_NotStart": "PENDING",
    "TK_Active": "IN_PROGRESS",
    "TK_Complete": "COMPLETED",
}
_STATUS_TO_XER = {
    "PENDING": "TK_NotStart",
    "IN_PROGRESS": "TK_Active",
    "COMPLETED": "TK_Complete",
    # 系統另有 DELAYED（P6 無對應狀態）；匯出時視為「進行中」的最佳近似。
    # 已知且「接受的」有損對應（accepted lossy mapping）：匯出 -> 再匯入
    # 會變成 IN_PROGRESS，DELAYED 狀態不可往返（P6 的 status_code 列舉僅
    # NotStart/Active/Complete 三態，無任何欄位可攜帶延誤語意；系統內的
    # DELAYED 本就由排程比對推導，重算後可自然恢復）。
    "DELAYED": "TK_Active",
}

_XER_TO_DEP = {
    "PR_FS": "FS",
    "PR_SS": "SS",
    "PR_FF": "FF",
    "PR_SF": "SF",
}
_DEP_TO_XER = {v: k for k, v in _XER_TO_DEP.items()}

_XER_TO_CONSTRAINT = {
    "CS_MSO": "MSO",
    "CS_MSOA": "SNET",
    "CS_MSOB": "SNLT",
    "CS_MEO": "MFO",
    "CS_MEOA": "FNET",
    "CS_MEOB": "FNLT",
}
_CONSTRAINT_TO_XER = {v: k for k, v in _XER_TO_CONSTRAINT.items()}
# CS_ALAP（as-late-as-possible）或缺漏一律視為「無限制」，不需對照。
_XER_NO_CONSTRAINT = {"CS_ALAP", ""}

_KNOWN_TABLES = {"PROJECT", "CALENDAR", "PROJWBS", "TASK", "TASKPRED"}

# parse_xer 未收到 work_days（其簽章刻意不含此參數）時，用來將 cstr_date
# 換算回 constraint_day 的預設工作日曆遮罩；須與 routers/interop.py 建立
# 專案時使用的預設 work_days 一致（1111100 = 週一至週五上工、週六日休）。
# 匯出端（generate_xer）不使用此常數 —— 改用 InteropProject.work_days /
# holidays（呼叫端自專案本身載入），確保匯出日期與系統內顯示一致。
_DEFAULT_WORK_DAYS = "1111100"

# generate_xer 的固定識別碼：單一專案 / 單一日曆，全檔一致引用。
_XER_PROJ_ID = 1
_XER_CLNDR_ID = 1

_DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y")


# ---------------------------------------------------------------------------
# 內部工具 (helpers)
# ---------------------------------------------------------------------------


def _split_row(line: str) -> list[str]:
    return line.split("\t")


def _parse_xer_date(raw: str | None) -> date | None:
    """解析 XER 日期字串（可能帶時間，如 "2026-07-01 08:00"）；失敗回傳 None。"""
    if not raw:
        return None
    token = raw.strip().split(" ")[0].split("T")[0]
    if not token:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(token, fmt).date()
        except ValueError:
            continue
    return None


def _iter_tables(text: str) -> list[tuple[str, list[str], list[list[str]]]]:
    """將 XER 文字切成 [(table_name, field_names, rows), ...]。

    未知行前綴 (非 ERMHDR / %T / %F / %R) 直接忽略，不視為錯誤。
    """
    tables: list[tuple[str, list[str], list[list[str]]]] = []
    current_name: str | None = None
    current_fields: list[str] = []
    current_rows: list[list[str]] = []

    def _flush() -> None:
        if current_name is not None:
            tables.append((current_name, current_fields, current_rows))

    for raw_line in text.splitlines():
        line = raw_line.rstrip("\r")
        if not line:
            continue
        if line.startswith("ERMHDR"):
            continue
        if line.startswith("%T"):
            _flush()
            parts = _split_row(line)
            current_name = parts[1].strip() if len(parts) > 1 else ""
            current_fields = []
            current_rows = []
        elif line.startswith("%F"):
            current_fields = [f.strip() for f in _split_row(line)[1:]]
        elif line.startswith("%R"):
            current_rows.append(_split_row(line)[1:])
        # 其餘前綴 (如少數匯出工具附加的 %E) 一律忽略。
    _flush()
    return tables


def _row_dicts(fields: list[str], rows: list[list[str]]) -> list[dict[str, str]]:
    return [dict(zip(fields, values)) for values in rows]


# ---------------------------------------------------------------------------
# parse_xer
# ---------------------------------------------------------------------------


def parse_xer(text: str, *, hours_per_day: float = 8.0) -> InteropProject:
    """解析 XER 文字為 InteropProject（純函式；解析失敗一律記錄 warning）。"""
    warnings: list[str] = []
    tables = _iter_tables(text)

    project_name = ""
    start_date: date | None = None

    # 多專案 (multi-project) 支援：以第一個 PROJECT 資料列為目標專案。
    target_proj_id: str | None = None
    project_clndr_id: str | None = None
    project_row_seen = False
    other_project_ids: set[str] = set()

    calendar_rows: list[dict[str, str]] = []
    wbs_rows: list[dict[str, str]] = []
    task_rows: list[dict[str, str]] = []
    pred_rows: list[dict[str, str]] = []

    seen_unknown_tables: set[str] = set()

    for name, fields, rows in tables:
        upper = name.strip().upper()
        if upper == "PROJECT":
            for row in _row_dicts(fields, rows):
                row_pid = (row.get("proj_id") or "").strip()
                if not project_row_seen:
                    # 第一個 PROJECT 資料列 = 目標專案（含預設日曆 clndr_id）。
                    project_row_seen = True
                    target_proj_id = row_pid or None
                    project_clndr_id = (row.get("clndr_id") or "").strip() or None
                elif row_pid and row_pid != (target_proj_id or ""):
                    # 檔案內的「其他專案」：不靜默合併其中繼資料。
                    other_project_ids.add(row_pid)
                    continue
                if not project_name:
                    project_name = (row.get("proj_short_name") or "").strip()
                raw_start = row.get("plan_start_date")
                if raw_start and start_date is None:
                    parsed = _parse_xer_date(raw_start)
                    if parsed is not None:
                        start_date = parsed
                    else:
                        warnings.append(
                            f"PROJECT.plan_start_date 無法解析（unparseable date）：{raw_start!r}"
                        )
        elif upper == "CALENDAR":
            calendar_rows.extend(_row_dicts(fields, rows))
        elif upper == "PROJWBS":
            wbs_rows.extend(_row_dicts(fields, rows))
        elif upper == "TASK":
            task_rows.extend(_row_dicts(fields, rows))
        elif upper == "TASKPRED":
            pred_rows.extend(_row_dicts(fields, rows))
        else:
            if upper and upper not in seen_unknown_tables and upper not in _KNOWN_TABLES:
                seen_unknown_tables.add(upper)
                warnings.append(f"忽略未知資料表（unknown table ignored）：{name}")

    if other_project_ids:
        warnings.append(
            "檔案含多個專案（multi-project XER），僅匯入第一個專案"
            f"（proj_id={target_proj_id}）；其餘專案已略過：{sorted(other_project_ids)}"
        )

    # 以目標 proj_id 過濾各資料表（缺 proj_id 欄位的資料列視為屬於目標專案）。
    wbs_rows = _rows_for_project(wbs_rows, "PROJWBS", target_proj_id, warnings)
    task_rows = _rows_for_project(task_rows, "TASK", target_proj_id, warnings)
    pred_rows = _rows_for_project(pred_rows, "TASKPRED", target_proj_id, warnings)

    effective_hours_per_day = _resolve_hours_per_day(
        calendar_rows, project_clndr_id, hours_per_day, warnings
    )

    wbs_nodes, wbs_id_to_code = _parse_projwbs(wbs_rows, warnings)
    tasks, xer_task_id_to_code = _parse_tasks(
        task_rows,
        wbs_id_to_code=wbs_id_to_code,
        start_date=start_date,
        hours_per_day=effective_hours_per_day,
        warnings=warnings,
    )
    _apply_taskpred(pred_rows, tasks, xer_task_id_to_code, effective_hours_per_day, warnings)

    return InteropProject(
        name=project_name,
        start_date=start_date,
        hours_per_day=effective_hours_per_day,
        wbs=wbs_nodes,
        tasks=tasks,
        warnings=warnings,
    )


def _rows_for_project(
    rows: list[dict[str, str]],
    table: str,
    target_proj_id: str | None,
    warnings: list[str],
) -> list[dict[str, str]]:
    """過濾出屬於目標專案的資料列（multi-project XER 防護）。

    * target_proj_id 為 None（PROJECT 表缺席或無 proj_id）時不過濾。
    * 資料列缺 proj_id 欄位（或為空）時視為屬於目標專案（寬容；我方
      generate_xer 舊版即不含此欄位）。
    * 屬於其他專案的資料列以「每表一則」警告彙總，不逐列洗版。
    """
    if target_proj_id is None:
        return rows
    kept: list[dict[str, str]] = []
    skipped = 0
    for row in rows:
        row_pid = (row.get("proj_id") or "").strip()
        if row_pid and row_pid != target_proj_id:
            skipped += 1
        else:
            kept.append(row)
    if skipped:
        warnings.append(
            f"{table}：{skipped} 列屬於其他專案（proj_id != {target_proj_id}），已略過"
        )
    return kept


def _resolve_hours_per_day(
    calendar_rows: list[dict[str, str]],
    project_clndr_id: str | None,
    default_hours: float,
    warnings: list[str],
) -> float:
    """解析每日工時：以 PROJECT.clndr_id 對應的 CALENDAR 列為準。

    真實 P6 匯出常含多個日曆（全域 + 專案/資源日曆，day_hr_cnt 可能不同）；
    「取檔案中第一列」可能選錯日曆而汙染所有工期/延時換算。此處：
      1. 專案指定的日曆（clndr_id 相符且 day_hr_cnt 可用）優先；
      2. 找不到時退回第一個可用日曆，並記一筆警告；
      3. 完全沒有可用日曆時退回呼叫端傳入的 hours_per_day。
    """
    fallback: float | None = None
    matched: float | None = None
    for row in calendar_rows:
        raw_hr = row.get("day_hr_cnt")
        if not raw_hr:
            continue
        try:
            value = float(raw_hr)
        except ValueError:
            warnings.append(f"CALENDAR.day_hr_cnt 無法解析（unparseable number）：{raw_hr!r}")
            continue
        if value <= 0:
            warnings.append(f"CALENDAR.day_hr_cnt 不合理（<=0），略過：{raw_hr!r}")
            continue
        clndr_id = (row.get("clndr_id") or "").strip()
        if (
            matched is None
            and project_clndr_id is not None
            and clndr_id == project_clndr_id
        ):
            matched = value
        if fallback is None:
            fallback = value
    if matched is not None:
        return matched
    if project_clndr_id is not None and calendar_rows:
        warnings.append(
            f"PROJECT.clndr_id={project_clndr_id} 找不到對應且含可用 day_hr_cnt 的 "
            "CALENDAR 資料列，"
            + ("改用檔案中第一個可用日曆" if fallback is not None else "改用呼叫端預設每日工時")
        )
    if fallback is not None:
        return fallback
    return default_hours if default_hours and default_hours > 0 else 8.0


def _parse_projwbs(
    wbs_rows: list[dict[str, str]], warnings: list[str]
) -> tuple[list[InteropWbsNode], dict[str, str]]:
    """解析 PROJWBS 資料列；回傳 (WBS 節點清單, {xer wbs_id: wbs_code})。

    代表整個專案本身的根節點不產生 WBS 節點：優先以 proj_node_flag == 'Y'
    辨識；若整張表都沒有這個欄位，退而求其次，取「唯一一個沒有
    parent_wbs_id」的列視為隱含根節點。皆辨識不出時（結構不明確）保守地
    不排除任何列，讓所有列都成為真正的 WBS 節點。
    """
    has_node_flag = any("proj_node_flag" in row for row in wbs_rows)
    root_ids: set[str] = set()
    if has_node_flag:
        root_ids = {
            (row.get("wbs_id") or "").strip()
            for row in wbs_rows
            if (row.get("proj_node_flag") or "").strip().upper() == "Y"
        }
    if not root_ids:
        no_parent = [row for row in wbs_rows if not (row.get("parent_wbs_id") or "").strip()]
        if len(no_parent) == 1:
            root_ids = {(no_parent[0].get("wbs_id") or "").strip()}

    wbs_id_to_code: dict[str, str] = {}
    parent_of: dict[str, str] = {}
    for row in wbs_rows:
        wbs_id = (row.get("wbs_id") or "").strip()
        if not wbs_id or wbs_id in root_ids:
            continue
        code = (row.get("wbs_short_name") or "").strip()
        if not code:
            warnings.append(f"PROJWBS 節點缺少 wbs_short_name，已略過（wbs_id={wbs_id}）")
            continue
        wbs_id_to_code[wbs_id] = code
        parent_of[wbs_id] = (row.get("parent_wbs_id") or "").strip()

    nodes: list[InteropWbsNode] = []
    for idx, row in enumerate(wbs_rows):
        wbs_id = (row.get("wbs_id") or "").strip()
        if not wbs_id or wbs_id in root_ids or wbs_id not in wbs_id_to_code:
            continue
        parent_id = parent_of.get(wbs_id, "")
        parent_code: str | None = None
        if parent_id and parent_id not in root_ids:
            parent_code = wbs_id_to_code.get(parent_id)
            if parent_code is None:
                warnings.append(
                    f"PROJWBS 節點 {wbs_id} 的 parent_wbs_id 找不到對應節點：{parent_id}"
                )
        nodes.append(
            InteropWbsNode(
                wbs_code=wbs_id_to_code[wbs_id],
                name=(row.get("wbs_name") or "").strip(),
                parent_code=parent_code,
                sort_order=idx,
            )
        )
    return nodes, wbs_id_to_code


def _parse_tasks(
    task_rows: list[dict[str, str]],
    *,
    wbs_id_to_code: dict[str, str],
    start_date: date | None,
    hours_per_day: float,
    warnings: list[str],
) -> tuple[list[InteropTask], dict[str, str]]:
    """解析 TASK 資料列；回傳 (任務清單, {xer task_id: our task_id(task_code)})。"""
    xer_task_id_to_code: dict[str, str] = {}
    for row in task_rows:
        xer_id = (row.get("task_id") or "").strip()
        code = (row.get("task_code") or "").strip() or xer_id
        if xer_id and code:
            xer_task_id_to_code[xer_id] = code

    tasks: list[InteropTask] = []
    for row in task_rows:
        xer_id = (row.get("task_id") or "").strip()
        task_code = xer_task_id_to_code.get(xer_id) or (row.get("task_code") or "").strip()
        if not task_code:
            warnings.append("TASK 資料列缺少 task_id/task_code，已略過")
            continue

        raw_hr = row.get("target_drtn_hr_cnt")
        duration_days = 0
        if raw_hr:
            try:
                duration_days = max(0, round(float(raw_hr) / hours_per_day))
            except ValueError:
                warnings.append(
                    f"TASK {task_code} 的 target_drtn_hr_cnt 無法解析，預設 0：{raw_hr!r}"
                )

        raw_wbs_id = (row.get("wbs_id") or "").strip()
        wbs_code: str | None = None
        if raw_wbs_id:
            wbs_code = wbs_id_to_code.get(raw_wbs_id)

        raw_status = (row.get("status_code") or "").strip()
        status = _XER_TO_STATUS.get(raw_status, "PENDING")
        if raw_status and raw_status not in _XER_TO_STATUS:
            warnings.append(
                f"TASK {task_code} 的 status_code 無法辨識，預設 PENDING：{raw_status!r}"
            )

        constraint_type, constraint_day = _parse_task_constraint(
            row, task_code=task_code, start_date=start_date, warnings=warnings
        )

        tasks.append(
            InteropTask(
                task_id=task_code,
                task_name=(row.get("task_name") or "").strip(),
                duration_days=duration_days,
                wbs_code=wbs_code,
                status=status,
                constraint_type=constraint_type,
                constraint_day=constraint_day,
                links=[],
            )
        )

    return tasks, xer_task_id_to_code


def _parse_task_constraint(
    row: dict[str, str],
    *,
    task_code: str,
    start_date: date | None,
    warnings: list[str],
) -> tuple[str | None, int | None]:
    raw_cstr_type = (row.get("cstr_type") or "").strip()
    if not raw_cstr_type or raw_cstr_type in _XER_NO_CONSTRAINT:
        return None, None

    mapped = _XER_TO_CONSTRAINT.get(raw_cstr_type)
    if mapped is None:
        warnings.append(
            f"TASK {task_code} 的 cstr_type 無法辨識，已略過此限制：{raw_cstr_type!r}"
        )
        return None, None

    raw_cstr_date = row.get("cstr_date")
    cstr_date = _parse_xer_date(raw_cstr_date)
    if cstr_date is None:
        warnings.append(
            f"TASK {task_code} 有 cstr_type（{raw_cstr_type}）但 cstr_date 缺漏或無法解析，"
            "已略過此限制"
        )
        return None, None

    if start_date is None:
        warnings.append(
            f"專案缺少 start_date，無法換算 TASK {task_code} 的 constraint_day，已略過此限制"
        )
        return None, None

    constraint_day = date_to_offset(start_date, cstr_date, _DEFAULT_WORK_DAYS, set())
    return mapped, constraint_day


def _apply_taskpred(
    pred_rows: list[dict[str, str]],
    tasks: list[InteropTask],
    xer_task_id_to_code: dict[str, str],
    hours_per_day: float,
    warnings: list[str],
) -> None:
    task_by_id = {t.task_id: t for t in tasks}

    for row in pred_rows:
        succ_xer_id = (row.get("task_id") or "").strip()
        pred_xer_id = (row.get("pred_task_id") or "").strip()
        succ_code = xer_task_id_to_code.get(succ_xer_id)
        pred_code = xer_task_id_to_code.get(pred_xer_id)

        if (
            succ_code is None
            or pred_code is None
            or succ_code not in task_by_id
            or pred_code not in task_by_id
        ):
            warnings.append(
                "TASKPRED 參照到未知的任務，已略過（task_id="
                f"{succ_xer_id!r}, pred_task_id={pred_xer_id!r}）"
            )
            continue

        raw_type = (row.get("pred_type") or "").strip()
        dep_type = _XER_TO_DEP.get(raw_type)
        if dep_type is None:
            if raw_type:
                warnings.append(f"TASKPRED 的 pred_type 無法辨識，預設 FS：{raw_type!r}")
            dep_type = "FS"

        raw_lag = row.get("lag_hr_cnt")
        lag_days = 0
        if raw_lag:
            try:
                lag_days = round(float(raw_lag) / hours_per_day)
            except ValueError:
                warnings.append(f"TASKPRED 的 lag_hr_cnt 無法解析，預設 0：{raw_lag!r}")

        task_by_id[succ_code].links.append(
            InteropLink(predecessor_task_id=pred_code, dep_type=dep_type, lag_days=lag_days)
        )


# ---------------------------------------------------------------------------
# generate_xer
# ---------------------------------------------------------------------------


def generate_xer(interop: InteropProject, *, hours_per_day: float = 8.0) -> str:
    """將 InteropProject 序列化為最小但可被 P6 讀取的 XER 文字。

    P6 相容性：PROJWBS / TASK / TASKPRED 每列皆帶 proj_id（TASKPRED 另帶
    pred_proj_id，本產生器僅輸出單一專案故兩者相同）；TASK 另帶 clndr_id
    （引用檔案內唯一的 CALENDAR 列）與 task_type / duration_type /
    complete_pct_type 等 P6 慣例上必備的欄位。

    日期換算：cstr_date 以 interop.work_days / interop.holidays（呼叫端自
    專案本身載入的行事曆）換算，確保匯出日期與系統內顯示的日期一致。
    """
    rate = hours_per_day if hours_per_day and hours_per_day > 0 else 8.0
    start_date = interop.start_date or date.today()
    proj_name = interop.name or "PROJECT"
    work_days = interop.work_days or _DEFAULT_WORK_DAYS
    holidays = interop.holidays or set()
    proj_id = _XER_PROJ_ID
    clndr_id = _XER_CLNDR_ID

    lines: list[str] = []
    export_ts = datetime.now().strftime("%Y-%m-%d")
    lines.append(
        "ERMHDR\t1\t"
        f"{export_ts}\tProject\tcpm-saas\tcpm-saas\tcpm-saas\tCPM SaaS\tProject Management\tTW"
    )

    # PROJECT（clndr_id = 專案預設日曆，供匯入端正確解析 day_hr_cnt）
    lines.append("%T\tPROJECT")
    lines.append("%F\tproj_id\tproj_short_name\tplan_start_date\tclndr_id")
    lines.append(f"%R\t{proj_id}\t{proj_name}\t{start_date.isoformat()}\t{clndr_id}")

    # CALENDAR（單一標準日曆，僅供 day_hr_cnt 覆寫使用）
    lines.append("%T\tCALENDAR")
    lines.append("%F\tclndr_id\tclndr_name\tclndr_type\tday_hr_cnt")
    lines.append(f"%R\t{clndr_id}\tStandard\tCA_Base\t{rate:g}")

    # PROJWBS：id=1 為專案根節點（不對應任何我方 WBS 節點），其餘依序編號。
    lines.append("%T\tPROJWBS")
    lines.append(
        "%F\twbs_id\tproj_id\tparent_wbs_id\twbs_short_name\twbs_name\tproj_node_flag"
    )
    lines.append(f"%R\t1\t{proj_id}\t\t{proj_name}\t{proj_name}\tY")

    code_to_wbs_id: dict[str, int] = {}
    next_wbs_id = 2
    for node in interop.wbs:
        code_to_wbs_id[node.wbs_code] = next_wbs_id
        next_wbs_id += 1
    for node in interop.wbs:
        wbs_id = code_to_wbs_id[node.wbs_code]
        parent_id = code_to_wbs_id.get(node.parent_code, 1) if node.parent_code else 1
        lines.append(
            f"%R\t{wbs_id}\t{proj_id}\t{parent_id}\t{node.wbs_code}\t{node.name}\tN"
        )

    # TASK
    lines.append("%T\tTASK")
    lines.append(
        "%F\ttask_id\tproj_id\twbs_id\tclndr_id\ttask_code\ttask_name"
        "\ttask_type\tduration_type\tcomplete_pct_type"
        "\ttarget_drtn_hr_cnt\tstatus_code\tcstr_type\tcstr_date"
    )
    task_id_map: dict[str, int] = {}
    next_task_id = 1
    for task in interop.tasks:
        task_id_map[task.task_id] = next_task_id
        next_task_id += 1

    for task in interop.tasks:
        xer_id = task_id_map[task.task_id]
        wbs_id = code_to_wbs_id.get(task.wbs_code, "") if task.wbs_code else ""
        hours = task.duration_days * rate
        status_code = _STATUS_TO_XER.get(task.status, "TK_NotStart")
        cstr_type = ""
        cstr_date = ""
        if task.constraint_type and task.constraint_day is not None:
            cstr_type = _CONSTRAINT_TO_XER.get(task.constraint_type, "")
            cstr_dt = offset_to_date(start_date, task.constraint_day, work_days, holidays)
            cstr_date = cstr_dt.isoformat()
        lines.append(
            f"%R\t{xer_id}\t{proj_id}\t{wbs_id}\t{clndr_id}\t{task.task_id}"
            f"\t{task.task_name}\tTT_Task\tDT_FixedDrtn\tCP_Drtn\t{hours:g}"
            f"\t{status_code}\t{cstr_type}\t{cstr_date}"
        )

    # TASKPRED（pred_proj_id = proj_id：本產生器僅輸出單一專案的內部相依）
    lines.append("%T\tTASKPRED")
    lines.append(
        "%F\ttask_pred_id\ttask_id\tpred_task_id\tproj_id\tpred_proj_id"
        "\tpred_type\tlag_hr_cnt"
    )
    pred_seq = 1
    for task in interop.tasks:
        succ_id = task_id_map[task.task_id]
        for link in task.links:
            pred_id = task_id_map.get(link.predecessor_task_id)
            if pred_id is None:
                continue
            dep_xer = _DEP_TO_XER.get(link.dep_type, "PR_FS")
            lag_hours = link.lag_days * rate
            lines.append(
                f"%R\t{pred_seq}\t{succ_id}\t{pred_id}\t{proj_id}\t{proj_id}"
                f"\t{dep_xer}\t{lag_hours:g}"
            )
            pred_seq += 1

    return "\n".join(lines) + "\n"
