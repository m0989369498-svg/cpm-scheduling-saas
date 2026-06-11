"""生產就緒 (production readiness) — 批次二測試 (Batch 2) —— sqlite dev 模式。

本檔「不」標記 @pytest.mark.integration，故於開發機 / CI backend-tests 皆執行
(與 test_auth.py / test_security_batch1.py 一致，不需 Postgres)。

涵蓋契約 (BATCH 2 — 六項驗收)
--------------------------------------------------------
(a) CHANGE-1a 弱密鑰 fail-fast：Settings(app_env="production", jwt_secret="short")
    -> raise (ValueError / ValidationError)；development 僅警告、不拋出。
(b) CHANGE-1b 停用立即生效：admin 建立使用者並登入後，admin PUT is_active=false，
    舊 token 下一次請求 -> 401 (monkeypatch settings.auth_required=True)。
(c) CHANGE-1b/c 角色回退：為「真實 active 使用者」偽造「無 role claim」的 token
    -> 角色以 DB 列為準 (viewer@tw 得 viewer、寫入 403；editor@tw 得 editor，
    證明取自 DB 而非固定 viewer 回退)。
(d) CHANGE-6a /health：回傳 {status, db, redis}，sqlite 下 db 為 ok。
(e) CHANGE-3 通知 outbox：POST evm/alert 於「落後+超支」種子專案觸發風險拋轉
    -> notification_outbox 有 >=1 筆 PENDING (無憑證 -> LOG 通道退路)；
    執行 worker.deliver_outbox_once() -> 該列翻為 SUCCESS。
(f) CHANGE-5 GET /audit：admin 可查 (含 LOGIN_SUCCESS、支援 action 過濾)；
    viewer -> 403。

關鍵設計 (與 test_auth.py 完全一致)
-----------------------------------
1. 「import app 之前」即設定 DATABASE_URL=sqlite 檔案 + DEV_BOOTSTRAP=1。使用真實
   檔案 (非 :memory:) 以利跨連線 / 交易共享種子資料。
2. conftest.py 於收集階段即 import app，故 engine/SessionLocal 可能早已綁至他種
   DSN；client fixture 以 _rebind_sqlite_engine() 將整個 DB 層重綁至本檔 sqlite 檔
   —— 包含 app.erp.worker 的 module-level engine / WorkerSessionLocal (Batch 2：
   deliver_outbox_once 經其存取 notification_outbox) —— 並於 teardown dispose
   engine、還原全域綁定、再刪暫存檔 (Windows 句柄安全)。

種子 demo 帳號 (密碼 demo1234)：
    admin@tw  -> TENT-9981 / TW / admin
    editor@tw -> TENT-9981 / TW / editor
    viewer@tw -> TENT-9981 / TW / viewer
"""
from __future__ import annotations

import asyncio
import os
import tempfile
from datetime import datetime, timedelta, timezone

# --------------------------------------------------------------------------- #
# 於匯入 app 之前先設定環境變數 (env at top before importing app)。
# 使用具名暫存「檔案」作為 sqlite DB，確保跨連線共享資料且測試可重現。
# --------------------------------------------------------------------------- #
_DB_FD, _DB_PATH = tempfile.mkstemp(prefix="cpm_prodready_test_", suffix=".db")
os.close(_DB_FD)
# sqlite URL 以正斜線表示路徑 (Windows 亦適用)。
_DB_URL = "sqlite+aiosqlite:///" + _DB_PATH.replace("\\", "/")
os.environ["DATABASE_URL"] = _DB_URL
os.environ["DEV_BOOTSTRAP"] = "1"
# 預設維持 header mode (個別測試再以 monkeypatch 切換 auth_required)。
os.environ.setdefault("AUTH_REQUIRED", "false")

import jwt as pyjwt  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.config import Settings, settings  # noqa: E402
from app.main import app  # noqa: E402

PREFIX = settings.api_v1_prefix
LOGIN_URL = f"{PREFIX}/auth/login"
ME_URL = f"{PREFIX}/auth/me"
PROJECTS_URL = f"{PREFIX}/projects"
USERS_URL = f"{PREFIX}/users"
AUDIT_URL = f"{PREFIX}/audit"

DEMO_PASSWORD = "demo1234"
TENANT_TW = "TENT-9981"
# 種子「落後 + 超支」示範專案：預設 data_date=10 時 SPI=0.62 / CPI≈0.83，
# 兩者皆 < 0.9 -> compute_evm risk_flagged=True (觸發 evm/alert 拋轉)。
SEED_PROJECT_TW = "PRJ-2026-TW-001"


