"""JWT 認證測試 (Auth tests) —— 在 sqlite dev 模式下執行 (不需 Postgres)。

本檔「不」標記 @pytest.mark.integration，因此在一般開發機 / CI backend-tests
任務 (預設 discovery) 皆會執行。

關鍵設計
--------
1. 在「import app 之前」即把環境變數設為：
       DATABASE_URL = sqlite+aiosqlite:///<tempfile>   (真實檔案，非 :memory:)
       DEV_BOOTSTRAP = 1
   使用「檔案」而非記憶體 DB，是為了讓 app 內多個連線 / 交易共享同一份資料
   (記憶體 sqlite 每連線各自獨立，種子寫入後 login 另開連線會查不到)。

2. conftest.py 於收集階段即 ``from app.main import app``，亦即 app.config.settings
   與 app.database.engine / SessionLocal 早在本模組頂層 env 設定「之前」就已建立，
   且其 DSN 取決於 pytest 啟動時的 DATABASE_URL (CI backend-tests 為 Postgres、
   開發機可能為 :memory:)。為使本測試「不論啟動 DSN 為何」皆穩定走 sqlite 檔，
   ``_rebind_sqlite_engine()`` 會：
     - 將 settings 切到本檔的 sqlite 檔並開啟 dev_bootstrap；
     - 以該 DSN 重建 async engine + sessionmaker，並把所有「在匯入時就以
       ``from app.database import SessionLocal`` 綁定」的模組 (database / deps /
       routers.auth) 之 SessionLocal 一併指向新 sessionmaker。
   之後再 ``with TestClient(app)`` 觸發 lifespan，create_all + 種子才會落在本檔 sqlite。

3. (c) 以 monkeypatch 把 settings.auth_required 暫設為 True，驗證：
       無 Authorization -> 401；帶 Bearer <token> -> 200。
   deps.verify_tenant 於請求時即時讀取 settings.auth_required，故 monkeypatch 生效。

種子 demo 帳號 (密碼 demo1234)：
    admin@tw -> TENT-9981 / TW
    admin@cn -> TENT-CN-002 / CN
"""
from __future__ import annotations

import os
import tempfile

# --------------------------------------------------------------------------- #
# 於匯入 app 之前先設定環境變數 (符合 SPEC：env at top before importing app)。
# 使用具名暫存「檔案」作為 sqlite DB，確保跨連線共享資料且測試可重現。
# --------------------------------------------------------------------------- #
_DB_FD, _DB_PATH = tempfile.mkstemp(prefix="cpm_auth_test_", suffix=".db")
os.close(_DB_FD)
# sqlite URL 以正斜線表示路徑 (Windows 亦適用)。
_DB_URL = "sqlite+aiosqlite:///" + _DB_PATH.replace("\\", "/")
os.environ["DATABASE_URL"] = _DB_URL
os.environ["DEV_BOOTSTRAP"] = "1"
# 預設維持 header mode (個別測試再以 monkeypatch 切換 auth_required)。
os.environ.setdefault("AUTH_REQUIRED", "false")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402

PREFIX = settings.api_v1_prefix
LOGIN_URL = f"{PREFIX}/auth/login"
PROJECTS_URL = f"{PREFIX}/projects"


