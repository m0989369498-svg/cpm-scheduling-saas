"""P6 XER + MS Project MSPDI 匯入/匯出（interop）測試。

Pro Batch A / interop：

  純解析模組（無 DB、無 FastAPI）：
    app.interop.xer   .parse_xer(text, *, hours_per_day=8.0) -> InteropProject
                       .generate_xer(interop, *, hours_per_day=8.0) -> str
    app.interop.mspdi .parse_mspdi(xml_text) -> InteropProject
                       .generate_mspdi(interop) -> str
    共用資料結構 InteropProject{name, start_date, hours_per_day, wbs, tasks, warnings}
    InteropTask{task_id, task_name, duration_days, wbs_code, status,
                constraint_type, constraint_day, links}。

  API（editor+ 匯入 / viewer 亦可匯出）：
    POST /projects/import                    multipart file 匯入（xer/mspdi 皆可，auto 偵測）
    GET  /projects/{pid}/export.xer          text/plain 附件
    GET  /projects/{pid}/export.mspdi.xml    application/xml 附件

本檔涵蓋（依指派契約）：
  (a) XER 黃金樣本（golden fixture）：PROJECT + PROJWBS 2 節點 + 3 TASK（跨 2 個 WBS），
      8h/日工期 40/24/16 小時 -> 5/3/2 天，TASKPRED 一條 FS + 一條 SS（lag 16hr -> 2 天），
      一個任務帶 CS_MSOB 限制（-> SNLT）—— 逐欄位比對且 warnings 應為空。
  (b) MSPDI 黃金樣本：OutlineLevel/OutlineNumber 階層（WBS 元素缺席時由此推導）、
      PT16H0M0S 工期、PredecessorLink Type 3（SS）+ LinkLag 4800（=8hr=1天）、
      ConstraintType 4 -> SNET。
  (c) 安全性：MSPDI 內容含 <!DOCTYPE 或 <!ENTITY（大小寫不拘）-> parse_mspdi 拋 ValueError
      （XXE / billion-laughs 防護），先於任何 XML 剖析之前即擋下。
  (d) 往返（round-trip）：以 API 建立含 WBS + links + 限制的專案 -> 匯出 .xer / .mspdi.xml
      -> 以該匯出內容匯入為新專案 -> 任務數 / 工期 / 相依型態 / 限制型態存活。
  (e) 端點測試：multipart 上傳黃金樣本 (a) -> 200，report 計數正確，ProjectOut 含 wbs 且
      CPM 正確（SS 相依任務的 es 確實吃到 lag）；超大檔案（>10MB）-> 413；壞檔案（無法判斷
      格式）-> 422；task_id 於檔案內重複 -> 422；viewer 匯入 -> 403；cp950 編碼內容
      （utf-8 解碼會失敗）匯入仍 200（fallback 解碼成功）。

額外覆蓋（超出指派清單，強化契約邊界）：
  * CALENDAR.day_hr_cnt 覆寫 hours_per_day。
  * 未知資料表（unknown table）與欄位數值異常（malformed numeric）-> 警告而非例外。
  * MSPDI 里程碑（Milestone / Duration 0）-> duration_days 0。
  * MSPDI PredecessorLink 缺少 <Type> -> 預設 FS。

關鍵設計（與 test_wbs_baselines.py / test_dashboard_export.py 完全一致）：
  1. 「import app 之前」即設定 DATABASE_URL=sqlite 檔案 + DEV_BOOTSTRAP=1。
  2. client fixture 以 _rebind_sqlite_engine() 將整個 DB 層重綁至本檔 sqlite 檔，
     teardown 時 dispose engine、還原全域綁定、再刪暫存檔（Windows 句柄安全）。
  3. 純解析器測試（不需 DB）直接呼叫 parse_xer / parse_mspdi，與 client fixture 無關，
     但仍受益於同一支檔案於「app 匯入之後」才 import interop 模組（避免與 sqlite
     bootstrap 順序打架）。
  4. interop 巢狀清單項目（wbs / links）之型別（dict 或 dataclass）未被契約完全鎖定，
     一律以 `_get()` 存取（dict 與屬性皆相容），降低測試與實作內部型別選擇的耦合。
"""
from __future__ import annotations

import os
import tempfile
from datetime import date

# --------------------------------------------------------------------------- #
# 於匯入 app 之前先設定環境變數 (env at top before importing app)。
# 使用具名暫存「檔案」作為 sqlite DB，確保跨連線共享資料且測試可重現。
# --------------------------------------------------------------------------- #
_DB_FD, _DB_PATH = tempfile.mkstemp(prefix="cpm_interop_test_", suffix=".db")
os.close(_DB_FD)
_DB_URL = "sqlite+aiosqlite:///" + _DB_PATH.replace("\\", "/")
os.environ["DATABASE_URL"] = _DB_URL
os.environ["DEV_BOOTSTRAP"] = "1"
os.environ.setdefault("AUTH_REQUIRED", "false")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402

# 純解析模組（no DB / no FastAPI）。
from app.interop import (  # noqa: E402
    InteropLink,
    InteropProject,
    InteropTask,
    InteropWbsNode,
)
from app.interop.xer import generate_xer, parse_xer  # noqa: E402
from app.interop.mspdi import parse_mspdi  # noqa: E402

PREFIX = settings.api_v1_prefix
LOGIN_URL = f"{PREFIX}/auth/login"
PROJECTS_URL = f"{PREFIX}/projects"
IMPORT_URL = f"{PROJECTS_URL}/import"

DEMO_PASSWORD = "demo1234"
MSPDI_NS = "http://schemas.microsoft.com/project"


# --------------------------------------------------------------------------- #
# sqlite rebind / teardown 工具（與 test_wbs_baselines.py 同法）
# --------------------------------------------------------------------------- #
def _rebind_sqlite_engine() -> None:
    """把 app 的 DB 層強制指向本檔的 sqlite 暫存檔。"""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.ext.asyncio import AsyncSession

    import app.database as database

    settings.database_url = _DB_URL
    settings.dev_bootstrap = True

    new_engine = create_async_engine(
        _DB_URL,
        echo=False,
        future=True,
        connect_args={"check_same_thread": False},
        execution_options={"schema_translate_map": {"erp_integration": None}},
    )
    new_sessionmaker = async_sessionmaker(
        bind=new_engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )

    database._engine = new_engine
    database._SessionLocal = new_sessionmaker
    database.engine = new_engine
    database.SessionLocal = new_sessionmaker

    import app.deps as deps
    import app.routers.auth as auth_router_mod

    deps.SessionLocal = new_sessionmaker
    auth_router_mod.SessionLocal = new_sessionmaker


