"""Pro Batch C 行動工地回報（field reporting）測試 —— 任務照片附件 + QR 深連結。

在 sqlite dev 模式下執行 (不需 Postgres)，與 test_auth.py / test_interop.py 同法。

範圍（依指派契約 —— 僅涵蓋 BACKEND TESTS）：
  (a) 上傳 jpeg（magic bytes FF D8 FF）-> 201 PhotoOut；檔案確實寫入
      settings.upload_dir（本檔以環境變數指向的暫存目錄）；list -> 1 筆；
      GET /photos/{id} -> 200 image/jpeg。
  (b) png / webp 皆可接受（各自的 magic bytes）；exe（MZ）/ gif（GIF8）
      內容 -> 415（無論 client 宣稱的 content-type 為何）；超過 5MB 上限
      -> 413。
  (c) viewer 角色可 GET 清單，但 POST 上傳 -> 403。
  (d) DELETE 移除資料列與檔案（best-effort unlink）。
  (e) 跨租戶：admin@cn 對 TW 專案底下的 photo id 直接 GET /photos/{id}
      -> 404（RLS 依 tenant GUC 過濾列）。
  (f) GET /projects/{pid}/tasks/{tid}/qr.png -> 200 image/png，內容以
      PNG magic bytes \\x89PNG 開頭。

關鍵設計（與 test_auth.py / test_interop.py 完全一致）：
  1. 「import app 之前」即設定：
         DATABASE_URL = sqlite+aiosqlite:///<tempfile>  (真實檔案，非 :memory:)
         DEV_BOOTSTRAP = 1
         UPLOAD_DIR    = <本檔專用暫存目錄>   (SPEC：settings.upload_dir，env UPLOAD_DIR)
     UPLOAD_DIR 亦須在 import app 之前設定，確保 settings 單例讀到本檔專屬路徑，
     不與其他測試檔或開發機的 ./uploads 混用。
  2. client fixture 以 _rebind_sqlite_engine() 將整個 DB 層重綁至本檔 sqlite 檔，
     teardown 時 dispose engine、還原全域綁定、再刪暫存檔（Windows 句柄安全）。
  3. 上傳超過 5MB 上限的測試直接組出真正 > 5MB 的位元組內容（jpeg 合法表頭 +
     填充），不耦合實作內部「上限常數」之符號名稱（與 test_interop.py 對
     10MB XER 上限的作法一致）。
  4. 圖片/QR magic bytes 判讀：僅檢查各格式定義性的表頭位元組，不依賴
     PIL / Pillow 等重量依賴（SPEC 明確排除縮圖功能，僅需最陽春的 sniff）。

種子 demo 帳號 (密碼 demo1234；見 app.main._seed_initial_admin 種子清單)：
    admin@tw  -> TENT-9981    / TW   (admin)
    admin@cn  -> TENT-CN-002  / CN   (admin)
    editor@tw -> TENT-9981    / TW   (editor)
    viewer@tw -> TENT-9981    / TW   (viewer)
"""
from __future__ import annotations

import os
import shutil
import tempfile

# --------------------------------------------------------------------------- #
# 於匯入 app 之前先設定環境變數 (env at top before importing app)。
# 使用具名暫存「檔案」作為 sqlite DB（跨連線共享資料），並另闢一個暫存
# 「目錄」作為本檔專屬的 UPLOAD_DIR（避免與其他測試檔或開發機 ./uploads 混用）。
# --------------------------------------------------------------------------- #
_DB_FD, _DB_PATH = tempfile.mkstemp(prefix="cpm_field_reporting_test_", suffix=".db")
os.close(_DB_FD)
_DB_URL = "sqlite+aiosqlite:///" + _DB_PATH.replace("\\", "/")
os.environ["DATABASE_URL"] = _DB_URL
os.environ["DEV_BOOTSTRAP"] = "1"
os.environ.setdefault("AUTH_REQUIRED", "false")

_UPLOAD_DIR = tempfile.mkdtemp(prefix="cpm_field_reporting_uploads_")
os.environ["UPLOAD_DIR"] = _UPLOAD_DIR

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402

PREFIX = settings.api_v1_prefix
LOGIN_URL = f"{PREFIX}/auth/login"
PROJECTS_URL = f"{PREFIX}/projects"
PHOTOS_URL = f"{PREFIX}/photos"

DEMO_PASSWORD = "demo1234"

