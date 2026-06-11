"""安全性快速修補 — 批次一測試 (security quick-fixes, batch 1) —— sqlite dev 模式。

本檔「不」標記 @pytest.mark.integration，故於開發機 / CI backend-tests 皆執行
(與 test_auth.py / test_dashboard_export.py 一致，不需 Postgres)。

涵蓋契約 (THE FIVE FIXES — 後端可由 API 驗證之行為)
--------------------------------------------------------
(a) FIX-3 模擬迭代上限：以 admin@tw 登入後 POST /projects/PRJ-2026-TW-001/simulate
    {"iterations": 999999} -> 422 (超過 SimulationRequest.iterations 之 le=10000)；
    {"iterations": 50} -> 200 (正常模擬)。
(b) FIX-2 登入鎖定 (lockout)：對一組不存在的帳密連續錯誤登入
    settings.login_max_failures 次 -> 皆 401；下一次 -> 429 且含 retry_after。
    以 monkeypatch 將 settings.login_max_failures 設為 3 以加速。
(c) FIX-2 timing-path 健全性：未知帳號登入回 401 (而非 500)；
    驗證 DUMMY_HASH timing-pad 路徑不會拋例外。
(d) FIX-1 種子閘門：本 sqlite + DEV_BOOTSTRAP=1 執行下 demo 帳號仍被種入
    (login admin@tw 成功) — 開發模式閘門仍會種子。

關鍵設計 (與 test_auth.py 完全一致)
-----------------------------------
1. 「import app 之前」即設定 DATABASE_URL=sqlite 檔案 + DEV_BOOTSTRAP=1。使用真實
   檔案 (非 :memory:) 以利跨連線 / 交易共享種子資料。
2. conftest.py 於收集階段即 import app，故 engine/SessionLocal 可能早已綁至他種
   DSN；client fixture 以 _rebind_sqlite_engine() 將整個 DB 層重綁至本檔 sqlite 檔，
   並於 teardown dispose engine、還原全域綁定、再刪暫存檔 (Windows 句柄安全)。
"""
from __future__ import annotations

import os
import tempfile

# --------------------------------------------------------------------------- #
# 於匯入 app 之前先設定環境變數 (env at top before importing app)。
# 使用具名暫存「檔案」作為 sqlite DB，確保跨連線共享資料且測試可重現。
# --------------------------------------------------------------------------- #
_DB_FD, _DB_PATH = tempfile.mkstemp(prefix="cpm_sec1_test_", suffix=".db")
os.close(_DB_FD)
# sqlite URL 以正斜線表示路徑 (Windows 亦適用)。
_DB_URL = "sqlite+aiosqlite:///" + _DB_PATH.replace("\\", "/")
os.environ["DATABASE_URL"] = _DB_URL
os.environ["DEV_BOOTSTRAP"] = "1"
# 預設維持 header mode (本檔不需強制 auth_required)。
os.environ.setdefault("AUTH_REQUIRED", "false")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402

PREFIX = settings.api_v1_prefix
LOGIN_URL = f"{PREFIX}/auth/login"
PROJECTS_URL = f"{PREFIX}/projects"

DEMO_PASSWORD = "demo1234"
TENANT_TW = "TENT-9981"
SEED_PROJECT_TW = "PRJ-2026-TW-001"
SIMULATE_URL = f"{PROJECTS_URL}/{SEED_PROJECT_TW}/simulate"