def _dispose_sqlite_engines() -> None:
    """釋放所有綁到本檔 sqlite 暫存檔的 async engine（Windows 釋放檔案句柄）。"""
    import asyncio

    import app.database as database

    async def _dispose_all() -> None:
        engines = []
        try:
            engines.append(database.get_engine())
        except Exception:
            pass
        rebound = database.__dict__.get("engine")
        if rebound is not None and rebound not in engines:
            engines.append(rebound)
        for eng in engines:
            try:
                await eng.dispose()
            except Exception:
                pass

    try:
        asyncio.run(_dispose_all())
    except Exception:
        pass


def _unlink_with_retry(path: str, attempts: int = 10, delay: float = 0.05) -> None:
    """刪除暫存 sqlite 檔，對 Windows 句柄釋放延遲做短暫重試。"""
    import gc
    import time

    gc.collect()
    for _ in range(attempts):
        try:
            os.unlink(path)
            return
        except FileNotFoundError:
            return
        except OSError:
            time.sleep(delay)
    try:
        os.unlink(path)
    except OSError:
        pass


_MISSING = object()


def _snapshot_app_db_state() -> dict:
    """快照 _rebind_sqlite_engine()/lifespan 會變動的全域 DB 綁定（避免污染後續測試）。"""
    import app.database as database
    import app.deps as deps
    import app.routers.auth as auth_router_mod

    snap: dict = {
        "database_url": settings.database_url,
        "dev_bootstrap": settings.dev_bootstrap,
        "_engine": database.__dict__.get("_engine"),
        "_SessionLocal": database.__dict__.get("_SessionLocal"),
        "attrs": {},
    }
    for mod, name in (
        (database, "engine"),
        (database, "SessionLocal"),
        (deps, "SessionLocal"),
        (auth_router_mod, "SessionLocal"),
    ):
        snap["attrs"][(mod.__name__, name)] = mod.__dict__.get(name, _MISSING)
    return snap


def _restore_app_db_state(snap: dict) -> None:
    """還原 _snapshot_app_db_state() 取得的全域 DB 綁定。"""
    import importlib

    import app.database as database

    settings.database_url = snap["database_url"]
    settings.dev_bootstrap = snap["dev_bootstrap"]
    database._engine = snap["_engine"]
    database._SessionLocal = snap["_SessionLocal"]
    for (modname, name), val in snap["attrs"].items():
        mod = importlib.import_module(modname)
        if val is _MISSING:
            mod.__dict__.pop(name, None)
        else:
            setattr(mod, name, val)


@pytest.fixture(scope="module")
def client():
    """module 範圍 TestClient；先把 DB 綁到 sqlite 暫存檔，再以 with 進入觸發 lifespan
    (create_all + 種核心資料 + 種 app users)。結束後 dispose、還原全域狀態、刪暫存檔。
    """
    snap = _snapshot_app_db_state()
    try:
        _rebind_sqlite_engine()
        with TestClient(app) as c:
            yield c
    finally:
        _dispose_sqlite_engines()
        _restore_app_db_state(snap)
        _unlink_with_retry(_DB_PATH)