# --- Magic-byte 樣本（各格式定義性表頭 + 填充，確保 size 位於合理範圍內）---
_JPEG_BYTES = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01" + b"\x00" * 200
_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 200
# WEBP：RIFF <4-byte size> WEBP ...；判讀僅需 bytes[0:4]=="RIFF" 且 bytes[8:12]=="WEBP"。
_WEBP_BYTES = b"RIFF" + b"\x24\x00\x00\x00" + b"WEBPVP8 " + b"\x00" * 200
# 非法內容：exe（MZ 表頭）/ gif（GIF89a 表頭）—— 皆非契約允許的三種格式。
_EXE_BYTES = b"MZ\x90\x00\x03\x00\x00\x00" + b"\x00" * 200
_GIF_BYTES = b"GIF89a" + b"\x00" * 200


# --------------------------------------------------------------------------- #
# sqlite rebind / teardown 工具（與 test_auth.py / test_interop.py 同法）
# --------------------------------------------------------------------------- #
def _rebind_sqlite_engine() -> None:
    """把 app 的 DB 層強制指向本檔的 sqlite 暫存檔。"""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.ext.asyncio import AsyncSession

    import app.database as database

    settings.database_url = _DB_URL
    settings.dev_bootstrap = True
    # settings 為模組載入時建立的單例；tests/conftest.py 在 pytest collection
    # 階段就先 `from app.config import settings`，早於本檔頂部設定
    # os.environ["UPLOAD_DIR"] 的時間點，故 Settings() 建構時讀到的仍是預設值
    # ("./uploads")。與 database_url/dev_bootstrap 同法：直接改寫已建立的
    # settings 單例屬性，確保 app.routers.photos 讀到的是本檔專屬 UPLOAD_DIR。
    settings.upload_dir = _UPLOAD_DIR

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
        "upload_dir": settings.upload_dir,
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
    settings.upload_dir = snap["upload_dir"]
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
    (create_all + 種子核心資料 + 種子 app users)。結束後 dispose、還原全域狀態、
    刪 sqlite 暫存檔與 UPLOAD_DIR 暫存目錄。
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
        shutil.rmtree(_UPLOAD_DIR, ignore_errors=True)


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


def _admin_cn_headers(client: TestClient) -> dict[str, str]:
    return _headers_for(client, "admin@cn")


def _editor_headers(client: TestClient) -> dict[str, str]:
    return _headers_for(client, "editor@tw")


def _viewer_headers(client: TestClient) -> dict[str, str]:
    return _headers_for(client, "viewer@tw")


# --------------------------------------------------------------------------- #
# 專案 / 任務建立小工具
# --------------------------------------------------------------------------- #
def _create_project_with_task(
    client: TestClient, headers: dict, pid: str, task_id: str = "T-01"
) -> None:
    resp = client.post(
        PROJECTS_URL,
        headers=headers,
        json={
            "project_id": pid,
            "project_name": f"工地回報測試 {pid}",
            "region": "TW",
            "schedule_data": [
                {
                    "task_id": task_id,
                    "task_name": "基礎開挖",
                    "duration": 5,
                    "predecessors": [],
                    "status": "PENDING",
                }
            ],
        },
    )
    assert resp.status_code == 201, resp.text


def _photos_url(pid: str, tid: str) -> str:
    return f"{PROJECTS_URL}/{pid}/tasks/{tid}/photos"


def _qr_url(pid: str, tid: str) -> str:
    return f"{PROJECTS_URL}/{pid}/tasks/{tid}/qr.png"