def _rebind_sqlite_engine() -> None:
    """把 app 的 DB 層強制指向本檔的 sqlite 暫存檔 (與 test_auth.py 同法)。

    conftest 會在收集階段先 import app，故 engine/SessionLocal 可能早已綁到別的
    DSN。這裡重建 async engine + sessionmaker (與 database.py 的 sqlite 分支一致：
    schema_translate_map 把 erp_integration 映射為 None、check_same_thread=False)，
    並更新所有「以 from app.database import SessionLocal 綁定」的模組參考
    (database / deps / routers.auth)。
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
    # 確保 lifespan 的 create_all() 與請求/種子皆落在本檔 sqlite (見 test_auth.py 註解)。
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


def _login_admin_tw(client: TestClient) -> str:
    """以 demo admin@tw 登入並回傳 Bearer access_token。"""
    resp = client.post(
        LOGIN_URL, json={"username": "admin@tw", "password": DEMO_PASSWORD}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


# --------------------------------------------------------------------------- #
# (d) FIX-1 種子閘門：sqlite + DEV_BOOTSTRAP=1 仍會種入 demo 帳號 (login 成功)。
#     先驗證此前提，後續測試 (a) 才有可用之 admin / 種子專案。
# --------------------------------------------------------------------------- #
def test_seed_gate_seeds_demo_users_in_dev(client):
    """開發閘門 (is_sqlite() 或 dev_bootstrap) 下 _seed_app_users 仍執行：
    admin@tw / demo1234 可成功登入，且回傳正確租戶 / 角色。
    """
    resp = client.post(
        LOGIN_URL, json={"username": "admin@tw", "password": DEMO_PASSWORD}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["access_token"]
    assert body["token_type"] == "bearer"
    assert body["tenant_id"] == TENANT_TW
    assert body["region"] == "TW"


# --------------------------------------------------------------------------- #
# (a) FIX-3 模擬迭代上限：超過上限 -> 422；合理次數 -> 200。
# --------------------------------------------------------------------------- #
def test_simulate_iterations_over_cap_422(client):
    """iterations 超過 SimulationRequest 的 le=10000 -> 422 (pydantic 驗證)。"""
    token = _login_admin_tw(client)
    resp = client.post(
        SIMULATE_URL,
        headers={"Authorization": f"Bearer {token}"},
        json={"iterations": 999999},
    )
    assert resp.status_code == 422, resp.text


def test_simulate_iterations_within_cap_200(client):
    """iterations 在上限內 (50) -> 200，且回傳 SimulationResult 形狀。"""
    token = _login_admin_tw(client)
    resp = client.post(
        SIMULATE_URL,
        headers={"Authorization": f"Bearer {token}"},
        json={"iterations": 50},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # 實際執行次數回填為 50；基本結構欄位齊備。
    assert body["iterations"] == 50
    assert "mean" in body
    assert "p50" in body


# --------------------------------------------------------------------------- #
# (b) FIX-2 登入鎖定：連續錯誤達 login_max_failures 次後，下一次回 429 + retry_after。
#     以 monkeypatch 把門檻設為 3 加速 (預設 5)。使用唯一帳號避免污染其他測試之計數器。
# --------------------------------------------------------------------------- #
def test_login_lockout_after_max_failures(client, monkeypatch):
    """達門檻前每次皆 401；達門檻後下一次 -> 429 且 body 含 retry_after。"""
    monkeypatch.setattr(settings, "login_max_failures", 3)

    username = "nosuch@user"
    creds = {"username": username, "password": "definitely-wrong"}

    # 連續 login_max_failures 次錯誤 -> 皆 401 (帳號不存在亦走 timing-pad 後計數 401)。
    for i in range(settings.login_max_failures):
        resp = client.post(LOGIN_URL, json=creds)
        assert resp.status_code == 401, f"attempt {i} expected 401, got {resp.text}"

    # 下一次 (超過門檻) -> 429 鎖定。retry_after 依 HTTP 慣例由 Retry-After header
    # 帶出 (auth.py 以 headers={"Retry-After": str(retry_after)} 回傳)；body.detail
    # 為契約之雙語訊息。
    locked = client.post(LOGIN_URL, json=creds)
    assert locked.status_code == 429, locked.text
    body = locked.json()
    assert body["detail"] == "嘗試次數過多，請稍後再試"
    # Retry-After header 必須存在且為正整數秒數。
    retry_after = locked.headers.get("retry-after")
    assert retry_after is not None, "429 應帶 Retry-After header"
    assert int(retry_after) > 0


# --------------------------------------------------------------------------- #
# (c) FIX-2 timing-path 健全性：未知帳號 -> 401 (而非 500)。
#     使用獨立唯一帳號 + 提高門檻，確保單次嘗試不會因前一測試之鎖定而誤判。
# --------------------------------------------------------------------------- #
def test_unknown_user_returns_401_not_500(client, monkeypatch):
    """未知帳號登入：DUMMY_HASH timing-pad 路徑不應拋例外，回 401。"""
    # 提高門檻，確保此單次嘗試之前不會被鎖定 (與 (b) 的計數器互不干擾：帳號不同)。
    monkeypatch.setattr(settings, "login_max_failures", 50)

    resp = client.post(
        LOGIN_URL,
        json={"username": "ghost@nowhere", "password": "irrelevant"},
    )
    assert resp.status_code == 401, resp.text