# --------------------------------------------------------------------------- #
# 登入小工具
# --------------------------------------------------------------------------- #
def _login(client: TestClient, username: str, password: str) -> dict:
    resp = client.post(LOGIN_URL, json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _headers_for(client: TestClient, username: str) -> dict[str, str]:
    token = _login(client, username, DEMO_PASSWORD)["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _admin_headers(client: TestClient) -> dict[str, str]:
    return _headers_for(client, "admin@tw")


def _editor_headers(client: TestClient) -> dict[str, str]:
    return _headers_for(client, "editor@tw")


def _viewer_headers(client: TestClient) -> dict[str, str]:
    return _headers_for(client, "viewer@tw")


# --------------------------------------------------------------------------- #
# 通用存取小工具：interop 巢狀項目 (wbs / links) 型別未鎖定 (dict 或屬性物件)，
# 兩者皆相容，避免測試與實作的內部型別選擇過度耦合。
# --------------------------------------------------------------------------- #
def _get(obj, key: str, default=None):
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


# =============================================================================
# XER 樣本產生小工具（tab-delimited；以陣列 join 組字串，避免編輯器誤把 tab 轉空白）
# =============================================================================
def _xer_table(name: str, fields: list[str], rows: list[list[str]]) -> str:
    lines = [f"%T\t{name}", "%F\t" + "\t".join(fields)]
    for row in rows:
        lines.append("%R\t" + "\t".join(row))
    return "\n".join(lines)


def _xer_doc(*tables: str) -> str:
    header = (
        "ERMHDR\t20.12\t2026-07-13\tProject\tadmin\tadmin\t"
        "Project Management\tdd/mm/yyyy\tTWD"
    )
    return "\n".join([header, *tables]) + "\n"


def _xer_fixture_a() -> str:
    """黃金樣本 (a)：PROJECT + CALENDAR + PROJWBS(2 節點) + TASK(3, 跨 2 個 wbs) + TASKPRED。

    專案開工日 2026-07-06（週一），限制日 2026-07-10（週五）—— 皆落在同一週的
    週一至週五，故 date_to_offset 換算 offset=4，不受實作對 work_days 遮罩的
    預設假設（六日或五日工作週）影響，兩者皆得到相同 offset。
    """
    project_tbl = _xer_table(
        "PROJECT",
        ["proj_id", "proj_short_name", "plan_start_date"],
        [["1", "DEMO-XER", "2026-07-06 00:00"]],
    )
    calendar_tbl = _xer_table(
        "CALENDAR",
        ["clndr_id", "clndr_name", "day_hr_cnt"],
        [["1", "Standard", "8"]],
    )
    projwbs_tbl = _xer_table(
        "PROJWBS",
        ["wbs_id", "proj_id", "parent_wbs_id", "wbs_short_name", "wbs_name"],
        [
            # 根節點（代表專案本身）—— 不應成為 wbs 節點。
            ["100", "1", "", "DEMO-XER", "Demo XER 專案"],
            ["101", "1", "100", "1", "土建工程"],
            ["102", "1", "100", "2", "機電工程"],
        ],
    )
    task_tbl = _xer_table(
        "TASK",
        [
            "task_id", "proj_id", "wbs_id", "task_code", "task_name",
            "status_code", "target_drtn_hr_cnt", "cstr_type", "cstr_date",
        ],
        [
            ["1001", "1", "101", "T-01", "基礎開挖", "TK_NotStart", "40", "", ""],
            ["1002", "1", "101", "T-02", "鋼筋綁紮", "TK_Complete", "24", "", ""],
            ["1003", "1", "102", "T-03", "機電安裝", "TK_Active", "16",
             "CS_MSOB", "2026-07-10 00:00"],
        ],
    )
    taskpred_tbl = _xer_table(
        "TASKPRED",
        ["task_pred_id", "task_id", "pred_task_id", "proj_id", "pred_type", "lag_hr_cnt"],
        [
            ["1", "1002", "1001", "1", "PR_FS", "0"],
            ["2", "1003", "1001", "1", "PR_SS", "16"],
        ],
    )
    return _xer_doc(project_tbl, calendar_tbl, projwbs_tbl, task_tbl, taskpred_tbl)


# =============================================================================
# MSPDI 樣本產生小工具
# =============================================================================
def _mspdi_fixture_b() -> str:
    """黃金樣本 (b)：OutlineLevel/OutlineNumber 階層（WBS 元素缺席，由 OutlineNumber 推導），
    PT16H0M0S 工期，PredecessorLink Type 3（SS）+ LinkLag 4800（=8hr=1天），
    ConstraintType 4 -> SNET，ConstraintDate 落於同週（與 XER 樣本相同換算邏輯）。
    """
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Project xmlns="{MSPDI_NS}">
  <Name>DEMO-MSPDI</Name>
  <StartDate>2026-07-06T08:00:00</StartDate>
  <Tasks>
    <Task>
      <UID>1</UID>
      <ID>1</ID>
      <Name>土建工程</Name>
      <OutlineLevel>1</OutlineLevel>
      <OutlineNumber>1</OutlineNumber>
      <Summary>1</Summary>
    </Task>
    <Task>
      <UID>2</UID>
      <ID>2</ID>
      <Name>基礎開挖</Name>
      <OutlineLevel>2</OutlineLevel>
      <OutlineNumber>1.1</OutlineNumber>
      <Summary>0</Summary>
      <Duration>PT40H0M0S</Duration>
      <ConstraintType>0</ConstraintType>
    </Task>
    <Task>
      <UID>3</UID>
      <ID>3</ID>
      <Name>機電工程</Name>
      <OutlineLevel>1</OutlineLevel>
      <OutlineNumber>2</OutlineNumber>
      <Summary>1</Summary>
    </Task>
    <Task>
      <UID>4</UID>
      <ID>4</ID>
      <Name>機電安裝</Name>
      <OutlineLevel>2</OutlineLevel>
      <OutlineNumber>2.1</OutlineNumber>
      <Summary>0</Summary>
      <Duration>PT16H0M0S</Duration>
      <ConstraintType>4</ConstraintType>
      <ConstraintDate>2026-07-10T00:00:00</ConstraintDate>
      <PredecessorLink>
        <PredecessorUID>2</PredecessorUID>
        <Type>3</Type>
        <LinkLag>4800</LinkLag>
      </PredecessorLink>
    </Task>
  </Tasks>
</Project>"""


# =============================================================================
# (a) XER 純解析（pure parser）測試 —— 無需 DB
# =============================================================================
def test_parse_xer_golden_fixture_maps_every_field_no_warnings():
    interop = parse_xer(_xer_fixture_a())

    assert interop.name == "DEMO-XER"
    assert interop.start_date == date(2026, 7, 6)
    assert interop.hours_per_day == 8.0
    assert interop.warnings == []

    # PROJWBS：根節點（代表專案本身）不應成為 wbs 節點 —— 僅 2 個子節點。
    # wbs_code 採 wbs_short_name（非 wbs_id）—— 與 TASK.wbs_id 透過 PROJWBS
    # 反查 wbs_short_name 取得的 wbs_code 一致。
    assert len(interop.wbs) == 2
    wbs_by_code = {_get(w, "wbs_code"): w for w in interop.wbs}
    assert set(wbs_by_code) == {"1", "2"}
    assert _get(wbs_by_code["1"], "name") == "土建工程"
    assert _get(wbs_by_code["2"], "name") == "機電工程"
    # 父節點即根節點（已被排除）-> parent_code 應為 None（避免懸空參照）。
    assert _get(wbs_by_code["1"], "parent_code") is None
    assert _get(wbs_by_code["2"], "parent_code") is None

    assert len(interop.tasks) == 3
    tasks_by_id = {t.task_id: t for t in interop.tasks}
    assert set(tasks_by_id) == {"T-01", "T-02", "T-03"}

    t01 = tasks_by_id["T-01"]
    assert t01.task_name == "基礎開挖"
    assert t01.duration_days == 5  # 40hr / 8hr = 5 天
    assert t01.wbs_code == "1"
    assert t01.status == "PENDING"  # TK_NotStart
    assert t01.constraint_type is None
    assert t01.constraint_day is None
    assert not t01.links

    t02 = tasks_by_id["T-02"]
    assert t02.task_name == "鋼筋綁紮"
    assert t02.duration_days == 3  # 24hr / 8hr = 3 天
    assert t02.wbs_code == "1"
    assert t02.status == "COMPLETED"  # TK_Complete
    assert t02.constraint_type is None
    assert len(t02.links) == 1
    link02 = t02.links[0]
    assert _get(link02, "predecessor_task_id") == "T-01"
    assert _get(link02, "dep_type") == "FS"
    assert _get(link02, "lag_days") == 0

    t03 = tasks_by_id["T-03"]
    assert t03.task_name == "機電安裝"
    assert t03.duration_days == 2  # 16hr / 8hr = 2 天
    assert t03.wbs_code == "2"
    assert t03.status == "IN_PROGRESS"  # TK_Active
    assert t03.constraint_type == "SNLT"  # CS_MSOB -> SNLT
    assert t03.constraint_day == 4  # 2026-07-06(週一,offset0) -> 07-10(週五,offset4)
    assert len(t03.links) == 1
    link03 = t03.links[0]
    assert _get(link03, "predecessor_task_id") == "T-01"
    assert _get(link03, "dep_type") == "SS"
    assert _get(link03, "lag_days") == 2  # 16hr lag / 8hr = 2 天


def test_parse_xer_calendar_day_hr_cnt_overrides_hours_per_day():
    project_tbl = _xer_table(
        "PROJECT", ["proj_id", "proj_short_name", "plan_start_date"],
        [["1", "CAL-OVERRIDE", "2026-07-06 00:00"]],
    )
    calendar_tbl = _xer_table(
        "CALENDAR", ["clndr_id", "clndr_name", "day_hr_cnt"],
        [["1", "Standard4h", "4"]],
    )
    task_tbl = _xer_table(
        "TASK",
        ["task_id", "proj_id", "task_code", "task_name", "status_code", "target_drtn_hr_cnt"],
        [["1001", "1", "T-01", "半日工作", "TK_NotStart", "8"]],
    )
    text = _xer_doc(project_tbl, calendar_tbl, task_tbl)

    interop = parse_xer(text, hours_per_day=8.0)  # 傳入的預設值應被 CALENDAR 覆寫。
    assert interop.hours_per_day == 4.0
    task = interop.tasks[0]
    assert task.duration_days == 2  # 8hr / 4hr(覆寫後) = 2 天


def test_parse_xer_unknown_table_and_malformed_numeric_warn_not_raise():
    project_tbl = _xer_table(
        "PROJECT", ["proj_id", "proj_short_name", "plan_start_date"],
        [["1", "WARN-TEST", "2026-07-06 00:00"]],
    )
    # 未知資料表：應被忽略並記警告，絕不拋出例外。
    unknown_tbl = _xer_table(
        "RSRC", ["rsrc_id", "rsrc_name"], [["1", "起重機"]],
    )
    task_tbl = _xer_table(
        "TASK",
        ["task_id", "proj_id", "task_code", "task_name", "status_code", "target_drtn_hr_cnt"],
        [
            ["1001", "1", "T-01", "正常任務", "TK_NotStart", "8"],
            # 工期欄位非數字（malformed）-> 應記警告 + 給預設值，而非拋出例外。
            ["1002", "1", "T-02", "異常工期任務", "TK_NotStart", "N/A"],
        ],
    )
    text = _xer_doc(project_tbl, unknown_tbl, task_tbl)

    interop = parse_xer(text)  # 不應拋出例外。
    assert len(interop.warnings) >= 1

    tasks_by_id = {t.task_id: t for t in interop.tasks}
    assert tasks_by_id["T-01"].duration_days == 1  # 8hr / 8hr = 1 天，正常解析。
    assert tasks_by_id["T-02"].duration_days == 0  # 無法解析 -> 合理預設值。


# =============================================================================
# (b) MSPDI 純解析（pure parser）測試 —— 無需 DB
# =============================================================================
def test_parse_mspdi_golden_fixture_outline_hierarchy_and_ss_predecessor():
    interop = parse_mspdi(_mspdi_fixture_b())

    assert interop.name == "DEMO-MSPDI"
    assert interop.start_date == date(2026, 7, 6)

    # 摘要任務 (Summary=1) 成為 WBS 節點；非摘要任務成為一般任務。
    assert len(interop.tasks) == 2
    tasks_by_id = {t.task_id: t for t in interop.tasks}
    assert set(tasks_by_id) == {"2", "4"}

    assert len(interop.wbs) == 2
    wbs_by_code = {_get(w, "wbs_code"): w for w in interop.wbs}
    assert set(wbs_by_code) == {"1", "2"}
    assert _get(wbs_by_code["1"], "name") == "土建工程"
    assert _get(wbs_by_code["2"], "name") == "機電工程"
    assert _get(wbs_by_code["1"], "parent_code") is None
    assert _get(wbs_by_code["2"], "parent_code") is None

    t2 = tasks_by_id["2"]
    assert t2.task_name == "基礎開挖"
    assert t2.duration_days == 5  # PT40H0M0S / 8hr = 5 天
    assert t2.wbs_code == "1"
    assert t2.constraint_type is None  # ConstraintType 0 (ASAP) -> None
    assert t2.constraint_day is None
    assert not t2.links

    t4 = tasks_by_id["4"]
    assert t4.task_name == "機電安裝"
    assert t4.duration_days == 2  # PT16H0M0S / 8hr = 2 天
    assert t4.wbs_code == "2"
    assert t4.constraint_type == "SNET"  # ConstraintType 4 -> SNET
    assert t4.constraint_day == 4  # 同 XER 樣本換算邏輯
    assert len(t4.links) == 1
    link = t4.links[0]
    assert _get(link, "predecessor_task_id") == "2"
    assert _get(link, "dep_type") == "SS"  # Type 3 -> SS
    assert _get(link, "lag_days") == 1  # LinkLag 4800 tenths-min = 8hr = 1 天


def test_parse_mspdi_milestone_duration_zero():
    xml_text = f"""<?xml version="1.0" encoding="UTF-8"?>
<Project xmlns="{MSPDI_NS}">
  <Name>MILESTONE-TEST</Name>
  <StartDate>2026-07-06T08:00:00</StartDate>
  <Tasks>
    <Task>
      <UID>1</UID>
      <Name>驗收里程碑</Name>
      <OutlineLevel>1</OutlineLevel>
      <OutlineNumber>1</OutlineNumber>
      <Summary>0</Summary>
      <Duration>PT0H0M0S</Duration>
      <Milestone>1</Milestone>
    </Task>
  </Tasks>
</Project>"""
    interop = parse_mspdi(xml_text)
    assert len(interop.tasks) == 1
    assert interop.tasks[0].duration_days == 0


def test_parse_mspdi_predecessor_type_absent_defaults_to_fs():
    xml_text = f"""<?xml version="1.0" encoding="UTF-8"?>
<Project xmlns="{MSPDI_NS}">
  <Name>DEFAULT-FS-TEST</Name>
  <StartDate>2026-07-06T08:00:00</StartDate>
  <Tasks>
    <Task>
      <UID>1</UID>
      <Name>A</Name>
      <OutlineLevel>1</OutlineLevel>
      <OutlineNumber>1</OutlineNumber>
      <Summary>0</Summary>
      <Duration>PT8H0M0S</Duration>
    </Task>
    <Task>
      <UID>2</UID>
      <Name>B</Name>
      <OutlineLevel>1</OutlineLevel>
      <OutlineNumber>2</OutlineNumber>
      <Summary>0</Summary>
      <Duration>PT8H0M0S</Duration>
      <PredecessorLink>
        <PredecessorUID>1</PredecessorUID>
      </PredecessorLink>
    </Task>
  </Tasks>
</Project>"""
    interop = parse_mspdi(xml_text)
    tasks_by_id = {t.task_id: t for t in interop.tasks}
    b = tasks_by_id["2"]
    assert len(b.links) == 1
    assert _get(b.links[0], "dep_type") == "FS"


# =============================================================================
# (c) 安全性：DOCTYPE / ENTITY 注入防護（XXE / billion-laughs）
# =============================================================================
def test_parse_mspdi_doctype_rejected_value_error():
    malicious = (
        '<?xml version="1.0"?>'
        '<!DOCTYPE Project [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
        f'<Project xmlns="{MSPDI_NS}"><Name>&xxe;</Name></Project>'
    )
    with pytest.raises(ValueError):
        parse_mspdi(malicious)


def test_parse_mspdi_doctype_rejected_case_insensitive():
    malicious = (
        '<?xml version="1.0"?>'
        "<!doctype Project>"
        f'<Project xmlns="{MSPDI_NS}"/>'
    )
    with pytest.raises(ValueError):
        parse_mspdi(malicious)


def test_parse_mspdi_entity_without_doctype_rejected():
    # 即使不搭配 <!DOCTYPE（本身並非合法 XML），只要出現 <!ENTITY 子字串即應擋下。
    malicious = f'<!ENTITY foo "bar"><Project xmlns="{MSPDI_NS}"/>'
    with pytest.raises(ValueError):
        parse_mspdi(malicious)


# =============================================================================
# (e) 端點測試：POST /projects/import
# =============================================================================
def test_import_xer_endpoint_201_report_and_cpm_honors_ss_lag(client):
    headers = _editor_headers(client)
    resp = client.post(
        IMPORT_URL,
        headers=headers,
        files={"file": ("fixture_a.xer", _xer_fixture_a().encode("utf-8"), "text/plain")},
    )
    # 匯入建立「新」專案 -> 與 POST /projects 一致回 201 Created（非 200）。
    assert resp.status_code == 201, resp.text
    body = resp.json()

    report = body["report"]
    assert report["format"] == "xer"
    assert report["tasks"] == 3
    assert report["wbs"] == 2
    assert report["links"] == 2
    assert report["constraints"] == 1
    assert report["warnings"] == []

    project = body["project"]
    assert len(project["wbs"]) == 2
    tasks_by_id = {t["task_id"]: t for t in project["tasks"]}
    assert set(tasks_by_id) == {"T-01", "T-02", "T-03"}
    assert tasks_by_id["T-01"]["duration"] == 5
    assert tasks_by_id["T-02"]["duration"] == 3
    assert tasks_by_id["T-03"]["duration"] == 2
    assert tasks_by_id["T-03"]["constraint_type"] == "SNLT"

    # SS 相依（lag=2 天）：T-03.es 須確實吃到 lag（= T-01.es(0) + 2 = 2），而非 0。
    assert tasks_by_id["T-03"]["es"] == 2
    assert tasks_by_id["T-03"]["ef"] == 4


def test_import_oversized_file_413(client):
    headers = _editor_headers(client)
    # 刻意不依賴實作內部的大小限制常數名稱（尚未落地）：直接組出真正超過 10MB
    # 的上傳內容（前綴為合法樣本 + 大量填充位元組），驗證「超過上限 -> 413」
    # 這個文件化行為本身，而非耦合某個猜測的內部符號。
    oversized = _xer_fixture_a().encode("utf-8") + b"\n" + b"#" * (11 * 1024 * 1024)
    resp = client.post(
        IMPORT_URL,
        headers=headers,
        files={"file": ("huge.xer", oversized, "text/plain")},
        data={"format": "xer"},
    )
    assert resp.status_code == 413, resp.text


def test_import_bad_file_unrecognizable_format_422(client):
    headers = _editor_headers(client)
    # format=auto；檔名無 .xer/.xml 副檔名，內容既無 ERMHDR 開頭亦無 "<Project"
    # -> 無法判斷格式，依契約 auto 偵測規則應回 422（而非嘗試以任一解析器硬解）。
    resp = client.post(
        IMPORT_URL,
        headers=headers,
        files={"file": ("notes.txt", b"hello this is not p6 or ms project data", "text/plain")},
    )
    assert resp.status_code == 422, resp.text


def test_import_task_id_collision_within_file_422(client):
    headers = _editor_headers(client)
    project_tbl = _xer_table(
        "PROJECT", ["proj_id", "proj_short_name", "plan_start_date"],
        [["1", "DUP-TEST", "2026-07-06 00:00"]],
    )
    dup_task_tbl = _xer_table(
        "TASK",
        ["task_id", "proj_id", "task_code", "task_name", "status_code", "target_drtn_hr_cnt"],
        [
            ["1001", "1", "T-01", "重複任務A", "TK_NotStart", "8"],
            ["1002", "1", "T-01", "重複任務B", "TK_NotStart", "8"],
        ],
    )
    text = _xer_doc(project_tbl, dup_task_tbl)

    resp = client.post(
        IMPORT_URL,
        headers=headers,
        files={"file": ("dup.xer", text.encode("utf-8"), "text/plain")},
        data={"format": "xer"},
    )
    assert resp.status_code == 422, resp.text
    assert resp.json().get("detail")


def test_import_requires_editor_role_403_for_viewer(client):
    headers = _viewer_headers(client)
    resp = client.post(
        IMPORT_URL,
        headers=headers,
        files={"file": ("fixture_a.xer", _xer_fixture_a().encode("utf-8"), "text/plain")},
    )
    assert resp.status_code == 403, resp.text


def test_import_endpoint_decodes_cp950_fallback(client):
    headers = _editor_headers(client)
    project_tbl = _xer_table(
        "PROJECT", ["proj_id", "proj_short_name", "plan_start_date"],
        [["1", "CP950-TEST", "2026-07-06 00:00"]],
    )
    task_tbl = _xer_table(
        "TASK",
        ["task_id", "proj_id", "task_code", "task_name", "status_code", "target_drtn_hr_cnt"],
        [["1001", "1", "T-01", "土建工程基礎開挖", "TK_NotStart", "8"]],
    )
    text = _xer_doc(project_tbl, task_tbl)
    cp950_bytes = text.encode("cp950")

    # 自我檢核：確認此內容並非合法 utf-8，測試才真正走到 cp950 fallback 路徑。
    with pytest.raises(UnicodeDecodeError):
        cp950_bytes.decode("utf-8")

    resp = client.post(
        IMPORT_URL,
        headers=headers,
        files={"file": ("cp950.xer", cp950_bytes, "text/plain")},
        data={"format": "xer"},
    )
    assert resp.status_code == 201, resp.text
    tasks = resp.json()["project"]["tasks"]
    assert len(tasks) == 1
    assert tasks[0]["task_name"] == "土建工程基礎開挖"


# =============================================================================
# (d) 往返（round-trip）測試：API 建立 -> 匯出 -> 匯入為新專案 -> 內容存活
# =============================================================================
def _roundtrip_source_schedule() -> list[dict]:
    """往返測試共用來源任務清單：3 任務、跨 2 個 wbs、FS + SS(lag) 相依、1 個限制。"""
    return [
        {
            "task_id": "T-01", "task_name": "基礎開挖", "duration": 5,
            "predecessors": [], "status": "PENDING", "wbs_code": "1",
        },
        {
            "task_id": "T-02", "task_name": "鋼筋綁紮", "duration": 3,
            "links": [{"predecessor_task_id": "T-01", "dep_type": "FS", "lag_days": 0}],
            "status": "PENDING", "wbs_code": "1",
        },
        {
            "task_id": "T-03", "task_name": "機電安裝", "duration": 2,
            "links": [{"predecessor_task_id": "T-01", "dep_type": "SS", "lag_days": 2}],
            "status": "PENDING", "wbs_code": "2",
            "constraint_type": "SNET", "constraint_day": 1,
        },
    ]


def _create_roundtrip_source_project(client: TestClient, headers: dict, pid: str) -> None:
    create_resp = client.post(
        PROJECTS_URL,
        headers=headers,
        json={
            "project_id": pid,
            "project_name": f"匯出往返測試 {pid}",
            "region": "TW",
            # work_days 與匯入端點的預設值一致（1111100，週一至週五），確保
            # 匯出 -> 匯入為新專案後所用的行事曆一致，constraint_day 得以精確往返。
            "start_date": "2026-07-06",
            "work_days": "1111100",
            "schedule_data": _roundtrip_source_schedule(),
        },
    )
    assert create_resp.status_code == 201, create_resp.text

    wbs_payload = [
        {"wbs_code": "1", "name": "土建工程", "parent_code": None, "sort_order": 0},
        {"wbs_code": "2", "name": "機電工程", "parent_code": None, "sort_order": 1},
    ]
    put_wbs = client.put(f"{PROJECTS_URL}/{pid}/wbs", headers=headers, json=wbs_payload)
    assert put_wbs.status_code == 200, put_wbs.text


def _assert_roundtrip_survived(new_project: dict) -> None:
    """比對匯入後的專案內容與來源存活情形。

    以 task_name 索引（而非 task_id）：XER 的 task_code 會如實往返成我方
    task_id（"T-01" 等字面值不變），但 MSPDI 並無等價於 task_code 的欄位
    ——UID 為格式要求的整數，generate_mspdi 一律重新配置一組全新的遞增
    UID，reimport 後 task_id 會是全新的數字字串，非原始 "T-01"。task_name
    則兩種格式皆逐字保留，故以此作為任務身分的穩定比對鍵；相依關聯改以
    「動態查出前置任務的（新）task_id」比對，而非寫死 "T-01"。
    """
    tasks_by_name = {t["task_name"]: t for t in new_project["tasks"]}
    assert set(tasks_by_name) == {"基礎開挖", "鋼筋綁紮", "機電安裝"}

    excavation = tasks_by_name["基礎開挖"]
    rebar = tasks_by_name["鋼筋綁紮"]
    electrical = tasks_by_name["機電安裝"]

    assert excavation["duration"] == 5
    assert rebar["duration"] == 3
    assert electrical["duration"] == 2

    rebar_links = {l["predecessor_task_id"]: l for l in rebar["links"]}
    assert excavation["task_id"] in rebar_links
    assert rebar_links[excavation["task_id"]]["dep_type"] == "FS"

    electrical_links = {l["predecessor_task_id"]: l for l in electrical["links"]}
    assert excavation["task_id"] in electrical_links
    assert electrical_links[excavation["task_id"]]["dep_type"] == "SS"
    assert electrical_links[excavation["task_id"]]["lag_days"] == 2

    assert electrical["constraint_type"] == "SNET"
    assert electrical["constraint_day"] == 1

    # WBS 節點與「任務 -> WBS 歸屬」皆須存活（XER 以 wbs_short_name 往返；
    # MSPDI 以摘要任務 WBS 文字 + OutlineNumber 階層反查往返）。
    assert len(new_project["wbs"]) == 2
    assert {w["wbs_code"] for w in new_project["wbs"]} == {"1", "2"}
    assert excavation["wbs_code"] == "1"
    assert rebar["wbs_code"] == "1"
    assert electrical["wbs_code"] == "2"


def test_export_xer_then_reimport_preserves_tasks_links_constraints(client):
    headers = _admin_headers(client)
    pid = "PRJ-INTEROP-XER-SRC"
    _create_roundtrip_source_project(client, headers, pid)

    export_resp = client.get(f"{PROJECTS_URL}/{pid}/export.xer", headers=headers)
    assert export_resp.status_code == 200, export_resp.text
    assert "text/plain" in export_resp.headers.get("content-type", "")
    xer_text = export_resp.text
    assert xer_text.strip() != ""

    import_resp = client.post(
        IMPORT_URL,
        headers=headers,
        files={"file": (f"{pid}.xer", xer_text.encode("utf-8"), "text/plain")},
        data={"format": "xer"},
    )
    assert import_resp.status_code == 201, import_resp.text
    body = import_resp.json()

    report = body["report"]
    assert report["format"] == "xer"
    assert report["tasks"] == 3
    assert report["wbs"] == 2
    assert report["links"] == 2
    assert report["constraints"] == 1

    new_pid = body["project"]["project_id"]
    assert new_pid != pid

    got = client.get(f"{PROJECTS_URL}/{new_pid}", headers=headers)
    assert got.status_code == 200, got.text
    _assert_roundtrip_survived(got.json())


# =============================================================================
# 多專案 XER / 日曆 clndr_id 解析（adversarial regression）
# =============================================================================
def test_parse_xer_multi_project_file_filters_to_first_project_with_warning():
    """多專案 XER：僅匯入第一個 PROJECT；其他專案的資料列略過並記警告。"""
    project_tbl = _xer_table(
        "PROJECT",
        ["proj_id", "proj_short_name", "plan_start_date"],
        [
            ["1", "MAIN", "2026-07-06 00:00"],
            ["2", "OTHER", "2026-08-03 00:00"],
        ],
    )
    task_tbl = _xer_table(
        "TASK",
        ["task_id", "proj_id", "task_code", "task_name", "status_code", "target_drtn_hr_cnt"],
        [
            ["1001", "1", "T-01", "主專案任務", "TK_NotStart", "8"],
            ["9001", "2", "X-99", "他專案任務", "TK_NotStart", "8"],
        ],
    )
    pred_tbl = _xer_table(
        "TASKPRED",
        ["task_pred_id", "task_id", "pred_task_id", "proj_id", "pred_type", "lag_hr_cnt"],
        [["1", "9001", "1001", "2", "PR_FS", "0"]],
    )
    interop = parse_xer(_xer_doc(project_tbl, task_tbl, pred_tbl))

    assert interop.name == "MAIN"
    assert interop.start_date == date(2026, 7, 6)
    # 他專案的任務與相依不得被靜默合併。
    assert {t.task_id for t in interop.tasks} == {"T-01"}
    assert not interop.tasks[0].links
    # 多專案 + 略過的資料列皆有警告（非靜默）。
    assert any("多個專案" in w for w in interop.warnings)
    assert any("其他專案" in w and "已略過" in w for w in interop.warnings)


def test_parse_xer_calendar_resolved_by_project_clndr_id_not_first_row():
    """多日曆 XER：以 PROJECT.clndr_id 對應的日曆為準，而非檔案第一列。"""
    project_tbl = _xer_table(
        "PROJECT",
        ["proj_id", "proj_short_name", "plan_start_date", "clndr_id"],
        [["1", "CAL-LINK", "2026-07-06 00:00", "7"]],
    )
    calendar_tbl = _xer_table(
        "CALENDAR",
        ["clndr_id", "clndr_name", "day_hr_cnt"],
        [
            ["5", "Global10h", "10"],  # 檔案第一列 —— 不是專案引用的日曆。
            ["7", "Project4h", "4"],
        ],
    )
    task_tbl = _xer_table(
        "TASK",
        ["task_id", "proj_id", "task_code", "task_name", "status_code", "target_drtn_hr_cnt"],
        [["1001", "1", "T-01", "任務", "TK_NotStart", "8"]],
    )
    interop = parse_xer(_xer_doc(project_tbl, calendar_tbl, task_tbl))

    assert interop.hours_per_day == 4.0  # clndr_id=7 的日曆，非第一列的 10。
    assert interop.tasks[0].duration_days == 2  # 8hr / 4hr = 2 天
    assert interop.warnings == []


def test_parse_xer_calendar_clndr_id_unmatched_falls_back_with_warning():
    project_tbl = _xer_table(
        "PROJECT",
        ["proj_id", "proj_short_name", "plan_start_date", "clndr_id"],
        [["1", "CAL-MISS", "2026-07-06 00:00", "99"]],
    )
    calendar_tbl = _xer_table(
        "CALENDAR",
        ["clndr_id", "clndr_name", "day_hr_cnt"],
        [["5", "Global10h", "10"]],
    )
    interop = parse_xer(_xer_doc(project_tbl, calendar_tbl))

    assert interop.hours_per_day == 10.0  # 找不到 clndr_id=99 -> 退回第一個可用日曆。
    assert any("clndr_id=99" in w for w in interop.warnings)


def test_generate_xer_emits_proj_id_and_p6_mandatory_columns():
    """P6 相容性：PROJWBS/TASK/TASKPRED 皆帶 proj_id（TASKPRED 另帶
    pred_proj_id）；TASK 帶 clndr_id/task_type/duration_type/complete_pct_type。"""
    interop = InteropProject(
        name="GEN-P6",
        start_date=date(2026, 7, 6),
        wbs=[InteropWbsNode(wbs_code="1", name="土建")],
        tasks=[
            InteropTask(task_id="T-01", task_name="A", duration_days=2, wbs_code="1"),
            InteropTask(
                task_id="T-02",
                task_name="B",
                duration_days=1,
                links=[InteropLink(predecessor_task_id="T-01", dep_type="SS", lag_days=1)],
            ),
        ],
    )
    text = generate_xer(interop)

    # 抽出各表的 %F 欄位清單。
    fields_by_table: dict[str, list[str]] = {}
    current = ""
    for line in text.splitlines():
        if line.startswith("%T\t"):
            current = line.split("\t")[1]
        elif line.startswith("%F\t") and current:
            fields_by_table[current] = line.split("\t")[1:]

    assert "clndr_id" in fields_by_table["PROJECT"]
    assert "proj_id" in fields_by_table["PROJWBS"]
    for col in ("proj_id", "clndr_id", "task_type", "duration_type", "complete_pct_type"):
        assert col in fields_by_table["TASK"], col
    for col in ("proj_id", "pred_proj_id"):
        assert col in fields_by_table["TASKPRED"], col

    # 產出的檔案自身可被 parse_xer 正確讀回（proj_id/clndr_id 連動一致）。
    reparsed = parse_xer(text)
    assert {t.task_id for t in reparsed.tasks} == {"T-01", "T-02"}
    assert reparsed.warnings == []


# =============================================================================
# MSPDI：P1D 日分量工期 / 自訂 WBS 代碼遮罩階層（adversarial regression）
# =============================================================================
def test_parse_mspdi_duration_day_form_supported():
    xml_text = f"""<?xml version="1.0" encoding="UTF-8"?>
<Project xmlns="{MSPDI_NS}">
  <Name>P1D-TEST</Name>
  <StartDate>2026-07-06T08:00:00</StartDate>
  <Tasks>
    <Task>
      <UID>1</UID>
      <Name>整日任務</Name>
      <OutlineLevel>1</OutlineLevel>
      <OutlineNumber>1</OutlineNumber>
      <Summary>0</Summary>
      <Duration>P1D</Duration>
    </Task>
    <Task>
      <UID>2</UID>
      <Name>日加時任務</Name>
      <OutlineLevel>1</OutlineLevel>
      <OutlineNumber>2</OutlineNumber>
      <Summary>0</Summary>
      <Duration>P1DT8H0M0S</Duration>
    </Task>
  </Tasks>
</Project>"""
    interop = parse_mspdi(xml_text)
    tasks_by_id = {t.task_id: t for t in interop.tasks}
    assert tasks_by_id["1"].duration_days == 1  # P1D = 8hr = 1 天
    assert tasks_by_id["2"].duration_days == 2  # 1 天 + 8hr = 16hr = 2 天
    assert interop.warnings == []


def test_parse_mspdi_custom_wbs_mask_hierarchy_from_outline_number():
    """自訂 WBS 代碼遮罩（非 "." 分段，如 "A"/"A-1"）：階層由 OutlineNumber
    推導，WBS 文字僅作為顯示代碼 —— 樹狀結構不得被壓扁。"""
    xml_text = f"""<?xml version="1.0" encoding="UTF-8"?>
<Project xmlns="{MSPDI_NS}">
  <Name>CUSTOM-WBS</Name>
  <StartDate>2026-07-06T08:00:00</StartDate>
  <Tasks>
    <Task>
      <UID>1</UID>
      <Name>主體工程</Name>
      <OutlineLevel>1</OutlineLevel>
      <OutlineNumber>1</OutlineNumber>
      <WBS>A</WBS>
      <Summary>1</Summary>
    </Task>
    <Task>
      <UID>2</UID>
      <Name>基礎分項</Name>
      <OutlineLevel>2</OutlineLevel>
      <OutlineNumber>1.1</OutlineNumber>
      <WBS>A-1</WBS>
      <Summary>1</Summary>
    </Task>
    <Task>
      <UID>3</UID>
      <Name>開挖</Name>
      <OutlineLevel>3</OutlineLevel>
      <OutlineNumber>1.1.1</OutlineNumber>
      <WBS>A-1-X</WBS>
      <Summary>0</Summary>
      <Duration>PT8H0M0S</Duration>
    </Task>
  </Tasks>
</Project>"""
    interop = parse_mspdi(xml_text)

    wbs_by_code = {_get(w, "wbs_code"): w for w in interop.wbs}
    assert set(wbs_by_code) == {"A", "A-1"}
    assert _get(wbs_by_code["A"], "parent_code") is None
    assert _get(wbs_by_code["A-1"], "parent_code") == "A"  # 舊實作會誤判為 None（壓扁）。

    assert len(interop.tasks) == 1
    assert interop.tasks[0].wbs_code == "A-1"  # 由 OutlineNumber 父段反查顯示代碼。


# =============================================================================
# 端點：超長欄位截斷（truncate + warn, not 500）/ 匯出採用專案實際行事曆
# =============================================================================
def test_import_truncates_overlong_fields_with_warning(client):
    headers = _editor_headers(client)
    long_code = "T-" + "X" * 150  # > 100 -> 截斷至 100
    long_name = "任務" + "名" * 300  # > 255 -> 截斷至 255
    project_tbl = _xer_table(
        "PROJECT",
        ["proj_id", "proj_short_name", "plan_start_date"],
        [["1", "TRUNC-TEST", "2026-07-06 00:00"]],
    )
    task_tbl = _xer_table(
        "TASK",
        ["task_id", "proj_id", "task_code", "task_name", "status_code", "target_drtn_hr_cnt"],
        [["1001", "1", long_code, long_name, "TK_NotStart", "8"]],
    )
    text = _xer_doc(project_tbl, task_tbl)

    resp = client.post(
        IMPORT_URL,
        headers=headers,
        files={"file": ("trunc.xer", text.encode("utf-8"), "text/plain")},
        data={"format": "xer"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()

    tasks = body["project"]["tasks"]
    assert len(tasks) == 1
    assert len(tasks[0]["task_id"]) == 100
    assert tasks[0]["task_id"] == long_code[:100]
    assert len(tasks[0]["task_name"]) == 255
    assert any("截斷" in w for w in body["report"]["warnings"])


def _xer_task_row(xer_text: str, task_code: str) -> dict[str, str]:
    """自 XER 文字中抽出指定 task_code 的 TASK 資料列（欄名 -> 值）。"""
    current = ""
    fields: list[str] = []
    for line in xer_text.splitlines():
        if line.startswith("%T\t"):
            current = line.split("\t")[1]
        elif line.startswith("%F\t") and current == "TASK":
            fields = line.split("\t")[1:]
        elif line.startswith("%R\t") and current == "TASK":
            row = dict(zip(fields, line.split("\t")[1:]))
            if row.get("task_code") == task_code:
                return row
    raise AssertionError(f"TASK row not found for task_code={task_code!r}")


def test_export_uses_real_project_calendar_and_holidays(client):
    """匯出日期須採專案「實際」行事曆（work_days + 例外假日），而非硬編碼
    的 5 日曆：start 2026-07-06（週一）、work_days 1111110（含週六）、
    2026-07-07（週二）為例外假日 -> constraint_day 4 對應 2026-07-11（週六）。
    舊實作（硬編碼 1111100、無假日）會錯誤地輸出 2026-07-10（週五）。"""
    headers = _admin_headers(client)
    pid = "PRJ-INTEROP-CAL"
    create_resp = client.post(
        PROJECTS_URL,
        headers=headers,
        json={
            "project_id": pid,
            "project_name": "行事曆匯出測試",
            "region": "TW",
            "start_date": "2026-07-06",
            "work_days": "1111110",
            "schedule_data": [
                {
                    "task_id": "T-01", "task_name": "受限任務", "duration": 2,
                    "predecessors": [], "status": "PENDING",
                    "constraint_type": "SNET", "constraint_day": 4,
                },
            ],
        },
    )
    assert create_resp.status_code == 201, create_resp.text
    put_hol = client.put(
        f"{PROJECTS_URL}/{pid}/holidays",
        headers=headers,
        json=[{"holiday_date": "2026-07-07", "name": "颱風假"}],
    )
    assert put_hol.status_code == 200, put_hol.text

    # 偏移 4 於（1111110 + 假日 7/7）：7/6=0, 7/8=1, 7/9=2, 7/10=3, 7/11=4。
    xer_resp = client.get(f"{PROJECTS_URL}/{pid}/export.xer", headers=headers)
    assert xer_resp.status_code == 200, xer_resp.text
    task_row = _xer_task_row(xer_resp.text, "T-01")
    assert task_row["cstr_type"] == "CS_MSOA"  # SNET
    # 實際行事曆 -> 2026-07-11（週六）；硬編碼 5 日曆會錯給 2026-07-10（週五）。
    assert task_row["cstr_date"] == "2026-07-11"

    mspdi_resp = client.get(f"{PROJECTS_URL}/{pid}/export.mspdi.xml", headers=headers)
    assert mspdi_resp.status_code == 200, mspdi_resp.text
    assert "<ConstraintDate>2026-07-11T08:00:00</ConstraintDate>" in mspdi_resp.text
    assert "<Start>2026-07-11T08:00:00</Start>" in mspdi_resp.text  # es=4 同樣受行事曆影響。
    assert "<ConstraintDate>2026-07-10T08:00:00</ConstraintDate>" not in mspdi_resp.text


def test_export_mspdi_then_reimport_preserves_tasks_links_constraints(client):
    headers = _admin_headers(client)
    pid = "PRJ-INTEROP-MSPDI-SRC"
    _create_roundtrip_source_project(client, headers, pid)

    export_resp = client.get(f"{PROJECTS_URL}/{pid}/export.mspdi.xml", headers=headers)
    assert export_resp.status_code == 200, export_resp.text
    assert "xml" in export_resp.headers.get("content-type", "")
    xml_text = export_resp.text
    assert xml_text.strip() != ""

    import_resp = client.post(
        IMPORT_URL,
        headers=headers,
        files={"file": (f"{pid}.mspdi.xml", xml_text.encode("utf-8"), "application/xml")},
        data={"format": "mspdi"},
    )
    assert import_resp.status_code == 201, import_resp.text
    body = import_resp.json()

    report = body["report"]
    assert report["format"] == "mspdi"
    assert report["tasks"] == 3
    assert report["wbs"] == 2
    assert report["links"] == 2
    assert report["constraints"] == 1

    new_pid = body["project"]["project_id"]
    assert new_pid != pid

    got = client.get(f"{PROJECTS_URL}/{new_pid}", headers=headers)
    assert got.status_code == 200, got.text
    _assert_roundtrip_survived(got.json())
