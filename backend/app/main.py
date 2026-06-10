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
async def _seed_core_data() -> None:
    """冪等寫入核心示範資料 (僅在 sqlite / dev_bootstrap 模式呼叫)。

    PostgreSQL 正式環境的核心資料由 db/init.sql 權威建立，不走此路徑。
    內容：兩個租戶 (TENT-9981/TW、TENT-CN-002/CN)、兩個專案及其任務/相依、
    ERP 設定列。皆「不存在才插入」(以主鍵 / 唯一鍵查詢) 故可重入。
    """
    from sqlalchemy import select

    from app import database
    from app.models.orm import (
        ErpConfig,
        Project,
        ProjectResourceLimit,
        Task,
        TaskDependency,
        TaskRiskParameter,
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
    #   limits : (resource_type, max_capacity)            專案資源上限
    #   risk   : (task_id, optimistic, most_likely, pessimistic)  PERT 三點估計
    projects = {
        "PRJ-2026-TW-001": {
            "tenant_id": "TENT-9981",
            "project_name": "2026 示範建案工程排程",
            "region": "TW",
            "tasks": [
                ("T-01", "基地開挖", 5, "COMPLETED", 0, 5, 0, 5, 0, True,
                    {"crane": 1, "manpower": 10}),
                ("T-02", "一樓鋼筋綁紮", 3, "IN_PROGRESS", 5, 8, 5, 8, 0, True,
                    {"crane": 2, "manpower": 15}),
                ("T-03", "一樓混凝土澆置", 2, "PENDING", 8, 10, 8, 10, 0, True,
                    {"crane": 2, "manpower": 8}),
            ],
            "deps": [("T-02", "T-01"), ("T-03", "T-02")],
            "limits": [("crane", 2), ("manpower", 20)],
            "risk": [
                ("T-01", 3, 5, 9),
                ("T-02", 2, 3, 7),
                ("T-03", 1, 2, 5),
            ],
        },
        "PRJ-2026-CN-001": {
            "tenant_id": "TENT-CN-002",
            "project_name": "2026 示范建筑工程排程",
            "region": "CN",
            "tasks": [
                ("C-01", "土方开挖", 4, "COMPLETED", 0, 4, 0, 4, 0, True,
                    {"crane": 1, "manpower": 12}),
                ("C-02", "基础施工", 6, "IN_PROGRESS", 4, 10, 4, 10, 0, True,
                    {"crane": 2, "manpower": 18}),
            ],
            "deps": [("C-02", "C-01")],
            "limits": [("crane", 2), ("manpower", 20)],
            "risk": [
                ("C-01", 2, 4, 8),
                ("C-02", 4, 6, 11),
            ],
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
                        )
                    )
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
                for resource_type, max_capacity in p.get("limits", []):
                    found = await session.execute(
                        select(ProjectResourceLimit).where(
                            ProjectResourceLimit.project_id == project_id,
                            ProjectResourceLimit.resource_type == resource_type,
                        )
                    )
                    if found.scalar_one_or_none() is None:
                        session.add(
                            ProjectResourceLimit(
                                project_id=project_id,
                                tenant_id=p["tenant_id"],
                                resource_type=resource_type,
                                max_capacity=max_capacity,
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


async def _seed_app_users() -> None:
    """冪等寫入示範登入帳號 (在 ALL 模式 — pg 與 sqlite — 皆執行)。

    以 passlib 雜湊密碼 (pbkdf2_sha256，純 python)，username 不存在才插入。
    db/init.sql 刻意「不」種 app_users (避免在 SQL 寫死預雜湊)，由此處統一種。
    需要 tenants 已存在 (FK)；於 pg 由 init.sql 提供、於 sqlite 由 _seed_core_data 提供。
    """
    from sqlalchemy import select

    from app import database
    from app.core.security import hash_password
    from app.models.orm import AppUser

    SessionLocal = database.SessionLocal

    # (username, password, tenant_id, region)
    users = [
        ("admin@tw", "demo1234", "TENT-9981", "TW"),
        ("admin@cn", "demo1234", "TENT-CN-002", "CN"),
    ]
    async with SessionLocal() as session:
        async with session.begin():
            for username, password, tenant_id, region in users:
                found = await session.execute(
                    select(AppUser).where(AppUser.username == username)
                )
                if found.scalar_one_or_none() is None:
                    session.add(
                        AppUser(
                            tenant_id=tenant_id,
                            username=username,
                            password_hash=hash_password(password),
                            region=region,
                            is_active=True,
                        )
                    )


async def _bootstrap_database() -> None:
    """啟動時的資料庫初始化 (create_all + 種子)，全程 best-effort。

    - sqlite 或 settings.dev_bootstrap=True：create_all + 種核心資料 + 種帳號。
    - 其餘 (PostgreSQL 正式環境)：schema/核心資料由 init.sql 權威建立；
      此處僅種「應用帳號」(init.sql 不種帳號)。
    """
    do_create_and_core = is_sqlite() or settings.dev_bootstrap

    if do_create_and_core:
        try:
            from app.database import create_all

            await create_all()
            logger.info("Database tables created (create_all).")
        except Exception as exc:  # noqa: BLE001 - 不可中斷啟動
            logger.warning("create_all failed (continuing): %s", exc)
        try:
            await _seed_core_data()
            logger.info("Core demo data seeded.")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Core data seeding failed (continuing): %s", exc)

    # 帳號種子：所有模式皆執行 (resilient)。
    try:
        await _seed_app_users()
        logger.info("App users seeded.")
    except Exception as exc:  # noqa: BLE001
        logger.warning("App user seeding failed (continuing): %s", exc)


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


@app.get("/health", tags=["meta"])
async def health() -> dict:
    """健康檢查端點（不需 tenant 標頭）。"""
    return {"status": "ok"}