def _rebind_sqlite_engine() -> None:
    """把 app 的 DB 層強制指向本檔的 sqlite 暫存檔 (不論 conftest 先前綁定何種 DSN)。

    conftest 會在收集階段先 import app，故 engine/SessionLocal 可能早已綁到別的
    DSN。這裡重建 async engine + sessionmaker (與 database.py 的 sqlite 分支一致：
    schema_translate_map 把 erp_integration 映射為 None、check_same_thread=False)，
    並更新所有「以 from app.database import SessionLocal 綁定」的模組參考。
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

    # 關鍵：同步更新 database 的「lazy 快取」(_engine / _SessionLocal)，而不只是
    # public 屬性。lifespan 的 create_all() 與 worker 都透過 get_engine() /
    # get_sessionmaker() 取用 _engine / _SessionLocal；若僅改 public 屬性，當
    # _engine 先前已被建為 postgres (CI 中 lifespan 啟動的 ERP worker 會觸發建立)，
    # create_all() 仍會把表建到 postgres 而非本檔 sqlite，使 login 查 sqlite 時
    # 出現 "no such table: app_users"。
    database._engine = new_engine
    database._SessionLocal = new_sessionmaker
    database.engine = new_engine
    database.SessionLocal = new_sessionmaker

    # 更新已在匯入時綁定 SessionLocal 名稱的模組 (deps / routers.auth)。
    import app.deps as deps
    import app.routers.auth as auth_router_mod

    deps.SessionLocal = new_sessionmaker
    auth_router_mod.SessionLocal = new_sessionmaker


def _dispose_sqlite_engines() -> None:
    """釋放所有綁到本檔 sqlite 暫存檔的 async engine，確保檔案句柄關閉。

    Windows 上若 engine 未 dispose，OS 仍持有 sqlite 檔句柄，後續 os.unlink 會以
    PermissionError ([WinError 32] 檔案使用中) 失敗，於系統暫存目錄留下殘檔。
    此處需處理「兩個」可能開啟同一份 sqlite 檔的 engine：
      1) database.get_engine() —— lifespan 內 create_all() 經 get_engine() 延遲
         建立的全域 _engine。
      2) database.engine —— _rebind_sqlite_engine() 另建並綁進 request/seed
         sessionmaker 的 new_engine (若與 (1) 不同物件)。
    以新事件圈逐一 await dispose()，全程 best-effort (teardown 不應因此失敗)。
    """
    import asyncio

    import app.database as database

    async def _dispose_all() -> None:
        engines = []
        try:
            engines.append(database.get_engine())
        except Exception:
            pass
        # 僅在 rebind「確實」設過 engine 屬性時取用 (避免 PEP 562 __getattr__
        # 回退又建出 lazy engine)；不同物件才追加，避免重複 dispose。
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
    """刪除暫存 sqlite 檔，對 Windows 句柄釋放延遲做短暫重試。

    即使已 dispose engine，aiosqlite 於背景執行緒關閉底層 sqlite3 連線後，OS 釋放
    檔案句柄可能仍有極短延遲；緊接著 os.unlink 偶發 PermissionError
    ([WinError 32])。先 gc 收集殘留的連線/wrapper，再以小間隔重試數次即可穩定刪除。
    """
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
    # 最後再試一次；仍失敗則靜默放棄 (不讓 teardown 失敗)。
    try:
        os.unlink(path)
    except OSError:
        pass


_MISSING = object()

# _rebind_sqlite_engine() 會把「全域」DB 狀態永久切到 sqlite。若不還原，會污染
# 同一個 pytest 行程中後續在「真實 Postgres」上執行的整合測試 (test_integration_db)：
# is_sqlite() 因 settings.database_url 被改成 sqlite 而誤判為 True，使 set_tenant_guc
# 變成 no-op -> RLS 的 app.current_tenant GUC 從未設定 -> INSERT 違反 WITH CHECK
# (new row violates row-level security policy)。故此處「快照 -> 還原」所有被 _rebind
# 與 lifespan 動到的全域綁定，使本檔的 sqlite 改動嚴格侷限於自身 module。
def _snapshot_app_db_state() -> dict:
    """快照 _rebind_sqlite_engine()/lifespan 會變動的全域 DB 綁定。"""
    import app.database as database
    import app.deps as deps
    import app.routers.auth as auth_router_mod

    snap: dict = {
        "database_url": settings.database_url,
        "dev_bootstrap": settings.dev_bootstrap,
        # database 的 lazy 快取 (PEP 562)：get_engine()/get_sessionmaker() 會寫入。
        "_engine": database.__dict__.get("_engine"),
        "_SessionLocal": database.__dict__.get("_SessionLocal"),
        "attrs": {},
    }
    # _rebind 會在這些模組上「設定」public 屬性 (engine / SessionLocal)，
    # 蓋過 database 的 PEP 562 lazy 行為。記錄其原始有無/值以便還原。
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
    # 還原 lazy 快取；下次存取會依還原後的 settings 重新建立正確 (postgres) engine。
    database._engine = snap["_engine"]
    database._SessionLocal = snap["_SessionLocal"]
    for (modname, name), val in snap["attrs"].items():
        mod = importlib.import_module(modname)
        if val is _MISSING:
            mod.__dict__.pop(name, None)  # 原本無此屬性 -> 移除 _rebind 設下的 shadow
        else:
            setattr(mod, name, val)


@pytest.fixture(scope="module")
def client():
    """module 範圍 TestClient；先把 DB 綁到 sqlite 暫存檔，再以 with 進入觸發 lifespan
    (create_all + 種子核心資料 + 種子 app users)。結束後 dispose engine、還原全域 DB
    狀態、再刪暫存檔。

    以 try/finally 包住 _rebind 與 TestClient：即使 _rebind 中途失敗 (例如 CI 缺
    aiosqlite，create_async_engine 於設定 settings.database_url=sqlite 之後才拋錯)，
    finally 仍會把全域狀態還原回 Postgres，確保後續整合測試不被污染。
    """
    snap = _snapshot_app_db_state()
    try:
        _rebind_sqlite_engine()
        with TestClient(app) as c:
            yield c
    finally:
        # 順序重要：先 dispose 綁 sqlite 檔的 engine (Windows 釋放句柄)，再還原全域
        # DB 狀態 (避免污染後續 Postgres 整合測試)，最後重試刪除暫存檔。
        _dispose_sqlite_engines()
        _restore_app_db_state(snap)
        _unlink_with_retry(_DB_PATH)


# --------------------------------------------------------------------------- #
# (a) 正確帳密登入 -> 200 + access_token + tenant_id
# --------------------------------------------------------------------------- #
def test_login_tw_success(client):
    resp = client.post(
        LOGIN_URL, json={"username": "admin@tw", "password": "demo1234"}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["access_token"]
    assert body["token_type"] == "bearer"
    assert body["tenant_id"] == "TENT-9981"
    assert body["region"] == "TW"


# --------------------------------------------------------------------------- #
# (b) 錯誤密碼 -> 401
# --------------------------------------------------------------------------- #
def test_login_wrong_password_401(client):
    resp = client.post(
        LOGIN_URL, json={"username": "admin@tw", "password": "wrong-password"}
    )
    assert resp.status_code == 401, resp.text


# --------------------------------------------------------------------------- #
# (c) auth_required=True 時：無 Bearer -> 401；帶 Bearer -> 200
# --------------------------------------------------------------------------- #
def test_auth_required_gates_protected_endpoints(client, monkeypatch):
    # 暫時開啟強制認證 (test 結束後 monkeypatch 自動還原)。
    monkeypatch.setattr(settings, "auth_required", True)

    # 取得有效 token。
    login = client.post(
        LOGIN_URL, json={"username": "admin@tw", "password": "demo1234"}
    )
    assert login.status_code == 200, login.text
    token = login.json()["access_token"]

    # 無 Authorization -> 401 (即使曾帶 X-Tenant-Id 也不接受 header mode)。
    no_auth = client.get(PROJECTS_URL, headers={"X-Tenant-Id": "TENT-9981"})
    assert no_auth.status_code == 401, no_auth.text

    # 帶 Bearer <token> -> 200。
    with_auth = client.get(
        PROJECTS_URL, headers={"Authorization": f"Bearer {token}"}
    )
    assert with_auth.status_code == 200, with_auth.text
    assert isinstance(with_auth.json(), list)


# --------------------------------------------------------------------------- #
# (d) admin@cn 登入 -> tenant TENT-CN-002 / region CN
# --------------------------------------------------------------------------- #
def test_login_cn_tenant_and_region(client):
    resp = client.post(
        LOGIN_URL, json={"username": "admin@cn", "password": "demo1234"}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["tenant_id"] == "TENT-CN-002"
    assert body["region"] == "CN"
    assert body["access_token"]
