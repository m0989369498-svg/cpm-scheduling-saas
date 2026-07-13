"""MS Project MSPDI (Microsoft Project Data Interchange) XML —— 純函式模組。

無 DB / 無 FastAPI 依賴：只依賴標準函式庫 (xml.etree.ElementTree / re /
datetime) 與另外兩個既有的純函式模組：
    app.interop            共用中介資料結構 (InteropProject / InteropTask / ...)
    app.core.workcal       offset_to_date / date_to_offset（工作日曆換算）
    app.core.cpm_engine    calculate_cpm（僅 generate 端用來推導 Start/Finish）
    app.schemas.schedule   TaskDefinition / DependencyLink（僅作為 CPM 引擎輸入）

公開 API：
    parse_mspdi(xml_text, *, hours_per_day=8.0)   -> InteropProject
    generate_mspdi(interop)                        -> str (MSPDI XML)

安全性 (security)：parse_mspdi 在動用 XML parser 之前，先以大小寫不敏感的
字串比對拒絕任何含有 "<!DOCTYPE" 或 "<!ENTITY" 的輸入，防範 XXE / billion
laughs 攻擊（詳見 _reject_dangerous_xml）。

MSPDI 匯出入日曆假設：
  * 匯出（generate_mspdi）：Start/Finish/ConstraintDate 一律以
    InteropProject.work_days / holidays（呼叫端自專案本身載入的行事曆）
    換算，確保匯出檔案的日期與系統內顯示的日期一致。
  * 匯入（parse_mspdi）：ConstraintDate -> constraint_day 採用
    _DEFAULT_WORK_DAYS（週一至週五 5 日曆、無例外假日），與
    routers/interop.py 匯入時建立專案所用的 DEFAULT_IMPORT_WORK_DAYS 一致
    （匯入建立的新專案即採此行事曆，offset 與日期因此互相吻合）。

WBS 階層推導：以 <OutlineNumber>（MS Project 保證以 "." 分段的階層編號）
推導父子關係；<WBS> 文字（可能是使用者自訂代碼遮罩，如 "A-1"，不保證以
"." 分段）僅作為節點的顯示代碼（wbs_code 標籤），兩者解耦。
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import date

from app.core.cpm_engine import calculate_cpm
from app.core.workcal import date_to_offset, offset_to_date
from app.interop import InteropLink, InteropProject, InteropTask, InteropWbsNode
from app.schemas.schedule import DependencyLink, TaskDefinition

__all__ = ["parse_mspdi", "generate_mspdi"]

# MSPDI 預設命名空間（namespace）。實務上 MS Project 匯出的檔案一律宣告此
# 命名空間；解析時以「本地名稱 (local name)」比對，不強制要求命名空間
# 完全相符，對缺命名空間宣告的簡化測試檔案也保持相容。
_NS = "http://schemas.microsoft.com/project"

# 匯入端（parse_mspdi）ConstraintDate -> constraint_day 換算用的預設行事曆
# ——標準 5 日曆（週一至週五工作，不含例外假日），與 routers/interop.py 的
# DEFAULT_IMPORT_WORK_DAYS 一致。匯出端（generate_mspdi）不使用此常數，
# 改用 InteropProject.work_days / holidays（呼叫端自專案本身載入）。
_DEFAULT_WORK_DAYS = "1111100"

# ConstraintType 數值 -> 我方 constraint_type（None = 不受限 / ASAP·ALAP）。
_CONSTRAINT_IMPORT: dict[int, str | None] = {
    0: None,  # As Soon As Possible
    1: None,  # As Late As Possible
    2: "MSO",  # Must Start On
    3: "MFO",  # Must Finish On
    4: "SNET",  # Start No Earlier Than
    5: "SNLT",  # Start No Later Than
    6: "FNET",  # Finish No Earlier Than
    7: "FNLT",  # Finish No Later Than
}
_CONSTRAINT_EXPORT: dict[str | None, int] = {
    None: 0,
    "MSO": 2,
    "MFO": 3,
    "SNET": 4,
    "SNLT": 5,
    "FNET": 6,
    "FNLT": 7,
}

# PredecessorLink Type 數值 -> 我方 dep_type；Type 元素缺席時預設 FS（=1）。
_LINK_TYPE_IMPORT: dict[int, str] = {0: "FF", 1: "FS", 2: "SF", 3: "SS"}
_LINK_TYPE_EXPORT: dict[str, int] = {v: k for k, v in _LINK_TYPE_IMPORT.items()}

# ISO-8601-like Duration：MS Project 本身一律輸出全小時的 PT 形式
# （PT8H0M0S），但其他 MSPDI 相容產生器可能輸出日分量（P1D / P1DT8H0M0S）
# ——同屬合法 ISO-8601，一併支援（日分量以 hours_per_day 換算為小時）。
_DURATION_RE = re.compile(
    r"^P"
    r"(?:(?P<d>\d+(?:\.\d+)?)D)?"
    r"(?:T"
    r"(?:(?P<h>\d+(?:\.\d+)?)H)?"
    r"(?:(?P<m>\d+(?:\.\d+)?)M)?"
    r"(?:(?P<s>\d+(?:\.\d+)?)S)?"
    r")?$",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# 安全性 (security)
# ---------------------------------------------------------------------------
def _reject_dangerous_xml(xml_text: str) -> None:
    """拒絕含 DOCTYPE / ENTITY 宣告的輸入（XXE / billion-laughs 防護）。

    刻意在動用任何 XML parser 之前，以單純的大小寫不敏感字串比對執行，
    避免 parser 本身在建構 DOM/事件流時就已經展開了惡意實體。
    """
    lowered = xml_text.lower()
    if "<!doctype" in lowered or "<!entity" in lowered:
        raise ValueError(
            "MSPDI 內容含 DOCTYPE/ENTITY 宣告，已拒絕解析（rejected: "
            "possible XXE / billion-laughs payload）"
        )


# ---------------------------------------------------------------------------
# XML 讀取小工具（忽略命名空間前綴，僅比對本地名稱）
# ---------------------------------------------------------------------------
def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _find(elem: ET.Element, name: str) -> ET.Element | None:
    for child in elem:
        if _local(child.tag) == name:
            return child
    return None


def _find_all(elem: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in elem if _local(child.tag) == name]


def _text(elem: ET.Element | None, name: str, default: str | None = None) -> str | None:
    if elem is None:
        return default
    child = _find(elem, name)
    if child is None or child.text is None or child.text.strip() == "":
        return default
    return child.text.strip()


# ---------------------------------------------------------------------------
# 值轉換小工具
# ---------------------------------------------------------------------------
def _parse_date(text: str | None, warnings: list[str], context: str) -> date | None:
    """解析 MSPDI 日期文字（"2026-07-13" 或 "2026-07-13T08:00:00"）。"""
    if not text:
        return None
    try:
        return date.fromisoformat(text.strip()[:10])
    except ValueError:
        warnings.append(f"無法解析日期（{context}）：{text!r}")
        return None


def _parse_duration_hours(
    text: str | None, warnings: list[str], context: str, hours_per_day: float = 8.0
) -> float:
    """解析 ISO-8601-like Duration（如 "PT8H0M0S" 或 "P1DT4H"）為小時數。

    日分量（"P1D"）以 hours_per_day 換算為小時（1 天 = hours_per_day 小時）。
    """
    if not text:
        return 0.0
    match = _DURATION_RE.match(text.strip())
    if not match:
        warnings.append(f"無法解析 Duration（{context}）：{text!r}，以 0 小時處理")
        return 0.0
    days = float(match.group("d") or 0)
    hours = float(match.group("h") or 0)
    minutes = float(match.group("m") or 0)
    seconds = float(match.group("s") or 0)
    return days * hours_per_day + hours + minutes / 60.0 + seconds / 3600.0


def _status_from_percent(text: str | None) -> str:
    """依 PercentComplete 粗略推導狀態；缺席時預設 PENDING。"""
    if text is None:
        return "PENDING"
    try:
        pct = float(text)
    except ValueError:
        return "PENDING"
    if pct >= 100:
        return "COMPLETED"
    if pct > 0:
        return "IN_PROGRESS"
    return "PENDING"


def _parent_code(code: str) -> str | None:
    """由階層代碼 (如 "1.2.3") 去掉最後一段推導父節點代碼；無 "." 視為根層級。

    僅應餵入「保證以 . 分段」的階層鍵（OutlineNumber；WBS 文字僅在
    OutlineNumber 缺席時退而求其次）——使用者自訂 WBS 代碼遮罩（如 "A-1"）
    不保證以 . 分段，不可直接以本函式推導其階層。
    """
    return code.rsplit(".", 1)[0] if "." in code else None


def _esc(text: str) -> str:
    """XML 文字內容跳脫（& < > 三者足以保證 well-formed；屬性值本模組未使用）。"""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---------------------------------------------------------------------------
# parse_mspdi
# ---------------------------------------------------------------------------
def parse_mspdi(xml_text: str, *, hours_per_day: float = 8.0) -> InteropProject:
    """解析 MSPDI XML 字串為 InteropProject。

    * OutlineLevel<=0（專案根摘要任務）不建立任何節點（代表整個專案本身）。
    * Summary=1 的 Task 視為 WBS 節點；其餘視為一般任務。
    * 階層推導與顯示代碼「解耦」：
        - 階層鍵（hierarchy key）：優先 <OutlineNumber>（MS Project 保證以
          "." 分段），缺席時退回 <WBS> 文字；以去掉最後一段推導父節點。
        - 顯示代碼（wbs_code 標籤）：優先 <WBS> 文字（可能是自訂代碼遮罩，
          如 "A-1"），缺席時退回 <OutlineNumber>。
      如此自訂 WBS 代碼遮罩（非 "." 分段）的檔案仍能正確還原樹狀階層。
    * 任務的 wbs_code 為其「所屬摘要任務」的顯示代碼（由階層鍵之父段反查）。
    * task_id 於檔案內重複 -> 直接拋出 ValueError（由呼叫端轉換為 422）。
    * 數值 / 日期欄位解析失敗一律降級為警告 (warnings) + 合理預設值，不中斷
      整體解析（DOCTYPE/ENTITY 除外，屬不可恢復的安全性錯誤）。
    """
    if not isinstance(xml_text, str):
        raise ValueError("parse_mspdi 需要字串輸入 (xml_text must be str)")

    _reject_dangerous_xml(xml_text)

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise ValueError(f"MSPDI XML 格式錯誤（malformed XML）：{exc}") from exc

    hpd = hours_per_day if hours_per_day and hours_per_day > 0 else 8.0
    warnings: list[str] = []

    project_name = _text(root, "Name", "") or ""
    start_date = _parse_date(_text(root, "StartDate"), warnings, "Project/StartDate")

    tasks_el = _find(root, "Tasks")
    task_els = _find_all(tasks_el, "Task") if tasks_el is not None else []

    wbs_nodes: list[InteropWbsNode] = []
    tasks: list[InteropTask] = []
    seen_task_ids: set[str] = set()

    # 階層鍵（OutlineNumber / 退回 WBS 文字）-> 顯示代碼（wbs_code 標籤）。
    # MSPDI 依大綱順序列出任務（父摘要先於子項），故子項查父時父已註冊。
    key_to_label: dict[str, str] = {}
    used_labels: set[str] = set()

    for idx, task_el in enumerate(task_els):
        uid = _text(task_el, "UID")
        if uid is None:
            warnings.append(f"Task 缺少 UID（文件順序第 {idx} 筆），已略過")
            continue

        outline_level_text = _text(task_el, "OutlineLevel")
        if outline_level_text is None:
            outline_level = 1
        else:
            try:
                outline_level = int(float(outline_level_text))
            except ValueError:
                warnings.append(
                    f"OutlineLevel 非整數（UID={uid}）：{outline_level_text!r}，視為 1"
                )
                outline_level = 1

        # 專案根摘要任務 (OutlineLevel <= 0，通常 UID=0) 代表整個專案本身，
        # 不對應任何 WBS 節點或任務。
        if outline_level <= 0:
            continue

        name_text = _text(task_el, "Name", "") or ""
        is_summary = _text(task_el, "Summary") == "1"
        wbs_text = _text(task_el, "WBS")
        outline_number = _text(task_el, "OutlineNumber")
        # 階層鍵：OutlineNumber 優先（保證 "." 分段）；顯示代碼：WBS 文字優先。
        hierarchy_key = outline_number or wbs_text

        if is_summary:
            if not hierarchy_key:
                warnings.append(f"摘要任務缺少 WBS/OutlineNumber（UID={uid}），已略過")
                continue
            label = wbs_text or outline_number or ""
            if label in used_labels:
                fallback = outline_number if outline_number and outline_number != label else None
                if fallback and fallback not in used_labels:
                    warnings.append(
                        f"WBS 代碼重複（UID={uid}）：{label!r}，改以 OutlineNumber "
                        f"{fallback!r} 作為節點代碼"
                    )
                    label = fallback
                else:
                    warnings.append(f"WBS 代碼重複（UID={uid}）：{label!r}，已略過此摘要任務")
                    continue
            used_labels.add(label)

            parent_key = _parent_code(hierarchy_key)
            parent_label: str | None = None
            if parent_key is not None:
                parent_label = key_to_label.get(parent_key)
                if parent_label is None:
                    warnings.append(
                        f"摘要任務（UID={uid}）的父層 {parent_key!r} 找不到對應節點，"
                        "視為根層級"
                    )

            key_to_label[hierarchy_key] = label
            if wbs_text and wbs_text != hierarchy_key:
                # 同時以 WBS 文字註冊，供「缺 OutlineNumber 的子項」以 WBS 文字反查。
                key_to_label.setdefault(wbs_text, label)

            wbs_nodes.append(
                InteropWbsNode(
                    wbs_code=label,
                    name=name_text,
                    parent_code=parent_label,
                    sort_order=idx,
                )
            )
            continue

        # 一般任務
        if uid in seen_task_ids:
            raise ValueError(f"匯入檔案內任務代碼重複（duplicate task_id / UID）：{uid}")
        seen_task_ids.add(uid)

        container_key = _parent_code(hierarchy_key) if hierarchy_key else None
        wbs_code_for_task = (
            key_to_label.get(container_key, container_key) if container_key else None
        )

        is_milestone = _text(task_el, "Milestone") == "1"
        duration_hours = _parse_duration_hours(
            _text(task_el, "Duration"), warnings, f"Task UID={uid}", hpd
        )
        duration_days = 0 if is_milestone else max(0, round(duration_hours / hpd))

        status = _status_from_percent(_text(task_el, "PercentComplete"))

        constraint_type, constraint_day = _parse_constraint(
            task_el, start_date, warnings, uid
        )

        links: list[InteropLink] = []
        for pred_el in _find_all(task_el, "PredecessorLink"):
            pred_uid = _text(pred_el, "PredecessorUID")
            if not pred_uid:
                warnings.append(
                    f"PredecessorLink 缺少 PredecessorUID（UID={uid}），已略過"
                )
                continue
            type_text = _text(pred_el, "Type")
            if type_text is None:
                type_num = 1  # 缺席時預設 FS
            else:
                try:
                    type_num = int(type_text)
                except ValueError:
                    warnings.append(
                        f"PredecessorLink Type 非整數（UID={uid}）：{type_text!r}，以 FS 處理"
                    )
                    type_num = 1
            dep_type = _LINK_TYPE_IMPORT.get(type_num)
            if dep_type is None:
                warnings.append(
                    f"未知的 PredecessorLink Type（UID={uid}）：{type_num}，以 FS 處理"
                )
                dep_type = "FS"

            lag_text = _text(pred_el, "LinkLag")
            if lag_text is None:
                lag_tenths = 0.0
            else:
                try:
                    lag_tenths = float(lag_text)
                except ValueError:
                    warnings.append(
                        f"LinkLag 非數字（UID={uid}）：{lag_text!r}，以 0 處理"
                    )
                    lag_tenths = 0.0
            lag_days = round((lag_tenths / 10.0) / 60.0 / hpd)

            links.append(
                InteropLink(predecessor_task_id=pred_uid, dep_type=dep_type, lag_days=lag_days)
            )

        tasks.append(
            InteropTask(
                task_id=uid,
                task_name=name_text,
                duration_days=duration_days,
                wbs_code=wbs_code_for_task,
                status=status,
                constraint_type=constraint_type,
                constraint_day=constraint_day,
                links=links,
            )
        )

    return InteropProject(
        name=project_name,
        start_date=start_date,
        hours_per_day=hpd,
        wbs=wbs_nodes,
        tasks=tasks,
        warnings=warnings,
    )


def _parse_constraint(
    task_el: ET.Element,
    start_date: date | None,
    warnings: list[str],
    uid: str,
) -> tuple[str | None, int | None]:
    """由 ConstraintType + ConstraintDate 推導 (constraint_type, constraint_day)。"""
    type_text = _text(task_el, "ConstraintType")
    if type_text is None:
        return None, None
    try:
        type_num = int(type_text)
    except ValueError:
        warnings.append(
            f"ConstraintType 非整數（UID={uid}）：{type_text!r}，已忽略此限制"
        )
        return None, None

    _MISSING = object()
    ctype = _CONSTRAINT_IMPORT.get(type_num, _MISSING)
    if ctype is _MISSING:
        warnings.append(f"未知的 ConstraintType（UID={uid}）：{type_num}，已忽略此限制")
        return None, None
    if ctype is None:  # ASAP / ALAP -> 不受限
        return None, None

    date_text = _text(task_el, "ConstraintDate")
    target = _parse_date(date_text, warnings, f"ConstraintDate（UID={uid}）")
    if target is None:
        warnings.append(
            f"限制型態 {ctype} 缺少可用的 ConstraintDate（UID={uid}），已忽略此限制"
        )
        return None, None

    # InteropProject 若無 start_date（理應罕見），退化為以限制日期本身為基準，
    # 使 constraint_day 至少落在合理範圍（0）而不拋例外。
    base_start = start_date or target
    constraint_day = date_to_offset(base_start, target, _DEFAULT_WORK_DAYS, set())
    return ctype, constraint_day


# ---------------------------------------------------------------------------
# generate_mspdi
# ---------------------------------------------------------------------------
def generate_mspdi(interop: InteropProject) -> str:
    """由 InteropProject 產生可被 MS Project 開啟的 MSPDI XML 字串。

    * Start/Finish 由 CPM 引擎（core.cpm_engine.calculate_cpm）推導的 es/ef
      工作日偏移換算而來（採 interop.work_days / interop.holidays ——
      呼叫端自專案本身載入的行事曆，確保日期與系統內顯示一致）。
    * 每個 WBS 節點輸出為 Summary=1 的 Task，其 Start/Finish 為底下所有任務
      （含巢狀子 WBS）es/ef 的彙總 (rollup)；無任何任務時退化為 (0, 0)。
    * OutlineNumber 依樹狀位置產生（根層 1、2…；子層 1.1、1.2…），與
      parse_mspdi 的階層推導精確互逆；摘要任務的 <WBS> 為我方 wbs_code
      顯示代碼，一般任務的 <WBS> 與 OutlineNumber 相同（MS Project 預設）。
    * PredecessorLink 僅在前置任務同樣存在於本檔案中時才輸出（避免產生指向
      不存在 UID 的懸空參照）。
    """
    hpd = interop.hours_per_day if interop.hours_per_day and interop.hours_per_day > 0 else 8.0
    start = interop.start_date or date.today()
    work_days = interop.work_days or _DEFAULT_WORK_DAYS
    holidays = interop.holidays or set()

    task_defs: list[TaskDefinition] = []
    for t in interop.tasks:
        task_defs.append(
            TaskDefinition(
                task_id=t.task_id,
                task_name=t.task_name,
                duration=max(0, int(t.duration_days or 0)),
                links=[
                    DependencyLink(
                        predecessor_task_id=link.predecessor_task_id,
                        dep_type=link.dep_type,
                        lag_days=link.lag_days,
                    )
                    for link in t.links
                ],
                status=t.status,
                wbs_code=t.wbs_code,
                constraint_type=t.constraint_type,
                constraint_day=t.constraint_day,
            )
        )
    try:
        results = calculate_cpm(task_defs) if task_defs else {}
    except ValueError:
        # 防禦性處理：手動組裝的 InteropProject 若含循環相依 / 懸空前置等
        # CPM 引擎會拒絕的問題，仍應輸出「可開啟」的檔案（退化為無排程）。
        results = {}

    children_map: dict[str | None, list[InteropWbsNode]] = defaultdict(list)
    for node in interop.wbs:
        children_map[node.parent_code].append(node)
    for lst in children_map.values():
        lst.sort(key=lambda n: (n.sort_order, n.wbs_code))

    tasks_by_wbs: dict[str | None, list[InteropTask]] = defaultdict(list)
    for t in interop.tasks:
        tasks_by_wbs[t.wbs_code].append(t)

    def _rollup(code: str) -> tuple[int, int]:
        """回傳 (min es, max ef)；此節點（含子節點）底下無任務時回傳 (0, 0)。"""
        es_values: list[int] = []
        ef_values: list[int] = []
        for t in tasks_by_wbs.get(code, []):
            r = results.get(t.task_id)
            if r is not None:
                es_values.append(r.es)
                ef_values.append(r.ef)
        for child in children_map.get(code, []):
            c_es, c_ef = _rollup(child.wbs_code)
            es_values.append(c_es)
            ef_values.append(c_ef)
        if not es_values:
            return 0, 0
        return min(es_values), max(ef_values)

    lines: list[str] = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        f'<Project xmlns="{_NS}">',
        f"  <Name>{_esc(interop.name or '')}</Name>",
        f"  <StartDate>{start.isoformat()}T08:00:00</StartDate>",
        "  <SaveVersion>1</SaveVersion>",
        "  <Tasks>",
    ]

    # UID 配置：先給所有「一般任務」配置穩定遞增整數 UID（PredecessorLink
    # 需要以整數參照），再接續配置給 WBS 摘要節點。任務原始 task_id
    # （可能非數字）不強制對應 UID —— 對照聯絡點見 header 註解。
    task_uid_by_id: dict[str, int] = {}
    next_uid = 1
    for t in interop.tasks:
        task_uid_by_id[t.task_id] = next_uid
        next_uid += 1
    wbs_uid: dict[str, int] = {}
    for node in interop.wbs:
        wbs_uid[node.wbs_code] = next_uid
        next_uid += 1

    row_id = [0]

    def _emit_task(t: InteropTask, level: int, outline: str) -> None:
        row_id[0] += 1
        r = results.get(t.task_id)
        es_off = r.es if r is not None else 0
        ef_off = r.ef if r is not None else max(es_off, t.duration_days)
        s_date = offset_to_date(start, es_off, work_days, holidays)
        f_date = offset_to_date(start, max(es_off, ef_off), work_days, holidays)
        is_milestone = (t.duration_days or 0) == 0

        lines.append("    <Task>")
        lines.append(f"      <UID>{task_uid_by_id[t.task_id]}</UID>")
        lines.append(f"      <ID>{row_id[0]}</ID>")
        lines.append(f"      <Name>{_esc(t.task_name or '')}</Name>")
        lines.append(f"      <OutlineLevel>{level}</OutlineLevel>")
        # 一般任務：WBS 與 OutlineNumber 皆為位置式大綱編號（MS Project 預設
        # WBS = OutlineNumber）；parse 端由其父段反查所屬摘要節點的顯示代碼。
        lines.append(f"      <WBS>{_esc(outline)}</WBS>")
        lines.append(f"      <OutlineNumber>{_esc(outline)}</OutlineNumber>")
        lines.append("      <Summary>0</Summary>")
        lines.append(f"      <Milestone>{1 if is_milestone else 0}</Milestone>")
        lines.append(f"      <Start>{s_date.isoformat()}T08:00:00</Start>")
        lines.append(f"      <Finish>{f_date.isoformat()}T17:00:00</Finish>")
        lines.append(
            f"      <Duration>PT{int(round((t.duration_days or 0) * hpd))}H0M0S</Duration>"
        )
        ctype_num = _CONSTRAINT_EXPORT.get(t.constraint_type, 0)
        lines.append(f"      <ConstraintType>{ctype_num}</ConstraintType>")
        if t.constraint_type is not None and t.constraint_day is not None:
            c_date = offset_to_date(start, t.constraint_day, work_days, holidays)
            lines.append(f"      <ConstraintDate>{c_date.isoformat()}T08:00:00</ConstraintDate>")
        for link in t.links:
            pred_uid = task_uid_by_id.get(link.predecessor_task_id)
            if pred_uid is None:
                continue  # 懸空前置參照（檔案外）：不輸出，避免產生無效 UID 參照
            type_num = _LINK_TYPE_EXPORT.get(link.dep_type, 1)
            lag_tenths = int(round(link.lag_days * hpd * 60 * 10))
            lines.append("      <PredecessorLink>")
            lines.append(f"        <PredecessorUID>{pred_uid}</PredecessorUID>")
            lines.append(f"        <Type>{type_num}</Type>")
            lines.append("        <CrossProject>0</CrossProject>")
            lines.append(f"        <LinkLag>{lag_tenths}</LinkLag>")
            lines.append("        <LagFormat>7</LagFormat>")
            lines.append("      </PredecessorLink>")
        lines.append("    </Task>")

    def _emit_summary(node: InteropWbsNode, level: int, outline: str) -> None:
        row_id[0] += 1
        es_off, ef_off = _rollup(node.wbs_code)
        s_date = offset_to_date(start, es_off, work_days, holidays)
        f_date = offset_to_date(start, ef_off, work_days, holidays)
        duration_days = max(0, ef_off - es_off)

        lines.append("    <Task>")
        lines.append(f"      <UID>{wbs_uid[node.wbs_code]}</UID>")
        lines.append(f"      <ID>{row_id[0]}</ID>")
        lines.append(f"      <Name>{_esc(node.name or '')}</Name>")
        lines.append(f"      <OutlineLevel>{level}</OutlineLevel>")
        # 摘要任務：WBS = 我方顯示代碼（可能是自訂遮罩），OutlineNumber =
        # 位置式大綱編號（保證 "." 分段）—— parse 端兩者解耦、精確互逆。
        lines.append(f"      <WBS>{_esc(node.wbs_code)}</WBS>")
        lines.append(f"      <OutlineNumber>{_esc(outline)}</OutlineNumber>")
        lines.append("      <Summary>1</Summary>")
        lines.append("      <Milestone>0</Milestone>")
        lines.append(f"      <Start>{s_date.isoformat()}T08:00:00</Start>")
        lines.append(f"      <Finish>{f_date.isoformat()}T17:00:00</Finish>")
        lines.append(f"      <Duration>PT{int(round(duration_days * hpd))}H0M0S</Duration>")
        lines.append("    </Task>")

        child_seq = 0
        for t in sorted(tasks_by_wbs.get(node.wbs_code, []), key=lambda tt: tt.task_id):
            child_seq += 1
            _emit_task(t, level + 1, f"{outline}.{child_seq}")
        for child in children_map.get(node.wbs_code, []):
            child_seq += 1
            _emit_summary(child, level + 1, f"{outline}.{child_seq}")

    # 依樹狀順序輸出：根層級 WBS 摘要節點（遞迴含其下任務與子節點），
    # 最後輸出未指派 WBS 的頂層任務。大綱編號依樹狀位置遞增。
    top_seq = 0
    for root_node in children_map.get(None, []):
        top_seq += 1
        _emit_summary(root_node, 1, str(top_seq))
    for t in sorted(tasks_by_wbs.get(None, []), key=lambda tt: tt.task_id):
        top_seq += 1
        _emit_task(t, 1, str(top_seq))

    lines.append("  </Tasks>")
    lines.append("</Project>")
    return "\n".join(lines)
