"""FastAPI 應用程式入口（企業級工程排程與自動化 SaaS / CPM）。

職責：
  - 建立 FastAPI app，掛載 CORS
  - 在 settings.api_v1_prefix 之下註冊所有路由器
  - 提供 /health 健康檢查
  - 啟動時 best-effort ping Redis（失敗不阻斷啟動）

多租戶：所有 /api/v1 端點皆需 X-Tenant-Id 標頭，並透過 RLS 隔離資料。
雙區域：X-Region（TW / CN）影響在地化與通知通道（LINE / 釘釘）。
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import is_sqlite, settings
from app.routers import (
    schedule_router,
    projects_router,
    tasks_router,
    erp_router,
)
from app.routers.auth import router as auth_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("cpm.main")


# --------------------------------------------------------------------------- #
# 啟動種子 (seeding) —— 全程 best-effort，任何失敗只記錄、絕不中斷啟動。
# --------------------------------------------------------------------------- #
# Pro Batch D (FEATURE D1) —— 種子專案資源費率示範值 (單一事實來源)：
#   {(project_id, resource_type): (unit_cost, category)}
# 用途：
#   1. _seed_core_data 建立「新」project_resource_limits 列時查表填入。
#   2. _ensure_batch6_columns 於既有 sqlite dev DB「剛以 ALTER 新增
#      unit_cost/category 欄位的當次」一次性回填 —— 該時點所有列必為 ALTER
#      的預設值 (0 / 'labor')，不可能是使用者編輯過的值，故回填安全；
#      之後的啟動不再觸碰既有列 (使用者將 unit_cost 改為 0 亦不會被覆寫)。
_SEED_RESOURCE_RATES: dict[tuple[str, str], tuple[float, str]] = {
    ("PRJ-2026-TW-001", "crane"): (3000.0, "equipment"),
    ("PRJ-2026-TW-001", "manpower"): (250.0, "labor"),
    ("PRJ-2026-CN-001", "crane"): (2800.0, "equipment"),
    ("PRJ-2026-CN-001", "manpower"): (220.0, "labor"),
    ("PRJ-2026-TW-PARALLEL", "crane"): (3200.0, "equipment"),
    ("PRJ-2026-TW-PARALLEL", "manpower"): (260.0, "labor"),
}

# Pro Batch E (FEATURE E1) —— 租戶層級 (enterprise) 資源池示範資料 (單一事實來源)：
#   {tenant_id: [(resource_type, name, category, capacity, unit_cost, work_days), ...]}
# 用途：_seed_core_data 冪等寫入 tenant_resources (供投資組合資源分配示範)。
_SEED_TENANT_RESOURCES: dict[str, list[tuple[str, str, str, int, float, str]]] = {
    "TENT-9981": [
        ("crane", "吊車", "equipment", 2, 3200.0, "1111100"),
        ("manpower", "人力", "labor", 40, 260.0, "1111110"),
        ("concrete_pump", "混凝土泵浦車", "equipment", 1, 5000.0, "1111100"),
    ],
    "TENT-CN-002": [
        ("crane", "塔吊", "equipment", 2, 2800.0, "1111100"),
        ("manpower", "人力", "labor", 30, 220.0, "1111110"),
    ],
}


async def _seed_core_data() -> None:
    """冪等寫入核心示範資料 (僅在 sqlite / dev_bootstrap 模式呼叫)。

    PostgreSQL 正式環境的核心資料由 db/init.sql 權威建立，不走此路徑。
    內容：兩個租戶 (TENT-9981/TW、TENT-CN-002/CN)、兩個專案及其任務/相依、
    ERP 設定列。皆「不存在才插入」(以主鍵 / 唯一鍵查詢) 故可重入。
    """
    from datetime import date

    from sqlalchemy import select

    from app import database
    from app.models.orm import (
        ErpConfig,
        Project,
        ProjectBaseline,
        ProjectResourceLimit,
        ResourceCalendar,
        ResourceCalendarHoliday,
        Task,
        TaskDependency,
        TaskProgress,
        TaskRiskParameter,
        TenantResource,
        Tenant,
    )

    SessionLocal = database.SessionLocal

    # (tenant_id, name, region)
    tenants = [
        ("TENT-9981", "示範營造工程顧問", "TW"),
        ("TENT-CN-002", "示范建筑工程公司", "CN"),
    ]
    # project_id -> (tenant_id, project_name, region, tasks, deps, limits, risk)
    #   tasks  : (task_id, task_name, duration, status, es, ef, ls, lf, float, critical, demands)
    #            demands: dict[str,int] | None  (每任務資源需求 e.g. {"crane":1,"manpower":15})
    #   deps   : (task_id, predecessor_task_id)
    #   limits : (resource_type, max_capacity)  專案資源上限
    #            (Pro Batch D FEATURE D1：unit_cost/category 示範值由模組層級
    #             _SEED_RESOURCE_RATES 查表提供，僅於「新建列」時填入)
    #   calendars : (resource_type, work_days)  資源專屬工作日曆 (Pro Batch D FEATURE D3；選填)
    #   risk   : (task_id, optimistic, most_likely, pessimistic)  PERT 三點估計
    #   progress : (task_id, budget, percent_complete, actual_cost,
    #              actual_start_day|None, actual_finish_day|None)  Phase 9 進度/EVM
    #   baseline : {"project_duration": int,
    #              "tasks": [{task_id, es, ef, duration, budget}, ...]}  Phase 9 基準線快照
    projects = {
        "PRJ-2026-TW-001": {
            "tenant_id": "TENT-9981",
            "project_name": "2026 示範建案工程排程",
            "region": "TW",
            "start_date": date(2026, 3, 2),
            "tasks": [
                ("T-01", "基地開挖", 5, "COMPLETED", 0, 5, 0, 5, 0, True,
                    {"crane": 1, "manpower": 10}),
                ("T-02", "一樓鋼筋綁紮", 3, "IN_PROGRESS", 5, 8, 5, 8, 0, True,
                    {"crane": 2, "manpower": 15}),
                ("T-03", "一樓混凝土澆置", 2, "PENDING", 8, 10, 8, 10, 0, True,
                    {"crane": 2, "manpower": 8}),
            ],
            "deps": [("T-02", "T-01"), ("T-03", "T-02")],
            "limits": [
                ("crane", 2),
                ("manpower", 20),
            ],
            "calendars": [
                ("crane", "1111100"),
                ("manpower", "1111110"),
            ],
            "risk": [
                ("T-01", 3, 5, 9),
                ("T-02", 2, 3, 7),
                ("T-03", 1, 2, 5),
            ],
            # Phase 9 — 刻意造出「落後 + 超支」的示範 (BAC=100000)。
            # 於 data_date=8：SPI<1 (落後)、CPI<1 (超支)。
            "progress": [
                ("T-01", 50000, 100, 55000, 0, 6),
                ("T-02", 30000, 40, 20000, 6, None),
                ("T-03", 20000, 0, 0, None, None),
            ],
            "baseline": {
                "project_duration": 10,
                "tasks": [
                    {"task_id": "T-01", "es": 0, "ef": 5, "duration": 5, "budget": 50000},
                    {"task_id": "T-02", "es": 5, "ef": 8, "duration": 3, "budget": 30000},
                    {"task_id": "T-03", "es": 8, "ef": 10, "duration": 2, "budget": 20000},
                ],
            },
        },
        "PRJ-2026-CN-001": {
            "tenant_id": "TENT-CN-002",
            "project_name": "2026 示范建筑工程排程",
            "region": "CN",
            "start_date": date(2026, 3, 2),
            "tasks": [
                ("C-01", "土方开挖", 4, "COMPLETED", 0, 4, 0, 4, 0, True,
                    {"crane": 1, "manpower": 12}),
                ("C-02", "基础施工", 6, "IN_PROGRESS", 4, 10, 4, 10, 0, True,
                    {"crane": 2, "manpower": 18}),
            ],
            "deps": [("C-02", "C-01")],
            "limits": [
                ("crane", 2),
                ("manpower", 20),
            ],
            "risk": [
                ("C-01", 2, 4, 8),
                ("C-02", 4, 6, 11),
            ],
            # Phase 9 — 健康示範值。
            "progress": [
                ("C-01", 40000, 100, 38000, 0, 4),
                ("C-02", 60000, 50, 30000, 4, None),
            ],
            "baseline": {
                "project_duration": 10,
                "tasks": [
                    {"task_id": "C-01", "es": 0, "ef": 4, "duration": 4, "budget": 40000},
                    {"task_id": "C-02", "es": 4, "ef": 10, "duration": 6, "budget": 60000},
                ],
            },
        },
        # 雙塔平行工程示範 (資源衝突)：A/B 兩棟於 PA0 整備後平行施工。
        # PA1 與 PB1 同時各需吊車 1 部，但專案吊車上限僅 1 部 => 必然衝突。
        # A 支 (PA1→PA2) 為要徑、B 支 (PB1→PB2) 較短而有正時差，撫平啟發法會
        # 把可移動的 B 支推遲、保護要徑 A 支。es/ef/ls/lf/float/critical 之值
        # 係以 calculate_cpm 預先算得 (專案總工期 = 12 天)。
        "PRJ-2026-TW-PARALLEL": {
            "tenant_id": "TENT-9981",
            "project_name": "雙塔平行工程示範 (資源衝突)",
            "region": "TW",
            "start_date": date(2026, 3, 2),
            "tasks": [
                ("PA0", "場地整備", 2, "COMPLETED", 0, 2, 0, 2, 0, True,
                    {"crane": 0, "manpower": 5}),
                ("PA1", "A棟基礎", 4, "IN_PROGRESS", 2, 6, 2, 6, 0, True,
                    {"crane": 1, "manpower": 10}),
                ("PB1", "B棟基礎", 4, "PENDING", 2, 6, 5, 9, 3, False,
                    {"crane": 1, "manpower": 10}),
                ("PA2", "A棟結構", 5, "PENDING", 6, 11, 6, 11, 0, True,
                    {"crane": 1, "manpower": 12}),
                ("PB2", "B棟結構", 2, "PENDING", 6, 8, 9, 11, 3, False,
                    {"crane": 1, "manpower": 8}),
                ("PF", "竣工驗收", 1, "PENDING", 11, 12, 11, 12, 0, True,
                    {"crane": 0, "manpower": 4}),
            ],
            "deps": [
                ("PA1", "PA0"),
                ("PB1", "PA0"),
                ("PA2", "PA1"),
                ("PB2", "PB1"),
                ("PF", "PA2"),
                ("PF", "PB2"),
            ],
            "limits": [
                ("crane", 1),
                ("manpower", 20),
            ],
            "risk": [
                ("PA0", 1, 2, 4),
                ("PA1", 3, 4, 8),
                ("PB1", 2, 4, 7),
                ("PA2", 4, 5, 10),
                ("PB2", 1, 2, 5),
                ("PF", 1, 1, 2),
            ],
            # Phase 9 — 健康示範值 (BAC=170000)。
            "progress": [
                ("PA0", 10000, 100, 10000, 0, 2),
                ("PA1", 40000, 100, 39000, 2, 6),
                ("PB1", 35000, 75, 26000, 2, None),
                ("PA2", 50000, 20, 11000, 6, None),
                ("PB2", 20000, 0, 0, None, None),
                ("PF", 15000, 0, 0, None, None),
            ],
            "baseline": {
                "project_duration": 12,
                "tasks": [
                    {"task_id": "PA0", "es": 0, "ef": 2, "duration": 2, "budget": 10000},
                    {"task_id": "PA1", "es": 2, "ef": 6, "duration": 4, "budget": 40000},
                    {"task_id": "PB1", "es": 2, "ef": 6, "duration": 4, "budget": 35000},
                    {"task_id": "PA2", "es": 6, "ef": 11, "duration": 5, "budget": 50000},
                    {"task_id": "PB2", "es": 6, "ef": 8, "duration": 2, "budget": 20000},
                    {"task_id": "PF", "es": 11, "ef": 12, "duration": 1, "budget": 15000},
                ],
            },
        },
    }
    # (tenant_id, erp_type, api_endpoint)
    erp_configs = [
        ("TENT-9981", "DINGXIN_TW", ""),
        ("TENT-CN-002", "YONYOU_CN", ""),
    ]

    async with SessionLocal() as session:
        async with session.begin():
            for tenant_id, name, region in tenants:
                exists = await session.get(Tenant, tenant_id)
                if exists is None:
                    session.add(Tenant(tenant_id=tenant_id, name=name, region=region))

            for project_id, p in projects.items():
                exists = await session.get(Project, project_id)
                if exists is None:
                    session.add(
                        Project(
                            project_id=project_id,
                            tenant_id=p["tenant_id"],
                            project_name=p["project_name"],
                            region=p["region"],
                            start_date=p.get("start_date"),
                        )
                    )
                elif exists.start_date is None and p.get("start_date") is not None:
                    # 冪等回填：既有 demo 專案 (前一版種子無 start_date) 補上開工日，
                    # 讓企業資源配置剖析 / 資源行事曆一開箱即可展示 (不覆寫既有值)。
                    exists.start_date = p["start_date"]
                for (
                    task_id,
                    task_name,
                    duration,
                    st,
                    es,
                    ef,
                    ls,
                    lf,
                    ft,
                    crit,
                    demands,
                ) in p["tasks"]:
                    found = await session.execute(
                        select(Task).where(
                            Task.project_id == project_id, Task.task_id == task_id
                        )
                    )
                    existing = found.scalar_one_or_none()
                    if existing is None:
                        session.add(
                            Task(
                                project_id=project_id,
                                tenant_id=p["tenant_id"],
                                task_id=task_id,
                                task_name=task_name,
                                duration=duration,
                                status=st,
                                es=es,
                                ef=ef,
                                ls=ls,
                                lf=lf,
                                float_time=ft,
                                is_critical=crit,
                                resource_demands=demands,
                            )
                        )
                    elif existing.resource_demands is None and demands is not None:
                        # 既有資料 (前一版種子無 resource_demands) 冪等回填示範需求。
                        existing.resource_demands = demands
                for task_id, pred in p["deps"]:
                    found = await session.execute(
                        select(TaskDependency).where(
                            TaskDependency.project_id == project_id,
                            TaskDependency.task_id == task_id,
                            TaskDependency.predecessor_task_id == pred,
                        )
                    )
                    if found.scalar_one_or_none() is None:
                        session.add(
                            TaskDependency(
                                project_id=project_id,
                                tenant_id=p["tenant_id"],
                                task_id=task_id,
                                predecessor_task_id=pred,
                            )
                        )

                # Phase 8 — 專案資源上限 (冪等：project_id + resource_type 唯一)
                # Pro Batch D (FEATURE D1)：僅「新建列」帶入示範 unit_cost/
                # category (_SEED_RESOURCE_RATES 查表)。既有列一律不動 ——
                # 既有 dev DB 的一次性回填由 _ensure_batch6_columns 於 ALTER
                # 新增欄位的當次完成，避免以「值是否為 0」猜測而覆寫使用者
                # 刻意設定的 unit_cost=0 (backward-compat：既有列保持不變)。
                for resource_type, max_capacity in p.get("limits", []):
                    found = await session.execute(
                        select(ProjectResourceLimit).where(
                            ProjectResourceLimit.project_id == project_id,
                            ProjectResourceLimit.resource_type == resource_type,
                        )
                    )
                    if found.scalar_one_or_none() is None:
                        unit_cost, category = _SEED_RESOURCE_RATES.get(
                            (project_id, resource_type), (0.0, "labor")
                        )
                        session.add(
                            ProjectResourceLimit(
                                project_id=project_id,
                                tenant_id=p["tenant_id"],
                                resource_type=resource_type,
                                max_capacity=max_capacity,
                                unit_cost=unit_cost,
                                category=category,
                            )
                        )

                # Pro Batch D (FEATURE D3) — 資源專屬工作日曆 (冪等：
                # project_id + resource_type 唯一)。
                for resource_type, work_days in p.get("calendars", []):
                    found = await session.execute(
                        select(ResourceCalendar).where(
                            ResourceCalendar.project_id == project_id,
                            ResourceCalendar.resource_type == resource_type,
                        )
                    )
                    if found.scalar_one_or_none() is None:
                        session.add(
                            ResourceCalendar(
                                project_id=project_id,
                                tenant_id=p["tenant_id"],
                                resource_type=resource_type,
                                work_days=work_days,
                            )
                        )

                # Phase 8 — 任務風險參數 / PERT 三點估計 (冪等：project_id + task_id 唯一)
                for task_id, a, m, b in p.get("risk", []):
                    found = await session.execute(
                        select(TaskRiskParameter).where(
                            TaskRiskParameter.project_id == project_id,
                            TaskRiskParameter.task_id == task_id,
                        )
                    )
                    if found.scalar_one_or_none() is None:
                        session.add(
                            TaskRiskParameter(
                                project_id=project_id,
                                tenant_id=p["tenant_id"],
                                task_id=task_id,
                                optimistic_duration=a,
                                most_likely_duration=m,
                                pessimistic_duration=b,
                            )
                        )

                # Phase 9 — 任務進度 / EVM 預算 (冪等：project_id + task_id 唯一)
                for (
                    task_id,
                    budget,
                    percent_complete,
                    actual_cost,
                    actual_start_day,
                    actual_finish_day,
                ) in p.get("progress", []):
                    found = await session.execute(
                        select(TaskProgress).where(
                            TaskProgress.project_id == project_id,
                            TaskProgress.task_id == task_id,
                        )
                    )
                    if found.scalar_one_or_none() is None:
                        session.add(
                            TaskProgress(
                                project_id=project_id,
                                tenant_id=p["tenant_id"],
                                task_id=task_id,
                                budget=budget,
                                percent_complete=percent_complete,
                                actual_cost=actual_cost,
                                actual_start_day=actual_start_day,
                                actual_finish_day=actual_finish_day,
                            )
                        )

                # Phase 9 — 專案基準線 (冪等：該專案尚無任何基準線時才插入一條)
                baseline = p.get("baseline")
                if baseline is not None:
                    found = await session.execute(
                        select(ProjectBaseline.id).where(
                            ProjectBaseline.project_id == project_id
                        )
                    )
                    if found.first() is None:
                        session.add(
                            ProjectBaseline(
                                project_id=project_id,
                                tenant_id=p["tenant_id"],
                                name="baseline",
                                snapshot=baseline,
                            )
                        )

            for tenant_id, erp_type, endpoint in erp_configs:
                exists = await session.get(ErpConfig, tenant_id)
                if exists is None:
                    session.add(
                        ErpConfig(
                            tenant_id=tenant_id,
                            erp_type=erp_type,
                            api_endpoint=endpoint,
                            is_active=True,
                        )
                    )

            # Pro Batch E (FEATURE E1) — 租戶層級資源池 (冪等：tenant_id +
            # resource_type 唯一)。
            for tenant_id, resources in _SEED_TENANT_RESOURCES.items():
                for resource_type, name, category, capacity, unit_cost, work_days in resources:
                    found = await session.execute(
                        select(TenantResource).where(
                            TenantResource.tenant_id == tenant_id,
                            TenantResource.resource_type == resource_type,
                        )
                    )
                    if found.scalar_one_or_none() is None:
                        session.add(
                            TenantResource(
                                tenant_id=tenant_id,
                                resource_type=resource_type,
                                name=name,
                                category=category,
                                capacity=capacity,
                                unit_cost=unit_cost,
                                work_days=work_days,
                            )
                        )

            # Pro Batch E (FEATURE E2) — 單一資源專屬例外停工日示範 (冪等：
            # project_id + resource_type + holiday_date 唯一)：吊車保養日示範。
            from datetime import date as _seed_date

            found = await session.execute(
                select(ResourceCalendarHoliday.id).where(
                    ResourceCalendarHoliday.project_id == "PRJ-2026-TW-001",
                    ResourceCalendarHoliday.resource_type == "crane",
                    ResourceCalendarHoliday.holiday_date == _seed_date(2026, 7, 20),
                )
            )
            if found.first() is None:
                session.add(
                    ResourceCalendarHoliday(
                        project_id="PRJ-2026-TW-001",
                        tenant_id="TENT-9981",
                        resource_type="crane",
                        holiday_date=_seed_date(2026, 7, 20),
                        name="吊車保養日",
                    )
                )


async def _seed_app_users() -> None:
    """冪等寫入示範登入帳號 (僅在 sqlite / dev_bootstrap 模式呼叫)。

    以 passlib 雜湊密碼 (pbkdf2_sha256，純 python)，username 不存在才插入。
    安全性修正 FIX-1：正式 Postgres 不再無條件種入 demo1234 帳號 —— 此函式現與
    _seed_core_data 同一閘門 (is_sqlite() 或 dev_bootstrap)；正式環境改以
    INITIAL_ADMIN_* 經 _seed_initial_admin 建立管理員。
    需要 tenants 已存在 (FK)；於 sqlite/dev_bootstrap 由 _seed_core_data 提供。
    """
    from sqlalchemy import select

    from app import database
    from app.core.security import hash_password
    from app.models.orm import AppUser

    SessionLocal = database.SessionLocal

    # (username, password, tenant_id, region, role)
    #   admin@tw / admin@cn  -> admin (完整權限；既有 demo 帳號)
    #   editor@tw            -> editor (可寫；Feature 2 新增 demo 帳號)
    #   viewer@tw            -> viewer (僅讀；Feature 2 新增 demo 帳號)
    users = [
        ("admin@tw", "demo1234", "TENT-9981", "TW", "admin"),
        ("admin@cn", "demo1234", "TENT-CN-002", "CN", "admin"),
        ("editor@tw", "demo1234", "TENT-9981", "TW", "editor"),
        ("viewer@tw", "demo1234", "TENT-9981", "TW", "viewer"),
    ]
    async with SessionLocal() as session:
        async with session.begin():
            for username, password, tenant_id, region, role in users:
                found = await session.execute(
                    select(AppUser).where(AppUser.username == username)
                )
                existing = found.scalar_one_or_none()
                if existing is None:
                    session.add(
                        AppUser(
                            tenant_id=tenant_id,
                            username=username,
                            password_hash=hash_password(password),
                            region=region,
                            role=role,
                            is_active=True,
                        )
                    )
                elif not getattr(existing, "role", None):
                    # 既有帳號 (前一版種子無 role，或 ALTER 後欄位為 NULL/空) 冪等補齊。
                    # 不改動既有密碼 (保留已輪替之 demo 密碼)。
                    existing.role = role


async def _seed_initial_admin() -> None:
    """於「所有模式」啟動時冪等建立初始管理員 (供正式環境首次啟動)。

    當 settings.initial_admin_username 與 initial_admin_password 皆有值，且該
    username 尚不存在時：
      1) 確保對應租戶 (initial_admin_tenant) 列存在；於 Postgres 插入租戶 / 帳號
         前先於「同一交易」內 set_config('app.current_tenant', tenant, true)，
         使 RLS WITH CHECK 通過 (sqlite 為 no-op)。
      2) 插入一個 role=admin 的 AppUser (密碼以 hash_password 雜湊)。

    冪等：已存在則略過。全程 best-effort：絕不因任何錯誤中斷啟動 (try/except + log)。
    """
    username = (settings.initial_admin_username or "").strip()
    password = settings.initial_admin_password or ""
    if not username or not password:
        return

    from sqlalchemy import select

    from app import database
    from app.core.security import hash_password
    from app.models.orm import AppUser, Tenant

    SessionLocal = database.SessionLocal
    tenant_id = (settings.initial_admin_tenant or "TENT-9981").strip() or "TENT-9981"

    async with SessionLocal() as session:
        async with session.begin():
            # 先設 RLS GUC，後續 tenants / app_users 之 INSERT 才能通過 WITH CHECK。
            # (app_users 本身無 RLS，但 tenants 於 Postgres 受 RLS 規範)；sqlite no-op。
            await database.set_tenant_guc(session, tenant_id)

            found = await session.execute(
                select(AppUser).where(AppUser.username == username)
            )
            if found.scalar_one_or_none() is not None:
                logger.info("Initial admin already present; skipping (%s).", username)
                return

            existing_tenant = await session.get(Tenant, tenant_id)
            if existing_tenant is None:
                session.add(
                    Tenant(
                        tenant_id=tenant_id,
                        name="初始管理租戶 (initial admin tenant)",
                        region=settings.default_region or "TW",
                    )
                )

            session.add(
                AppUser(
                    tenant_id=tenant_id,
                    username=username,
                    password_hash=hash_password(password),
                    region=settings.default_region or "TW",
                    role="admin",
                    is_active=True,
                )
            )
    logger.info("Initial admin created (username=%s, tenant=%s).", username, tenant_id)


async def _assert_prod_has_admin() -> None:
    """正式環境安全閘：拒絕啟動一個「無人可登入 / 不安全」的系統。

    當 app_env 屬 production 且 auth_required=True 且 app_users 表為空，且未設定
    任何初始管理員 (INITIAL_ADMIN_USERNAME/PASSWORD) 時 -> raise RuntimeError，
    避免在正式環境啟動一個沒有任何帳號可登入 (且已關閉 demo 種子) 的系統。
    """
    if settings.app_env.lower() not in {"production", "prod"}:
        return
    if not settings.auth_required:
        return

    initial_admin_configured = bool(
        (settings.initial_admin_username or "").strip()
        and (settings.initial_admin_password or "")
    )
    if initial_admin_configured:
        return

    from sqlalchemy import func, select

    from app import database
    from app.models.orm import AppUser

    SessionLocal = database.SessionLocal
    user_count = 0
    try:
        async with SessionLocal() as session:
            result = await session.execute(select(func.count()).select_from(AppUser))
            user_count = int(result.scalar() or 0)
    except Exception as exc:  # noqa: BLE001 - 查詢失敗不應「誤判為空」而擋啟動
        logger.warning(
            "Could not verify app_users count for prod safety check "
            "(continuing): %s",
            exc,
        )
        return

    if user_count == 0:
        raise RuntimeError(
            "拒絕啟動：正式環境 (APP_ENV=production) 已啟用認證 (AUTH_REQUIRED=true)，"
            "但 app_users 為空且未設定初始管理員。請設定 INITIAL_ADMIN_USERNAME 與 "
            "INITIAL_ADMIN_PASSWORD (並視需要 INITIAL_ADMIN_TENANT) 後重啟。 | "
            "Refusing to boot: production has auth enabled but no app users exist and "
            "no initial admin is configured. Set INITIAL_ADMIN_USERNAME and "
            "INITIAL_ADMIN_PASSWORD (and optionally INITIAL_ADMIN_TENANT)."
        )


async def _ensure_app_users_role_column() -> None:
    """冪等為「既有」app_users 表補上 role 欄位 (供既有 sqlite dev DB 升級)。

    Feature 2 (Roles & Users) 新增 app_users.role。對「全新」DB，create_all /
    init.sql 已含此欄位；但對「既有」cpm_dev.db，create_all 不會修改既存表，
    故此處以 ALTER TABLE ADD COLUMN 補齊 —— 整段 try/except 包覆：

      - 欄位已存在 (新庫 / 已升級) -> ALTER 失敗 (duplicate column) -> 略過。
      - 表尚未建立 -> ALTER 失敗 -> 略過 (隨後 create_all 會建含 role 的新表)。

    僅針對 sqlite (本機 dev) 執行：PostgreSQL 的 schema 由 db/init.sql 權威建立
    且其 CREATE TABLE 已含 role 欄位，無需在此 ALTER。如此既有的 cpm_dev.db
    可「無需重建」升級，且已輪替的 demo 密碼亦得保留。
    """
    if not is_sqlite():
        return
    from sqlalchemy import text

    from app import database

    try:
        async with database.get_engine().begin() as conn:
            await conn.execute(
                text(
                    "ALTER TABLE app_users "
                    "ADD COLUMN role VARCHAR(20) DEFAULT 'admin'"
                )
            )
        logger.info("app_users.role column added (live sqlite upgrade).")
    except Exception as exc:  # noqa: BLE001 - 欄位已存在 / 表未建 皆視為正常
        logger.info("app_users.role ALTER skipped (already present?): %s", exc)


async def _ensure_batch3_columns() -> None:
    """冪等為「既有」sqlite dev DB 補上 Batch 3 新欄位 (live upgrade，免重建)。

    Batch 3 新增：
      projects.start_date / work_days (FEAT-2 真實日期 + 工作日曆)、
      projects.version (FEAT-3 樂觀併發)、
      projects.deleted_at / deleted_by (FEAT-4 軟刪除 / 回收桶)、
      task_dependencies.dep_type / lag_days (FEAT-1 依賴型態 + 延遲)。

    對「全新」DB，create_all / init.sql 已含這些欄位；但對「既有」cpm_dev.db，
    create_all 不會修改既存表 (僅補建新表 project_holidays)，故此處逐欄以
    ALTER TABLE ADD COLUMN 補齊 —— 沿用 _ensure_app_users_role_column 的
    已驗證模式：「每欄各自 try/except + 各自交易」：

      - 欄位已存在 (新庫 / 已升級) -> 該欄 ALTER 失敗 (duplicate column) -> 略過。
      - 表尚未建立 -> ALTER 失敗 -> 略過 (隨後/先前的 create_all 會建出完整新表)。

    僅針對 sqlite (本機 dev) 執行：PostgreSQL 由 Alembic 0002 / db/init.sql 權威
    管理。如此既有的 cpm_dev.db 可「無需重建」升級，已輪替的密碼亦得保留。
    """
    if not is_sqlite():
        return
    from sqlalchemy import text

    from app import database

    # (標籤, DDL) —— 預設值與 init.sql / ORM server_default 一致。
    statements = [
        # FEAT-2 真實日期 + 工作日曆
        ("projects.start_date",
         "ALTER TABLE projects ADD COLUMN start_date DATE"),
        ("projects.work_days",
         "ALTER TABLE projects ADD COLUMN work_days VARCHAR(7) "
         "NOT NULL DEFAULT '1111110'"),
        # FEAT-3 樂觀併發控制
        ("projects.version",
         "ALTER TABLE projects ADD COLUMN version INTEGER NOT NULL DEFAULT 0"),
        # FEAT-4 軟刪除 / 回收桶
        ("projects.deleted_at",
         "ALTER TABLE projects ADD COLUMN deleted_at TIMESTAMP"),
        ("projects.deleted_by",
         "ALTER TABLE projects ADD COLUMN deleted_by VARCHAR(150)"),
        # FEAT-1 依賴型態 + 延遲
        ("task_dependencies.dep_type",
         "ALTER TABLE task_dependencies ADD COLUMN dep_type VARCHAR(2) "
         "NOT NULL DEFAULT 'FS'"),
        ("task_dependencies.lag_days",
         "ALTER TABLE task_dependencies ADD COLUMN lag_days INTEGER "
         "NOT NULL DEFAULT 0"),
    ]
    for label, ddl in statements:
        try:
            async with database.get_engine().begin() as conn:
                await conn.execute(text(ddl))
            logger.info("Batch 3 column added: %s (live sqlite upgrade).", label)
        except Exception as exc:  # noqa: BLE001 - 欄位已存在 / 表未建 皆視為正常
            logger.info("Batch 3 ALTER skipped for %s (already present?): %s",
                        label, exc)


async def _ensure_batch4_columns() -> None:
    """冪等為「既有」sqlite dev DB 補上 Batch 4 (PERF-3) 新欄位 (live upgrade)。

    Batch 4 新增：sync_event_log.project_id (事件所屬專案) + 複合索引
    (tenant_id, sync_type, status)，並以 json_extract 自 payload 回填既有列。

    對「全新」DB，create_all / init.sql 已含欄位與索引；但對「既有」cpm_dev.db，
    create_all 不會修改既存表，故此處以 ALTER TABLE ADD COLUMN 補齊 —— 沿用
    _ensure_batch3_columns 的已驗證模式：「每句各自 try/except + 各自交易」：

      - 欄位已存在 (新庫 / 已升級) -> ALTER 失敗 (duplicate column) -> 略過。
      - 表尚未建立 -> ALTER 失敗 -> 略過 (隨後/先前的 create_all 會建出完整新表)。
      - 回填 / 索引建立皆冪等 (WHERE project_id IS NULL / IF NOT EXISTS)。

    僅針對 sqlite (本機 dev) 執行：PostgreSQL 由 Alembic 0003 / db/init.sql 權威
    管理。sqlite 下 erp_integration schema 映射為 None，表名即 sync_event_log。
    """
    if not is_sqlite():
        return
    from sqlalchemy import text

    from app import database

    statements = [
        ("sync_event_log.project_id (add)",
         "ALTER TABLE sync_event_log ADD COLUMN project_id VARCHAR(64)"),
        # 回填：既有列自 payload JSON 取出 project_id (idempotent)。
        ("sync_event_log.project_id (backfill)",
         "UPDATE sync_event_log "
         "SET project_id = json_extract(payload, '$.project_id') "
         "WHERE project_id IS NULL"),
        # 複合索引：dashboard / exports 風險事件統計查詢 (IF NOT EXISTS 冪等)。
        ("sync_event_log index (tenant_id, sync_type, status)",
         "CREATE INDEX IF NOT EXISTS ix_sync_event_log_tenant_type_status "
         "ON sync_event_log(tenant_id, sync_type, status)"),
    ]
    for label, ddl in statements:
        try:
            async with database.get_engine().begin() as conn:
                await conn.execute(text(ddl))
            logger.info("Batch 4 ensure applied: %s (live sqlite upgrade).", label)
        except Exception as exc:  # noqa: BLE001 - 欄位已存在 / 表未建 皆視為正常
            logger.info("Batch 4 ensure skipped for %s (already present?): %s",
                        label, exc)


async def _ensure_batch5_columns() -> None:
    """冪等為「既有」sqlite dev DB 補上 Batch 5 新欄位 (live upgrade，免重建)。

    Batch 5 新增：
      tasks.wbs_code (FEAT-1 WBS 階層歸屬)、
      tasks.constraint_type / constraint_day / constraint_violated
        (FEAT-2 活動限制 P6-style constraints)、
      project_baselines.is_active (FEAT-3 多組具名基準線)。
    新表 wbs_nodes 由 create_all 補建 (create_all 只補「缺漏的表」，不補既有
    表的欄位，故此處僅需處理既存表的新欄位)。

    對「全新」DB，create_all / init.sql 已含這些欄位；但對「既有」cpm_dev.db，
    create_all 不會修改既存表，故此處逐欄以 ALTER TABLE ADD COLUMN 補齊 ——
    沿用 _ensure_batch3_columns / _ensure_batch4_columns 的已驗證模式：
    「每句各自 try/except + 各自交易」：

      - 欄位已存在 (新庫 / 已升級) -> 該欄 ALTER 失敗 (duplicate column) -> 略過。
      - 表尚未建立 -> ALTER 失敗 -> 略過 (隨後/先前的 create_all 會建出完整新表)。

    僅針對 sqlite (本機 dev) 執行：PostgreSQL 由 Alembic 0004 / db/init.sql 權威
    管理。如此既有的 cpm_dev.db 可「無需重建」升級。
    """
    if not is_sqlite():
        return
    from sqlalchemy import text

    from app import database

    # (標籤, DDL) —— 預設值與 init.sql / ORM server_default 一致。
    statements = [
        # FEAT-1 WBS 階層歸屬
        ("tasks.wbs_code",
         "ALTER TABLE tasks ADD COLUMN wbs_code VARCHAR(60)"),
        # FEAT-2 活動限制 (P6-style constraints)
        ("tasks.constraint_type",
         "ALTER TABLE tasks ADD COLUMN constraint_type VARCHAR(10)"),
        ("tasks.constraint_day",
         "ALTER TABLE tasks ADD COLUMN constraint_day INTEGER"),
        ("tasks.constraint_violated",
         "ALTER TABLE tasks ADD COLUMN constraint_violated BOOLEAN "
         "NOT NULL DEFAULT 0"),
        # FEAT-3 多組具名基準線
        ("project_baselines.is_active",
         "ALTER TABLE project_baselines ADD COLUMN is_active BOOLEAN "
         "NOT NULL DEFAULT 0"),
    ]
    for label, ddl in statements:
        try:
            async with database.get_engine().begin() as conn:
                await conn.execute(text(ddl))
            logger.info("Batch 5 column added: %s (live sqlite upgrade).", label)
        except Exception as exc:  # noqa: BLE001 - 欄位已存在 / 表未建 皆視為正常
            logger.info("Batch 5 ALTER skipped for %s (already present?): %s",
                        label, exc)


async def _ensure_batch6_columns() -> None:
    """冪等為「既有」sqlite dev DB 補上 Pro Batch D 新欄位 (live upgrade，免重建)。

    Pro Batch D (FEATURE D1) 新增：
      project_resource_limits.unit_cost / category (成本負載 cost loading)。
    新表 resource_calendars (FEATURE D3) 由 create_all 補建 (create_all 只補
    「缺漏的表」，不補既有表的欄位，故此處僅需處理既存表的新欄位)。

    對「全新」DB，create_all / init.sql 已含這些欄位；但對「既有」cpm_dev.db，
    create_all 不會修改既存表，故此處逐欄以 ALTER TABLE ADD COLUMN 補齊 ——
    沿用 _ensure_batch3/4/5_columns 的已驗證模式：「每句各自 try/except +
    各自交易」：

      - 欄位已存在 (新庫 / 已升級) -> 該欄 ALTER 失敗 (duplicate column) -> 略過。
      - 表尚未建立 -> ALTER 失敗 -> 略過 (隨後/先前的 create_all 會建出完整新表)。

    僅針對 sqlite (本機 dev) 執行：PostgreSQL 由 Alembic 0006 / db/init.sql 權威
    管理。如此既有的 cpm_dev.db 可「無需重建」升級。

    另於「欄位剛新增的當次」一次性回填種子示範費率 (_SEED_RESOURCE_RATES)；
    之後的啟動不再觸碰既有列 (使用者編輯 —— 含刻意設為 0 —— 永不被覆寫)。
    """
    if not is_sqlite():
        return
    from sqlalchemy import text

    from app import database

    statements = [
        ("project_resource_limits.unit_cost",
         "ALTER TABLE project_resource_limits ADD COLUMN unit_cost REAL "
         "NOT NULL DEFAULT 0"),
        ("project_resource_limits.category",
         "ALTER TABLE project_resource_limits ADD COLUMN category VARCHAR(20) "
         "NOT NULL DEFAULT 'labor'"),
    ]
    columns_newly_added = False
    for label, ddl in statements:
        try:
            async with database.get_engine().begin() as conn:
                await conn.execute(text(ddl))
            logger.info("Batch 6 column added: %s (live sqlite upgrade).", label)
            columns_newly_added = True
        except Exception as exc:  # noqa: BLE001 - 欄位已存在 / 表未建 皆視為正常
            logger.info("Batch 6 ALTER skipped for %s (already present?): %s",
                        label, exc)

    # 一次性回填：僅在「本次啟動剛新增欄位」時，將既有種子示範限制列補上
    # 示範費率/類別。此時點所有列的 unit_cost/category 必為 ALTER 預設值
    # (0 / 'labor')，不可能是使用者編輯過的值 -> 回填安全。之後的啟動
    # (欄位已存在) 絕不觸碰既有列：使用者刻意設定 unit_cost=0 不會被覆寫。
    if columns_newly_added:
        try:
            async with database.get_engine().begin() as conn:
                for (pid, rtype), (unit_cost, category) in (
                    _SEED_RESOURCE_RATES.items()
                ):
                    await conn.execute(
                        text(
                            "UPDATE project_resource_limits "
                            "SET unit_cost = :unit_cost, category = :category "
                            "WHERE project_id = :pid AND resource_type = :rtype"
                        ),
                        {"unit_cost": unit_cost, "category": category,
                         "pid": pid, "rtype": rtype},
                    )
            logger.info("Batch 6 seed rate backfill applied (one-time).")
        except Exception as exc:  # noqa: BLE001 - 回填為 best-effort
            logger.info("Batch 6 seed rate backfill skipped: %s", exc)


async def _bootstrap_database() -> None:
    """啟動時的資料庫初始化 (create_all + 種子)，全程 best-effort。

    - sqlite 或 settings.dev_bootstrap=True：create_all + 種核心資料 + 種 demo 帳號。
    - 其餘 (PostgreSQL 正式環境)：schema/核心資料由 init.sql 權威建立；不種 demo
      帳號 (FIX-1)，改由 INITIAL_ADMIN_* 經 _seed_initial_admin 建立管理員。
    - 所有模式皆於最後嘗試 _seed_initial_admin，並執行正式環境安全閘檢查。
    """
    do_create_and_core = is_sqlite() or settings.dev_bootstrap

    if do_create_and_core:
        try:
            from app.database import create_all

            await create_all()
            logger.info("Database tables created (create_all).")
        except Exception as exc:  # noqa: BLE001 - 不可中斷啟動
            logger.warning("create_all failed (continuing): %s", exc)

        # Batch 3：為既有 sqlite dev DB 冪等補上新欄位 (新庫為 no-op)。
        # 須在 _seed_core_data「之前」執行 —— ORM 的 SELECT 已含新欄位
        # (projects.start_date 等)，未升級的既有庫會使種子查詢失敗。
        try:
            await _ensure_batch3_columns()
        except Exception as exc:  # noqa: BLE001
            logger.info("Batch 3 column ensure skipped: %s", exc)

        # Batch 4 (PERF-3)：為既有 sqlite dev DB 冪等補上 sync_event_log.project_id
        # + 複合索引 + payload 回填 (新庫為 no-op)。
        try:
            await _ensure_batch4_columns()
        except Exception as exc:  # noqa: BLE001
            logger.info("Batch 4 column ensure skipped: %s", exc)

        # Batch 5：為既有 sqlite dev DB 冪等補上 WBS / 活動限制 / 多組基準線
        # 新欄位 (新庫為 no-op)。須在 _seed_core_data「之前」執行 —— 理由同
        # Batch 3 (ORM 的 SELECT 已含新欄位，未升級的既有庫會使種子查詢失敗)。
        try:
            await _ensure_batch5_columns()
        except Exception as exc:  # noqa: BLE001
            logger.info("Batch 5 column ensure skipped: %s", exc)

        # Pro Batch D：為既有 sqlite dev DB 冪等補上成本負載新欄位
        # (project_resource_limits.unit_cost / category)。新表 resource_calendars
        # 由 create_all 補建。須在 _seed_core_data「之前」執行 —— 理由同上。
        try:
            await _ensure_batch6_columns()
        except Exception as exc:  # noqa: BLE001
            logger.info("Batch 6 column ensure skipped: %s", exc)

        try:
            await _seed_core_data()
            logger.info("Core demo data seeded.")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Core data seeding failed (continuing): %s", exc)

    # 為既有 sqlite dev DB 冪等補上 app_users.role 欄位 (新庫為 no-op)。
    # 須在帳號種子「之前」執行，種子才能寫入 / 補齊 role。
    try:
        await _ensure_app_users_role_column()
    except Exception as exc:  # noqa: BLE001
        logger.info("app_users.role ensure skipped: %s", exc)

    # demo 帳號種子：僅在 sqlite / dev_bootstrap 模式執行 (與 _seed_core_data 同一
    # 閘門)。正式 Postgres 不再無條件種入 demo1234 帳號 (安全性修正 FIX-1)；
    # 正式環境改以 INITIAL_ADMIN_* 由 _seed_initial_admin 建立管理員。
    if do_create_and_core:
        try:
            await _seed_app_users()
            logger.info("App users seeded.")
        except Exception as exc:  # noqa: BLE001
            logger.warning("App user seeding failed (continuing): %s", exc)

    # 初始管理員：所有模式皆嘗試 (resilient)。須在其他種子「之後」執行，
    # 確保租戶 (FK) 已就緒、且不與 demo 種子帳號衝突。
    try:
        await _seed_initial_admin()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Initial admin seeding failed (continuing): %s", exc)

    # 正式環境安全閘：若 production + 強制認證 + app_users 為空 + 未設定初始管理員，
    # 則拒絕啟動 (避免啟動一個無人可登入 / 不安全的正式系統)。
    await _assert_prod_has_admin()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """應用生命週期：啟動時 (1) 視模式 bootstrap DB；(2) best-effort 連線 Redis。"""
    # (1) 資料庫 bootstrap (create_all + seed)；全程 best-effort，不中斷啟動。
    await _bootstrap_database()

    # (2) Redis：best-effort 連線（失敗只記錄、不中斷）。
    redis_client = None
    try:
        import redis.asyncio as aioredis

        redis_client = aioredis.from_url(
            settings.redis_url, encoding="utf-8", decode_responses=True
        )
        pong = await redis_client.ping()
        logger.info("Redis connected (PING -> %s)", pong)
        app.state.redis = redis_client
    except Exception as exc:  # noqa: BLE001 - Redis 非啟動必要條件
        logger.warning("Redis ping failed (continuing without cache): %s", exc)
        app.state.redis = None

    try:
        yield
    finally:
        if redis_client is not None:
            try:
                await redis_client.aclose()
            except Exception:  # noqa: BLE001
                pass


app = FastAPI(
    title="企業級工程排程與自動化 SaaS (CPM / Critical Path)",
    description=(
        "Cross-strait (TW/CN) multi-tenant CPM scheduling SaaS for construction "
        "firms & engineering consultancies. 跨海峽多租戶要徑工程排程平台。"
    ),
    version="1.0.0",
    lifespan=lifespan,
)

# CORS（來源由設定檔提供，逗號分隔已於 config 解析為 list）
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 在 API 前綴下註冊所有路由器
_prefix = settings.api_v1_prefix
app.include_router(auth_router, prefix=_prefix)
app.include_router(schedule_router, prefix=_prefix)
app.include_router(projects_router, prefix=_prefix)
app.include_router(tasks_router, prefix=_prefix)
app.include_router(erp_router, prefix=_prefix)

# Phase 8 — 資源撫平 (resources) 與風險分析 (analytics) 路由器。
# 以模組路徑匯入並掛載於同一前綴之下；最後新增故置於既有路由器之後。
from app.routers.resources import router as resources_router  # noqa: E402
from app.routers.analytics import router as analytics_router  # noqa: E402

app.include_router(resources_router, prefix=_prefix)
app.include_router(analytics_router, prefix=_prefix)

# Phase 9 — 進度追蹤 / 實獲值管理 (EVM) 路由器 (progress)。
from app.routers.progress import router as progress_router  # noqa: E402

app.include_router(progress_router, prefix=_prefix)

# Batch 2 (CHANGE-5) — 稽核日誌查詢 (audit_view, admin-only)。
from app.routers.audit_view import router as audit_view_router  # noqa: E402

app.include_router(audit_view_router, prefix=_prefix)

# Phase 10 — 儀表板 (dashboard) / 使用者管理 (users) / 匯出 (exports) 路由器。
# 以 best-effort 匯入並掛載：這些模組由 Phase 10 的其他工作項建立；若於某中間
# 狀態尚未就緒則記錄並略過 (不中斷啟動)，待模組落地後即自動掛載。最後新增故置於
# 既有路由器之後。
for _mod_path, _label in (
    ("app.routers.dashboard", "dashboard"),
    ("app.routers.users", "users"),
    ("app.routers.exports", "exports"),
    # Pro Batch A — P6 XER / MS Project MSPDI 匯入匯出 (interop)。以 best-effort
    # 匯入掛載：app.interop.xer / app.interop.mspdi 為並行工作項建立的純函式
    # 解析器/產生器；若於某中間狀態尚未就緒則記錄並略過 (不中斷啟動)。
    ("app.routers.interop", "interop"),
    # Pro Batch C (FEATURE 1) — 任務照片附件 (mobile field reporting)。以
    # best-effort 掛載：Pro Batch C 其餘工作項 (QR deep-link / 前端現場模式)
    # 為並行工作項；若於某中間狀態尚未就緒則記錄並略過 (不中斷啟動)。
    ("app.routers.photos", "photos"),
    # Pro Batch D — 資源池 / 費率 / 成本負載 (cost) 與 DCMA 14-point 排程健康
    # 評估 (dcma)。以 best-effort 匯入掛載：Pro Batch D 其餘工作項 (前端
    # CostPanel / HealthPanel / 資源日曆) 為並行工作項；若於某中間狀態尚未
    # 就緒則記錄並略過 (不中斷啟動)。
    ("app.routers.cost", "cost"),
    ("app.routers.dcma", "dcma"),
    # Pro Batch E — 企業級 (tenant-level) 資源池 + 投資組合資源分配 (enterprise)。
    # 以 best-effort 匯入掛載：Pro Batch E 其餘工作項 (前端 EnterpriseResourcePanel)
    # 為並行工作項；若於某中間狀態尚未就緒則記錄並略過 (不中斷啟動)。
    ("app.routers.enterprise", "enterprise"),
):
    try:
        import importlib

        _module = importlib.import_module(_mod_path)
        app.include_router(_module.router, prefix=_prefix)
        logger.info("Mounted router: %s", _label)
    except Exception as exc:  # noqa: BLE001 - 模組尚未就緒不中斷啟動
        logger.warning("Router %s not mounted (continuing): %s", _label, exc)


@app.get("/health", tags=["meta"])
async def health() -> JSONResponse:
    """健康檢查端點（不需 tenant 標頭 / 認證；Batch 2 CHANGE-6a「真實」健檢）。

    回應形狀 {status, db, redis}：
      - db    : 經 get_sessionmaker() 執行 SELECT 1（短逾時）。DB 為必要依賴，
                失敗 -> 503（compose healthcheck / LB 據此判定不健康）。
      - redis : ping（優先重用 lifespan 建立的 app.state.redis；無則短暫新建
                連線測試）。Redis 為「選配」依賴：失敗僅回報 "down"，仍回 200。
      - status: db ok 且 redis ok -> "ok"；僅 redis down -> "degraded"（仍 200）；
                db down -> "error"（503）。

    刻意保持輕量（無認證、無租戶情境、短逾時），供 LB / docker healthcheck 高頻輪詢。
    """
    import asyncio

    from sqlalchemy import text

    from app import database

    # --- DB：SELECT 1（必要依賴；失敗 -> 503）--------------------------------
    db_status = "ok"

    async def _db_ping() -> None:
        async with database.get_sessionmaker()() as session:
            await session.execute(text("SELECT 1"))

    try:
        await asyncio.wait_for(_db_ping(), timeout=3.0)
    except Exception as exc:  # noqa: BLE001 - 逾時 / 連線失敗皆視為 down
        logger.warning("/health DB check failed: %s", exc)
        db_status = "down"

    # --- Redis：ping（選配依賴；失敗 -> "down" 但仍 200）----------------------
    redis_status = "ok"
    try:
        redis_client = getattr(app.state, "redis", None)
        owns_client = False
        if redis_client is None:
            # lifespan 啟動時未連上 (或尚未啟動)：短暫新建連線測試，用完即關。
            import redis.asyncio as aioredis

            redis_client = aioredis.from_url(
                settings.redis_url,
                encoding="utf-8",
                decode_responses=True,
                socket_connect_timeout=2,
                socket_timeout=2,
            )
            owns_client = True
        try:
            await asyncio.wait_for(redis_client.ping(), timeout=2.0)
        finally:
            if owns_client:
                try:
                    await redis_client.aclose()
                except Exception:  # noqa: BLE001 - 關閉失敗不影響健檢結果
                    pass
    except Exception as exc:  # noqa: BLE001 - Redis 非必要依賴，失敗僅標記 down
        logger.info("/health Redis check failed (optional dependency): %s", exc)
        redis_status = "down"

    if db_status != "ok":
        return JSONResponse(
            status_code=503,
            content={"status": "error", "db": db_status, "redis": redis_status},
        )
    return JSONResponse(
        content={
            "status": "ok" if redis_status == "ok" else "degraded",
            "db": db_status,
            "redis": redis_status,
        }
    )
