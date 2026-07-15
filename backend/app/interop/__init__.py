"""互通格式 (interop) 共用契約 —— 純資料結構，無 DB / 無 FastAPI 依賴。

P6 XER 與 MS Project MSPDI 兩個匯入/匯出解析器 (xer.py / mspdi.py) 皆以
本檔案定義的 dataclass 作為共同的中介表示 (intermediate representation)：
    檔案格式 --parse--> InteropProject --generate--> 檔案格式

欄位刻意貼近 app.schemas.schedule 的既有命名慣例 (task_id / dep_type /
lag_days / wbs_code / parent_code / sort_order / constraint_type /
constraint_day)，方便呼叫端 (routers/interop.py) 與 TaskDefinition /
WbsNode 之間直接對應，不需要額外的欄位改名轉換層。

注意：這裡的 dataclass 是「檔案格式」層的中介物件，*不是* API 契約本身；
routers/interop.py 匯入/匯出時負責在 InteropProject 與
app.schemas.schedule.TaskDefinition / WbsNode 之間轉換。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

__all__ = [
    "InteropLink",
    "InteropWbsNode",
    "InteropTask",
    "InteropProject",
]


@dataclass
class InteropLink(object):
    """單一相依連結 (dependency link)：對應 schemas.schedule.DependencyLink。"""

    predecessor_task_id: str
    dep_type: str = "FS"
    lag_days: int = 0


@dataclass
class InteropWbsNode(object):
    """WBS 節點：對應 schemas.schedule.WbsNode。"""

    wbs_code: str
    name: str = ""
    parent_code: str | None = None
    sort_order: int = 0


@dataclass
class InteropTask(object):
    """單一任務：對應 schemas.schedule.TaskDefinition 的匯入/匯出中介表示。

    Pro Batch E (FEATURE E3)：percent_complete / actual_start / actual_finish
    承載「實績」(actuals) —— 匯入端讀取來源檔案的完成度與實際起訖日，匯出端
    自 task_progress 回填，供 XER / MSPDI 匯入匯出往返 (round-trip)。
    """

    task_id: str
    task_name: str = ""
    duration_days: int = 0
    wbs_code: str | None = None
    status: str = "PENDING"
    constraint_type: str | None = None
    constraint_day: int | None = None
    links: list[InteropLink] = field(default_factory=list)
    percent_complete: int = 0
    actual_start: date | None = None
    actual_finish: date | None = None


@dataclass
class InteropProject(object):
    """整個匯入/匯出檔案的中介表示。

    work_days / holidays（匯出端行事曆）：
      匯出時（generate_xer / generate_mspdi）所有「工作日偏移 -> 實際日期」
      的換算（任務 Start/Finish、限制日期 cstr_date/ConstraintDate、WBS 彙總）
      一律採用這兩個欄位 —— 呼叫端（routers/interop.py）自專案本身的
      work_days 與例外假日（ProjectHoliday）載入，確保匯出檔案的日期與
      系統內看到的日期一致。匯入端（parse_*）產生的 InteropProject 沿用
      預設值（與匯入建立專案所用的 DEFAULT_IMPORT_WORK_DAYS 一致）。
    """

    name: str = ""
    start_date: date | None = None
    hours_per_day: float = 8.0
    wbs: list[InteropWbsNode] = field(default_factory=list)
    tasks: list[InteropTask] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    work_days: str = "1111100"
    holidays: set[date] = field(default_factory=set)