# --------------------------------------------------------------------------- #
# DB 重綁 / 還原 (與 test_auth.py 同法；Batch 2 額外涵蓋 app.erp.worker)
# --------------------------------------------------------------------------- #
def _rebind_sqlite_engine() -> None:
    """把 app 的 DB 層強制指向本檔的 sqlite 暫存檔 (不論 conftest 先前綁定何種 DSN)。

    conftest 會在收集階段先 import app，故 engine/SessionLocal 可能早已綁到別的
    DSN。這裡重建 async engine + sessionmaker (與 database.py 的 sqlite 分支一致：
    schema_translate_map 把 erp_integration 映射為 None、check_same_thread=False)，
    並更新所有「以 from app.database import SessionLocal 綁定」的模組參考
    (database / deps / routers.auth)，以及 Batch 2 新增的 worker 綁定。
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.ext.asyncio import AsyncSession

    import app.database as database

    # settings 切到本檔 sqlite + dev_bootstrap，確保 is_sqlite()/bootstrap 行為正確。
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

    # 同步更新 database 的 lazy 快取 (_engine / _SessionLocal) 與 public 屬性，
    # 確保 lifespan 的 create_all() 與請求/種子皆落在本檔 sqlite (見 test_auth.py)。
    database._engine = new_engine
    database._SessionLocal = new_sessionmaker
    database.engine = new_engine
    database.SessionLocal = new_sessionmaker

    import app.deps as deps
    import app.routers.auth as auth_router_mod

    deps.SessionLocal = new_sessionmaker
    auth_router_mod.SessionLocal = new_sessionmaker

    # Batch 2 (CHANGE-3c)：worker 的 module-level engine / WorkerSessionLocal 於
    # import 當下以「當時的」DSN 建立；deliver_outbox_once 經 WorkerSessionLocal
    # 存取 notification_outbox，故一併重綁至本檔 sqlite (new_engine 已帶
    # erp_integration -> None 的 schema_translate_map)。
    try:
        import app.erp.worker as worker_mod
    except Exception:  # noqa: BLE001 - 缺選配依賴時讓 outbox 測試自行顯露錯誤
        pass
    else:
        worker_mod._engine = new_engine
        worker_mod.WorkerSessionLocal = new_sessionmaker


def _dispose_sqlite_engines() -> None:
    """釋放所有綁到本檔 sqlite 暫存檔的 async engine (Windows 釋放檔案句柄)。"""
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
    """快照 _rebind_sqlite_engine()/lifespan 會變動的全域 DB 綁定 (避免污染後續測試)。"""
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

    # Batch 2：worker 的 module-level 綁定亦需快照 / 還原。
    try:
        import app.erp.worker as worker_mod
    except Exception:  # noqa: BLE001
        pass
    else:
        for name in ("_engine", "WorkerSessionLocal"):
            snap["attrs"][(worker_mod.__name__, name)] = worker_mod.__dict__.get(
                name, _MISSING
            )
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
# 小工具
# --------------------------------------------------------------------------- #
def _login(client: TestClient, username: str, password: str = DEMO_PASSWORD) -> str:
    """登入並回傳 Bearer access_token。"""
    resp = client.post(LOGIN_URL, json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _forge_token_without_role(username: str) -> str:
    """為真實 (種子) 使用者偽造「無 role claim」的有效 token (模擬舊版 token)。"""
    claims = {
        "sub": username,
        "tenant_id": TENANT_TW,
        "region": "TW",
        "exp": datetime.now(timezone.utc) + timedelta(minutes=30),
    }
    return pyjwt.encode(claims, settings.jwt_secret, algorithm=settings.jwt_algorithm)


# --------------------------------------------------------------------------- #
# (a) CHANGE-1a — 弱 JWT 密鑰於正式環境 fail-fast；非正式環境僅警告。
#     (純 Settings 驗證，不需 client / DB。)
# --------------------------------------------------------------------------- #
def test_weak_jwt_secret_fails_fast_in_production():
    """production + 弱密鑰 -> raise；development + 弱密鑰 -> 不拋出 (僅警告)。"""
    # 太短 (<32 bytes) -> production 拒絕啟動。
    # (pydantic 將 model_validator 的 ValueError 包裝為 ValidationError，
    #  其本身即為 ValueError 子類，故 pytest.raises(ValueError) 兩者皆涵蓋。)
    with pytest.raises(ValueError):
        Settings(app_env="production", jwt_secret="short", _env_file=None)

    # 已知弱預設值 (即使長度 >=32 bytes) -> "prod" 別名同樣拒絕。
    with pytest.raises(ValueError):
        Settings(
            app_env="prod",
            jwt_secret="dev-only-insecure-secret-DO-NOT-USE-IN-PROD-change-me",
            _env_file=None,
        )

    # 非正式環境：僅記錄警告、不中斷 (本機開發韌性)。
    dev = Settings(app_env="development", jwt_secret="short", _env_file=None)
    assert dev.jwt_secret == "short"

    # 正式環境 + 高強度密鑰 (>=32 bytes 且非預設) -> 正常建立。
    strong = "s" * 64
    prod = Settings(app_env="production", jwt_secret=strong, _env_file=None)
    assert prod.jwt_secret == strong


# --------------------------------------------------------------------------- #
# (d) CHANGE-6a — /health 回傳 {status, db, redis}；sqlite 下 db 為 ok。
# --------------------------------------------------------------------------- #
def test_health_reports_db_and_redis_status(client):
    """真實健檢：DB SELECT 1 成功 -> 200 + db:"ok"；redis 為選配 (ok 或 down)。"""
    resp = client.get("/health")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body.keys()) >= {"status", "db", "redis"}
    assert body["db"] == "ok"
    # 開發機通常無 Redis (down)；CI backend-tests 有 redis service (ok)。
    assert body["redis"] in {"ok", "down"}
    assert body["status"] in {"ok", "degraded"}


# --------------------------------------------------------------------------- #
# (b) CHANGE-1b — 停用立即生效：停用後「既有 token」下一次請求即 401。
# --------------------------------------------------------------------------- #
def test_deactivated_user_old_token_rejected(client, monkeypatch):
    """admin 建立使用者 -> 該使用者登入 -> admin 停用 -> 舊 token -> 401。"""
    monkeypatch.setattr(settings, "auth_required", True)

    admin_token = _login(client, "admin@tw")

    # admin 建立一個 editor 使用者 (非 admin，避開「最後一位 admin」護欄)。
    created = client.post(
        USERS_URL,
        headers=_bearer(admin_token),
        json={"username": "deact@tw", "password": "deact-pass-123", "role": "editor"},
    )
    assert created.status_code == 201, created.text
    user_id = created.json()["id"]

    # 新使用者登入後，token 可正常使用 (停用前)。
    user_token = _login(client, "deact@tw", "deact-pass-123")
    ok = client.get(PROJECTS_URL, headers=_bearer(user_token))
    assert ok.status_code == 200, ok.text

    # admin 停用該使用者。
    updated = client.put(
        f"{USERS_URL}/{user_id}",
        headers=_bearer(admin_token),
        json={"is_active": False},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["is_active"] is False

    # 舊 token 簽章仍有效，但 DB 即時複查 -> 401 (停用立即生效，不必等過期)。
    rejected = client.get(PROJECTS_URL, headers=_bearer(user_token))
    assert rejected.status_code == 401, rejected.text


# --------------------------------------------------------------------------- #
# (c) CHANGE-1b/c — 無 role claim 的 token：角色以 DB 列為準。
# --------------------------------------------------------------------------- #
def test_missing_role_claim_falls_back_to_db_role(client, monkeypatch):
    """viewer@tw (無 role claim) -> viewer (寫入 403)；editor@tw -> editor (取自 DB)。"""
    monkeypatch.setattr(settings, "auth_required", True)

    viewer_token = _forge_token_without_role("viewer@tw")

    # 讀取 (唯讀端點不掛 require_role) -> 200。
    read = client.get(PROJECTS_URL, headers=_bearer(viewer_token))
    assert read.status_code == 200, read.text

    # /auth/me 回報的角色來自 DB 列 (claim 缺漏不得回退 admin)。
    me = client.get(ME_URL, headers=_bearer(viewer_token))
    assert me.status_code == 200, me.text
    assert me.json()["role"] == "viewer"

    # 寫入 (POST baseline 掛 require_role("editor")) -> 403。
    write = client.post(
        f"{PROJECTS_URL}/{SEED_PROJECT_TW}/baseline",
        headers=_bearer(viewer_token),
        json={"name": "should-be-forbidden"},
    )
    assert write.status_code == 403, write.text

    # 同樣「無 role claim」但 DB 角色為 editor -> 得 editor：
    # 證明角色確實取自 DB，而非「缺 claim 即固定 viewer」的回退。
    editor_token = _forge_token_without_role("editor@tw")
    me2 = client.get(ME_URL, headers=_bearer(editor_token))
    assert me2.status_code == 200, me2.text
    assert me2.json()["role"] == "editor"


# --------------------------------------------------------------------------- #
# (e) CHANGE-3 — 通知 outbox：evm/alert 入列 PENDING/LOG 列；worker 投遞 -> SUCCESS。
# --------------------------------------------------------------------------- #
def test_outbox_enqueue_and_deliver(client, monkeypatch):
    """風險拋轉 -> notification_outbox >=1 筆 PENDING (LOG 退路)；
    deliver_outbox_once() -> 該列翻為 SUCCESS。"""
    # 清空全域通知憑證 -> 通道解析為空 -> 退路入列單筆 channel='LOG'
    # (deterministic：絕不對外發出真實 HTTP 通知)。
    monkeypatch.setattr(settings, "line_channel_access_token", "")
    monkeypatch.setattr(settings, "dingtalk_webhook_url", "")
    monkeypatch.setattr(settings, "wecom_webhook_url", "")

    token = _login(client, "admin@tw")

    # 種子 TW 專案為「落後 + 超支」(SPI/CPI < 0.9) -> risk_flagged -> 拋轉 + 入列。
    resp = client.post(
        f"{PROJECTS_URL}/{SEED_PROJECT_TW}/evm/alert",
        headers=_bearer(token),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["dispatched"] is True
    assert body["risk_flagged"] is True
    # 契約：notified=True <=> 至少一筆 outbox 列已入列。
    assert body["notified"] is True

    from sqlalchemy import select

    import app.database as database
    from app.models.orm import NotificationOutbox

    async def _fetch_rows() -> list[tuple]:
        async with database.SessionLocal() as session:
            result = await session.execute(select(NotificationOutbox))
            return [
                (r.id, r.tenant_id, r.channel, r.status)
                for r in result.scalars().all()
            ]

    rows = asyncio.run(_fetch_rows())
    pending = [r for r in rows if r[3] == "PENDING"]
    assert len(pending) >= 1, f"應有 >=1 筆 PENDING outbox 列，實際: {rows}"
    # 無任何憑證 -> LOG 通道退路 (管線在 demo 中仍可觀測)。
    assert any(r[2] == "LOG" for r in pending)
    # 寫入隔離：tenant_id 一律取自 ctx。
    assert all(r[1] == TENANT_TW for r in pending)

    # worker 單次投遞 (LOG 通道：記錄日誌即 SUCCESS)。
    from app.erp.worker import deliver_outbox_once

    stats = asyncio.run(deliver_outbox_once())
    assert stats["processed"] >= 1
    assert stats["success"] >= 1

    after = {r[0]: r for r in asyncio.run(_fetch_rows())}
    for row in pending:
        assert after[row[0]][3] == "SUCCESS", f"outbox 列未翻為 SUCCESS: {after[row[0]]}"


# --------------------------------------------------------------------------- #
# (f) CHANGE-5 — GET /audit：admin 可查 (含 LOGIN_SUCCESS)；viewer -> 403。
# --------------------------------------------------------------------------- #
def test_audit_endpoint_admin_only_includes_login_success(client):
    """admin 查得稽核列 (形狀 + LOGIN_SUCCESS + action 過濾)；viewer 403。"""
    admin_token = _login(client, "admin@tw")  # 此登入本身寫入一筆 LOGIN_SUCCESS

    resp = client.get(AUDIT_URL, headers=_bearer(admin_token))
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    assert isinstance(rows, list) and len(rows) >= 1
    for key in ("id", "actor", "action", "detail", "created_at"):
        assert key in rows[0], f"audit 列缺少欄位 {key}: {rows[0]}"

    # action 完全比對過濾：至少含 admin@tw 的 LOGIN_SUCCESS。
    filtered = client.get(
        AUDIT_URL,
        params={"action": "LOGIN_SUCCESS"},
        headers=_bearer(admin_token),
    )
    assert filtered.status_code == 200, filtered.text
    frows = filtered.json()
    assert len(frows) >= 1
    assert all(r["action"] == "LOGIN_SUCCESS" for r in frows)
    assert any(r["actor"] == "admin@tw" for r in frows)

    # viewer -> 403 (require_role("admin"))。
    viewer_token = _login(client, "viewer@tw")
    denied = client.get(AUDIT_URL, headers=_bearer(viewer_token))
    assert denied.status_code == 403, denied.text
