"""RBAC 權限測試 (roles & write-enforcement) —— 在 sqlite dev 模式下執行 (不需 Postgres)。

本檔「不」標記 @pytest.mark.integration，因此於一般開發機 / CI backend-tests
任務 (預設 discovery) 皆會執行。

涵蓋契約 (Feature 2 — Roles & Users)
------------------------------------
1. 角色階層 admin > editor > viewer (deps.ROLE_ORDER)；登入回應 + JWT 帶 role claim。
2. 寫入端點以 Depends(require_role("editor")) 守門：
     - viewer 對純讀 / 計算端點 (GET /projects、GET .../progress) -> 200。
     - viewer 對持久化端點 (POST /projects、PUT .../progress) -> 403。
     - editor 對持久化端點 -> 200/201。
3. 使用者管理 (/users) 全數 require_role("admin")：
     - 非 admin (editor / viewer) GET /users -> 403。
     - admin (admin@tw) GET /users -> 200；POST /users -> 201/200。
     - 由 admin 建立新使用者後，以該帳號登入可成功並取得對應 role。

設計與 test_auth.py 一致 (見該檔詳註)
-------------------------------------
* 於「import app 之前」即設定 DATABASE_URL=sqlite 檔 (非 :memory:) + DEV_BOOTSTRAP=1，
  使跨連線 / 交易共享同一份資料 (login 另開連線仍查得到種子)。
* _rebind_sqlite_engine() 把 app 的 DB 層 (database / deps / routers.auth) 強制綁到
  本檔 sqlite 暫存檔，之後 with TestClient(app) 觸發 lifespan：create_all + 種核心資料
  + 種 app users (含本檔賴以驗證的 editor@tw / viewer@tw demo 帳號)。
* 以 monkeypatch 把 settings.auth_required 暫設 True，使請求「必須」帶 Bearer，
  令 verify_tenant 由 token 的 role claim 推導 ctx.role (require_role 方能據以授權)。
* teardown：dispose engine -> 還原全域 DB 綁定 (避免污染後續 Postgres 整合測試)
  -> 重試刪除暫存檔 (Windows 句柄釋放延遲)。
"""
from __future__ import annotations

import os
import tempfile

# --------------------------------------------------------------------------- #
# 於匯入 app 之前先設定環境變數 (env at top before importing app)。
# --------------------------------------------------------------------------- #
_DB_FD, _DB_PATH = tempfile.mkstemp(prefix="cpm_perm_test_", suffix=".db")
os.close(_DB_FD)
_DB_URL = "sqlite+aiosqlite:///" + _DB_PATH.replace("\\", "/")
os.environ["DATABASE_URL"] = _DB_URL
os.environ["DEV_BOOTSTRAP"] = "1"
# 預設維持 header mode；個別測試再以 monkeypatch 切換 auth_required。
os.environ.setdefault("AUTH_REQUIRED", "false")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402

PREFIX = settings.api_v1_prefix
LOGIN_URL = f"{PREFIX}/auth/login"
PROJECTS_URL = f"{PREFIX}/projects"
USERS_URL = f"{PREFIX}/users"

# 種子 demo 帳號 (密碼 demo1234)；editor@tw / viewer@tw 由 Feature 2 種子新增。
DEMO_PASSWORD = "demo1234"
TENANT_TW = "TENT-9981"
SEED_PROJECT_TW = "PRJ-2026-TW-001"


def _rebind_sqlite_engine() -> None:
    """把 app 的 DB 層強制指向本檔的 sqlite 暫存檔 (與 test_auth.py 同法)。"""
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
    """釋放所有綁到本檔 sqlite 暫存檔的 async engine (Windows 釋放檔案句柄)。"""
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
    """快照 _rebind_sqlite_engine()/lifespan 會變動的全域 DB 綁定。"""
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
    """還原 _snapshot_app_db_state() 取得的全域 DB 綁定 (避免污染後續測試)。"""
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