# =============================================================================
# (a) 上傳 jpeg -> 201 PhotoOut；檔案落地於 UPLOAD_DIR；list -> 1；GET -> 200
# =============================================================================
def test_upload_jpeg_photo_201_persists_file_and_listable(client):
    headers = _editor_headers(client)
    pid = "PRJ-FIELD-JPEG"
    _create_project_with_task(client, headers, pid, task_id="T-01")

    # 上傳前：本檔專屬 UPLOAD_DIR 應為空（避免與其他測試互相污染判斷檔案落地）。
    before_files = set(os.listdir(_UPLOAD_DIR))

    resp = client.post(
        _photos_url(pid, "T-01"),
        headers=headers,
        files={"file": ("site.jpg", _JPEG_BYTES, "image/jpeg")},
        data={"note": "地基已完成澆置"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()

    assert body["task_id"] == "T-01"
    assert body["original_name"] == "site.jpg"
    assert body["content_type"] == "image/jpeg"
    assert body["size_bytes"] == len(_JPEG_BYTES)
    assert body["note"] == "地基已完成澆置"
    assert body["uploaded_by"]
    assert body["created_at"]
    assert body["url"]
    photo_id = body["id"]

    # 檔案確實寫入 settings.upload_dir（不得使用使用者提供的原始檔名）。
    after_files = set(os.listdir(_UPLOAD_DIR))
    new_files = after_files - before_files
    assert len(new_files) == 1
    stored_name = next(iter(new_files))
    assert stored_name != "site.jpg"  # 儲存檔名絕不可為使用者輸入原樣。
    stored_path = os.path.join(_UPLOAD_DIR, stored_name)
    with open(stored_path, "rb") as f:
        assert f.read() == _JPEG_BYTES

    # list -> 1 筆。
    list_resp = client.get(_photos_url(pid, "T-01"), headers=headers)
    assert list_resp.status_code == 200, list_resp.text
    items = list_resp.json()
    assert len(items) == 1
    assert items[0]["id"] == photo_id

    # GET /photos/{id} -> 200 image/jpeg，內容與上傳一致。
    get_resp = client.get(f"{PHOTOS_URL}/{photo_id}", headers=headers)
    assert get_resp.status_code == 200, get_resp.text
    assert "image/jpeg" in get_resp.headers.get("content-type", "")
    assert get_resp.content == _JPEG_BYTES


def test_upload_photo_404_for_nonexistent_task(client):
    headers = _editor_headers(client)
    pid = "PRJ-FIELD-404TASK"
    _create_project_with_task(client, headers, pid, task_id="T-01")

    resp = client.post(
        _photos_url(pid, "NO-SUCH-TASK"),
        headers=headers,
        files={"file": ("site.jpg", _JPEG_BYTES, "image/jpeg")},
    )
    assert resp.status_code == 404, resp.text


# =============================================================================
# (b) png / webp 接受；exe / gif -> 415；超過 5MB -> 413
# =============================================================================
def test_upload_png_and_webp_accepted(client):
    headers = _editor_headers(client)
    pid = "PRJ-FIELD-PNGWEBP"
    _create_project_with_task(client, headers, pid, task_id="T-01")

    png_resp = client.post(
        _photos_url(pid, "T-01"),
        headers=headers,
        # 刻意宣稱錯誤的 client content-type：伺服器應以 magic bytes 判讀，
        # 而非信任 client 宣稱的型態。
        files={"file": ("a.png", _PNG_BYTES, "application/octet-stream")},
    )
    assert png_resp.status_code == 201, png_resp.text
    assert png_resp.json()["content_type"] == "image/png"

    webp_resp = client.post(
        _photos_url(pid, "T-01"),
        headers=headers,
        files={"file": ("b.webp", _WEBP_BYTES, "application/octet-stream")},
    )
    assert webp_resp.status_code == 201, webp_resp.text
    assert webp_resp.json()["content_type"] == "image/webp"


def test_upload_non_image_magic_bytes_rejected_415(client):
    headers = _editor_headers(client)
    pid = "PRJ-FIELD-415"
    _create_project_with_task(client, headers, pid, task_id="T-01")

    # exe 內容，即使 client 謊稱 image/jpeg，伺服器仍須以 magic bytes 判讀拒絕。
    exe_resp = client.post(
        _photos_url(pid, "T-01"),
        headers=headers,
        files={"file": ("virus.exe", _EXE_BYTES, "image/jpeg")},
    )
    assert exe_resp.status_code == 415, exe_resp.text

    gif_resp = client.post(
        _photos_url(pid, "T-01"),
        headers=headers,
        files={"file": ("anim.gif", _GIF_BYTES, "image/gif")},
    )
    assert gif_resp.status_code == 415, gif_resp.text


def test_upload_oversized_file_413(client):
    headers = _editor_headers(client)
    pid = "PRJ-FIELD-413"
    _create_project_with_task(client, headers, pid, task_id="T-01")

    # 真正超過 5MB 上限的內容（合法 jpeg 表頭 + 大量填充），不耦合實作內部
    # 「上限常數」之符號名稱（與 test_interop.py 對 10MB XER 上限的作法一致）。
    oversized = _JPEG_BYTES + b"\x00" * (5 * 1024 * 1024 + 1024)
    resp = client.post(
        _photos_url(pid, "T-01"),
        headers=headers,
        files={"file": ("huge.jpg", oversized, "image/jpeg")},
    )
    assert resp.status_code == 413, resp.text


# =============================================================================
# (c) viewer 可 GET 清單，但 POST 上傳 -> 403
# =============================================================================
def test_viewer_can_list_but_cannot_upload(client):
    editor_headers = _editor_headers(client)
    pid = "PRJ-FIELD-VIEWER"
    _create_project_with_task(client, editor_headers, pid, task_id="T-01")

    viewer_headers = _viewer_headers(client)

    list_resp = client.get(_photos_url(pid, "T-01"), headers=viewer_headers)
    assert list_resp.status_code == 200, list_resp.text
    assert list_resp.json() == []

    upload_resp = client.post(
        _photos_url(pid, "T-01"),
        headers=viewer_headers,
        files={"file": ("site.jpg", _JPEG_BYTES, "image/jpeg")},
    )
    assert upload_resp.status_code == 403, upload_resp.text


# =============================================================================
# (d) DELETE 移除資料列與檔案
# =============================================================================
def test_delete_photo_removes_row_and_file(client):
    headers = _editor_headers(client)
    pid = "PRJ-FIELD-DELETE"
    _create_project_with_task(client, headers, pid, task_id="T-01")

    upload_resp = client.post(
        _photos_url(pid, "T-01"),
        headers=headers,
        files={"file": ("site.jpg", _JPEG_BYTES, "image/jpeg")},
    )
    assert upload_resp.status_code == 201, upload_resp.text
    photo_id = upload_resp.json()["id"]

    before_files = set(os.listdir(_UPLOAD_DIR))

    del_resp = client.delete(f"{PHOTOS_URL}/{photo_id}", headers=headers)
    assert del_resp.status_code in (200, 204), del_resp.text

    # 資料列已移除：GET -> 404，清單亦不再包含。
    get_resp = client.get(f"{PHOTOS_URL}/{photo_id}", headers=headers)
    assert get_resp.status_code == 404, get_resp.text

    list_resp = client.get(_photos_url(pid, "T-01"), headers=headers)
    assert list_resp.status_code == 200, list_resp.text
    assert all(item["id"] != photo_id for item in list_resp.json())

    # 檔案亦已（best-effort）unlink：UPLOAD_DIR 內對應檔案不再存在。
    after_files = set(os.listdir(_UPLOAD_DIR))
    assert after_files < before_files or len(after_files) < len(before_files)


def test_delete_photo_requires_editor_role_403_for_viewer(client):
    editor_headers = _editor_headers(client)
    pid = "PRJ-FIELD-DELROLE"
    _create_project_with_task(client, editor_headers, pid, task_id="T-01")

    upload_resp = client.post(
        _photos_url(pid, "T-01"),
        headers=editor_headers,
        files={"file": ("site.jpg", _JPEG_BYTES, "image/jpeg")},
    )
    assert upload_resp.status_code == 201, upload_resp.text
    photo_id = upload_resp.json()["id"]

    viewer_headers = _viewer_headers(client)
    del_resp = client.delete(f"{PHOTOS_URL}/{photo_id}", headers=viewer_headers)
    assert del_resp.status_code == 403, del_resp.text


# =============================================================================
# (e) 跨租戶：admin@cn 對 TW 專案的 photo id -> 404（RLS）
# =============================================================================
def test_cross_tenant_get_photo_404_rls(client):
    tw_headers = _admin_headers(client)
    pid = "PRJ-FIELD-RLS"
    _create_project_with_task(client, tw_headers, pid, task_id="T-01")

    upload_resp = client.post(
        _photos_url(pid, "T-01"),
        headers=tw_headers,
        files={"file": ("site.jpg", _JPEG_BYTES, "image/jpeg")},
    )
    assert upload_resp.status_code == 201, upload_resp.text
    photo_id = upload_resp.json()["id"]

    # 同租戶 (TW) 可正常取得。
    same_tenant_resp = client.get(f"{PHOTOS_URL}/{photo_id}", headers=tw_headers)
    assert same_tenant_resp.status_code == 200, same_tenant_resp.text

    # 跨租戶 (CN admin) 依 RLS 過濾 -> 404（不得洩漏「存在但無權限」的區別）。
    cn_headers = _admin_cn_headers(client)
    cross_tenant_resp = client.get(f"{PHOTOS_URL}/{photo_id}", headers=cn_headers)
    assert cross_tenant_resp.status_code == 404, cross_tenant_resp.text


# =============================================================================
# (f) QR 深連結：GET /projects/{pid}/tasks/{tid}/qr.png -> 200 image/png
# =============================================================================
def test_qr_png_endpoint_returns_png_magic_bytes(client):
    headers = _editor_headers(client)
    pid = "PRJ-FIELD-QR"
    _create_project_with_task(client, headers, pid, task_id="T-01")

    # viewer 亦可讀取（唯讀端點）。
    viewer_headers = _viewer_headers(client)
    resp = client.get(_qr_url(pid, "T-01"), headers=viewer_headers)
    assert resp.status_code == 200, resp.text
    assert "image/png" in resp.headers.get("content-type", "")
    assert resp.content.startswith(b"\x89PNG")


def test_qr_png_404_for_nonexistent_task(client):
    headers = _editor_headers(client)
    pid = "PRJ-FIELD-QR404"
    _create_project_with_task(client, headers, pid, task_id="T-01")

    resp = client.get(_qr_url(pid, "NO-SUCH-TASK"), headers=headers)
    assert resp.status_code == 404, resp.text