@pytest.fixture()
def auth_required(monkeypatch):
    """暫時開啟強制認證 (auth_required=True)，使 ctx.role 由 Bearer token claim 推導。

    require_role 讀取的 ctx.role 在 header/dev 模式固定為 admin (無法區分角色)，
    故 RBAC 守門測試必須走 Bearer 模式。monkeypatch 於測試結束後自動還原。
    """
    monkeypatch.setattr(settings, "auth_required", True)
    yield


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def _login(client, username: str, password: str = DEMO_PASSWORD):
    """登入並回傳完整回應物件 (供斷言 status_code / body)。"""
    return client.post(LOGIN_URL, json={"username": username, "password": password})


def _token(client, username: str, password: str = DEMO_PASSWORD) -> str:
    """登入並取出 access_token (要求登入必須成功)。"""
    resp = _login(client, username, password)
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------------- #
# (1) 登入回應帶 role；角色種子正確
# --------------------------------------------------------------------------- #
def test_login_returns_role_for_seeded_users(client):
    """admin@tw / editor@tw / viewer@tw 登入皆 200，且回應 role 與種子一致。"""
    expected = {
        "admin@tw": "admin",
        "editor@tw": "editor",
        "viewer@tw": "viewer",
    }
    for username, role in expected.items():
        resp = _login(client, username)
        assert resp.status_code == 200, f"{username}: {resp.text}"
        body = resp.json()
        assert body["tenant_id"] == TENANT_TW
        assert body["role"] == role, f"{username} 角色應為 {role}: {body}"
        assert body["access_token"]


# --------------------------------------------------------------------------- #
# (2) viewer：GET 讀取允許 (200)，但寫入被拒 (403)
# --------------------------------------------------------------------------- #
def test_viewer_can_read_projects(client, auth_required):
    """viewer GET /projects -> 200 (純讀，viewer 允許)。"""
    token = _token(client, "viewer@tw")
    resp = client.get(PROJECTS_URL, headers=_bearer(token))
    assert resp.status_code == 200, resp.text
    assert isinstance(resp.json(), list)


def test_viewer_can_read_progress(client, auth_required):
    """viewer GET /projects/{pid}/progress -> 200 (純讀，viewer 允許)。"""
    token = _token(client, "viewer@tw")
    resp = client.get(
        f"{PROJECTS_URL}/{SEED_PROJECT_TW}/progress", headers=_bearer(token)
    )
    assert resp.status_code == 200, resp.text
    assert isinstance(resp.json(), list)


def test_viewer_cannot_create_project(client, auth_required):
    """viewer POST /projects -> 403 (持久化端點需 editor 以上)。"""
    token = _token(client, "viewer@tw")
    payload = {
        "project_id": "PRJ-VIEWER-DENIED",
        "project_name": "viewer 不應能建立",
        "region": "TW",
        "schedule_data": [
            {"task_id": "V-01", "task_name": "x", "duration": 2, "predecessors": []},
        ],
    }
    resp = client.post(PROJECTS_URL, json=payload, headers=_bearer(token))
    assert resp.status_code == 403, resp.text


def test_viewer_cannot_put_progress(client, auth_required):
    """viewer PUT /projects/{pid}/progress -> 403 (持久化端點需 editor 以上)。"""
    token = _token(client, "viewer@tw")
    payload = [
        {
            "task_id": "T-01",
            "budget": 1.0,
            "percent_complete": 10,
            "actual_cost": 1.0,
            "actual_start_day": None,
            "actual_finish_day": None,
        }
    ]
    resp = client.put(
        f"{PROJECTS_URL}/{SEED_PROJECT_TW}/progress",
        json=payload,
        headers=_bearer(token),
    )
    assert resp.status_code == 403, resp.text


# --------------------------------------------------------------------------- #
# (3) editor：寫入允許 (200 / 201)
# --------------------------------------------------------------------------- #
def test_editor_can_create_project(client, auth_required):
    """editor POST /projects -> 201 (editor 可寫)。"""
    token = _token(client, "editor@tw")
    payload = {
        "project_id": "PRJ-EDITOR-OK",
        "project_name": "editor 建立的專案",
        "region": "TW",
        "schedule_data": [
            {"task_id": "E-01", "task_name": "開挖", "duration": 3, "predecessors": []},
            {"task_id": "E-02", "task_name": "結構", "duration": 2,
             "predecessors": ["E-01"]},
        ],
    }
    resp = client.post(PROJECTS_URL, json=payload, headers=_bearer(token))
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["project_id"] == "PRJ-EDITOR-OK"
    assert body["tenant_id"] == TENANT_TW


def test_editor_can_put_progress(client, auth_required):
    """editor PUT /projects/{pid}/progress -> 200 (editor 可寫)。"""
    token = _token(client, "editor@tw")
    payload = [
        {
            "task_id": "T-01",
            "budget": 50000.0,
            "percent_complete": 100,
            "actual_cost": 55000.0,
            "actual_start_day": 0,
            "actual_finish_day": 6,
        }
    ]
    resp = client.put(
        f"{PROJECTS_URL}/{SEED_PROJECT_TW}/progress",
        json=payload,
        headers=_bearer(token),
    )
    assert resp.status_code == 200, resp.text
    assert isinstance(resp.json(), list)


# --------------------------------------------------------------------------- #
# (4) /users —— 全數 require_role("admin")
# --------------------------------------------------------------------------- #
def test_non_admin_cannot_list_users(client, auth_required):
    """editor / viewer GET /users -> 403 (使用者管理僅 admin)。"""
    for username in ("editor@tw", "viewer@tw"):
        token = _token(client, username)
        resp = client.get(USERS_URL, headers=_bearer(token))
        assert resp.status_code == 403, f"{username}: {resp.text}"


def test_admin_can_list_users(client, auth_required):
    """admin GET /users -> 200，且回傳清單「不」外洩 password_hash。"""
    token = _token(client, "admin@tw")
    resp = client.get(USERS_URL, headers=_bearer(token))
    assert resp.status_code == 200, resp.text
    users = resp.json()
    assert isinstance(users, list)
    usernames = {u["username"] for u in users}
    # 種子的三個 TW demo 帳號皆應在當前租戶範圍內。
    assert {"admin@tw", "editor@tw", "viewer@tw"} <= usernames
    for u in users:
        assert "password_hash" not in u
        assert u["tenant_id"] == TENANT_TW  # 範圍限於 ctx.tenant_id


def test_admin_create_user_then_login(client, auth_required):
    """admin POST /users 建立新使用者 -> 2xx；以新帳號登入成功且 role 正確。"""
    token = _token(client, "admin@tw")
    new_username = "newviewer@tw"
    new_password = "newpass123"
    create = client.post(
        USERS_URL,
        json={"username": new_username, "password": new_password, "role": "viewer"},
        headers=_bearer(token),
    )
    assert create.status_code in (200, 201), create.text
    created = create.json()
    assert created["username"] == new_username
    assert created["role"] == "viewer"
    assert created["tenant_id"] == TENANT_TW
    assert "password_hash" not in created

    # 以新帳號登入 (header 模式即可；登入端點本身不需 Bearer)。
    login = _login(client, new_username, new_password)
    assert login.status_code == 200, login.text
    body = login.json()
    assert body["tenant_id"] == TENANT_TW
    assert body["role"] == "viewer"


def test_admin_create_duplicate_user_conflict(client, auth_required):
    """admin POST /users 重複 username -> 409 (唯一性由端點守住)。"""
    token = _token(client, "admin@tw")
    resp = client.post(
        USERS_URL,
        json={"username": "editor@tw", "password": "whatever123", "role": "editor"},
        headers=_bearer(token),
    )
    assert resp.status_code == 409, resp.text
