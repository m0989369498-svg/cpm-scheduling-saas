"""任務照片附件路由 (Task photos router) —— Pro Batch C FEATURE 1 (mobile field reporting)。

職責：
  POST   /projects/{project_id}/tasks/{task_id}/photos  上傳照片 (editor+，multipart)。
  GET    /projects/{project_id}/tasks/{task_id}/photos  列出任務照片 (viewer 亦可)。
  GET    /photos/{photo_id}                              取回照片檔案 (viewer 亦可)。
  DELETE /photos/{photo_id}                               刪除照片 (editor+)。

設計重點：
  * 實際檔案內容存於磁碟 (settings.upload_dir)，DB 只存中繼資料；stored_name
    為伺服器產生的 uuid4hex 檔名 (絕不取自使用者輸入的檔名，避免路徑穿越 /
    覆寫既有檔案)。
  * 上傳驗證順序：(1) 大小上限 MAX_PHOTO_BYTES (超過 -> 413，串流讀取途中
    即中止，不整檔載入記憶體)；(2) 型別以「檔案內容 magic bytes」判定
    (jpeg/png/webp)，忽略用戶端聲稱的 Content-Type -> 不支援 -> 415。
  * 寫入磁碟 / 刪除磁碟檔皆以 anyio.to_thread 執行，避免阻塞 event loop。
  * 租戶隔離：PostgreSQL 由 RLS 強制；sqlite (dev，無 RLS) 額外以
    TaskPhoto.tenant_id == ctx.tenant_id 在查詢層過濾 (get_photo /
    project 校驗)，兩種後端行為一致。
  * 重用 app.routers.projects._get_project_or_404 / app.routers.tasks._get_task_or_404
    驗證專案與任務存在，不重複實作。
  * 稽核 (best-effort)：PHOTO_UPLOAD / PHOTO_DELETE，失敗僅記錄、不中斷主要操作。
"""

from __future__ import annotations

import functools
import logging
import os
import uuid

import anyio
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core import audit
from app.deps import TenantContext, get_db, require_role, verify_tenant
from app.models.orm import TaskPhoto
from app.routers.projects import _get_project_or_404
from app.routers.tasks import _get_task_or_404
from app.schemas.photos import PhotoOut

logger = logging.getLogger("cpm.routers.photos")

router = APIRouter(tags=["photos"])

# 單張照片大小上限 (bytes)；測試以 monkeypatch 覆寫模擬超量 (413)。
MAX_PHOTO_BYTES = 5 * 1024 * 1024  # 5 MB

# magic bytes -> (content_type, 副檔名)。stored_name 之副檔名一律取自此表，
# 絕不取自使用者上傳的檔名 / 聲稱的 content-type。
_JPEG_MAGIC = b"\xff\xd8\xff"
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


# ---------------------------------------------------------------------------
# 內部工具
# ---------------------------------------------------------------------------
async def _read_upload_capped(file: UploadFile) -> bytes:
    """串流讀取上傳檔案，超過 MAX_PHOTO_BYTES 立即 413 (不整檔載入記憶體)。

    以模組層級全域 MAX_PHOTO_BYTES 於「呼叫當下」查值 (而非綁為預設參數)，
    使測試以 monkeypatch.setattr(photos, "MAX_PHOTO_BYTES", ...) 覆寫生效。
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_PHOTO_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"Photo exceeds size limit ({MAX_PHOTO_BYTES} bytes)",
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _sniff_image_type(data: bytes) -> tuple[str, str] | None:
    """依「檔案內容」magic bytes 判斷圖片型別，回傳 (content_type, ext) 或 None。

    刻意忽略用戶端聲稱的 Content-Type / 檔名副檔名 —— 僅信任位元組本身：
      JPEG : FF D8 FF
      PNG  : 89 50 4E 47 0D 0A 1A 0A
      WEBP : RIFF????WEBP (RIFF 容器，第 8-11 bytes 為 'WEBP')
    """
    if data[:3] == _JPEG_MAGIC:
        return "image/jpeg", "jpg"
    if data[:8] == _PNG_MAGIC:
        return "image/png", "png"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", "webp"
    return None


def _write_photo_file(directory: str, stored_name: str, data: bytes) -> str:
    """同步寫入檔案 (於 anyio.to_thread 執行)；目錄不存在時建立 (on demand)。"""
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, stored_name)
    with open(path, "wb") as fh:
        fh.write(data)
    return path


def _unlink_best_effort(path: str) -> None:
    """同步刪除檔案 (於 anyio.to_thread 執行)；不存在 / 失敗皆忽略。"""
    try:
        os.remove(path)
    except OSError:
        pass


def _photo_path(photo: TaskPhoto) -> str:
    """組出照片在磁碟上的路徑（含防禦性檢查）。

    stored_name 一律為伺服器產生的 uuid4hex 檔名；若 DB 內容遭竄改（含路徑
    分隔符 / '..' / 空值），拒絕 join 出 upload_dir 之外的路徑，直接視為
    不存在（404），杜絕任意檔案讀取 / 刪除。
    """
    name = photo.stored_name or ""
    if not name or os.path.basename(name) != name or name in {".", ".."}:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Photo file missing on disk for id '{photo.id}'",
        )
    return os.path.join(settings.upload_dir, name)


def _photo_to_schema(row: TaskPhoto) -> PhotoOut:
    return PhotoOut(
        id=int(row.id),
        task_id=row.task_id,
        original_name=row.original_name or "",
        content_type=row.content_type,
        size_bytes=int(row.size_bytes or 0),
        note=row.note or "",
        uploaded_by=row.uploaded_by or "",
        created_at=row.created_at.isoformat() if row.created_at is not None else "",
        url=f"{settings.api_v1_prefix}/photos/{int(row.id)}",
    )


async def _get_photo_or_404(
    db: AsyncSession, photo_id: int, tenant_id: str
) -> TaskPhoto:
    """取得指定 id 的照片，找不到 (含跨租戶) 回 404。

    tenant_id 過濾為「應用層雙保險」：PostgreSQL 由 RLS 強制隔離；sqlite
    (dev，無 RLS) 靠此條件確保跨租戶查詢一律 404，兩種後端行為一致。
    """
    result = await db.execute(
        select(TaskPhoto).where(
            TaskPhoto.id == photo_id,
            TaskPhoto.tenant_id == tenant_id,
        )
    )
    photo = result.scalar_one_or_none()
    if photo is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Photo '{photo_id}' not found",
        )
    return photo


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@router.post(
    "/projects/{project_id}/tasks/{task_id}/photos",
    response_model=PhotoOut,
    status_code=status.HTTP_201_CREATED,
)
async def upload_task_photo(
    project_id: str,
    task_id: str,
    file: UploadFile = File(...),
    # max_length 對應 DB 欄位 VARCHAR(500)：Postgres 會對超長值直接報錯 (500)，
    # sqlite 不強制長度 —— 在應用層先驗證 (422) 讓兩種後端行為一致。
    note: str = Form("", max_length=500),
    ctx: TenantContext = Depends(verify_tenant),
    db: AsyncSession = Depends(get_db),
    _role: None = Depends(require_role("editor")),
) -> PhotoOut:
    """上傳任務照片 (editor+)。

    驗證順序：專案/任務存在 (404) -> 大小上限 (413) -> magic bytes 型別 (415)。
    成功後寫入磁碟 (uuid4hex 檔名) 並插入中繼資料列。
    """
    await _get_project_or_404(db, project_id, ctx.tenant_id)
    await _get_task_or_404(db, project_id, task_id)

    raw = await _read_upload_capped(file)

    sniffed = _sniff_image_type(raw)
    if sniffed is None:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=(
                "Unsupported image type (only jpeg/png/webp accepted, "
                "detected by file content magic bytes)"
            ),
        )
    content_type, ext = sniffed
    stored_name = f"{uuid.uuid4().hex}.{ext}"

    await anyio.to_thread.run_sync(
        functools.partial(_write_photo_file, settings.upload_dir, stored_name, raw)
    )

    photo = TaskPhoto(
        project_id=project_id,
        tenant_id=ctx.tenant_id,
        task_id=task_id,
        stored_name=stored_name,
        # 截斷至 DB 欄位 VARCHAR(255)：超長的用戶端檔名在 Postgres 會使
        # INSERT 直接報錯 (sqlite 則不會)，先截斷讓兩種後端行為一致。
        original_name=(file.filename or "")[:255],
        content_type=content_type,
        size_bytes=len(raw),
        note=note or "",
        uploaded_by=(ctx.sub or ctx.tenant_id),
    )
    db.add(photo)
    await db.flush()
    await db.refresh(photo)

    # 稽核 (best-effort): 失敗僅記錄, 絕不中斷主要操作。
    try:
        await audit.log_action(
            db,
            ctx,
            "PHOTO_UPLOAD",
            {
                "project_id": project_id,
                "task_id": task_id,
                "photo_id": int(photo.id),
                "original_name": photo.original_name,
                "content_type": photo.content_type,
                "size_bytes": photo.size_bytes,
            },
        )
    except Exception as exc:  # noqa: BLE001 - 稽核失敗不可中斷主要操作
        logger.warning("audit PHOTO_UPLOAD failed (ignored): %s", exc)

    return _photo_to_schema(photo)


@router.get(
    "/projects/{project_id}/tasks/{task_id}/photos",
    response_model=list[PhotoOut],
)
async def list_task_photos(
    project_id: str,
    task_id: str,
    ctx: TenantContext = Depends(verify_tenant),
    db: AsyncSession = Depends(get_db),
) -> list[PhotoOut]:
    """列出任務所有照片 (viewer 亦可)，依上傳時間排序。"""
    await _get_project_or_404(db, project_id, ctx.tenant_id)
    await _get_task_or_404(db, project_id, task_id)

    result = await db.execute(
        select(TaskPhoto)
        .where(TaskPhoto.project_id == project_id, TaskPhoto.task_id == task_id)
        .order_by(TaskPhoto.created_at, TaskPhoto.id)
    )
    rows = list(result.scalars().all())
    return [_photo_to_schema(r) for r in rows]


@router.get("/photos/{photo_id}")
async def get_photo_file(
    photo_id: int,
    ctx: TenantContext = Depends(verify_tenant),
    db: AsyncSession = Depends(get_db),
) -> FileResponse:
    """取回照片檔案 (viewer 亦可)。RLS + tenant_id 過濾使跨租戶 id 一律 404。"""
    photo = await _get_photo_or_404(db, photo_id, ctx.tenant_id)
    path = _photo_path(photo)
    if not os.path.isfile(path):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Photo file missing on disk for id '{photo_id}'",
        )
    return FileResponse(path, media_type=photo.content_type)


@router.delete("/photos/{photo_id}")
async def delete_photo(
    photo_id: int,
    ctx: TenantContext = Depends(verify_tenant),
    db: AsyncSession = Depends(get_db),
    _role: None = Depends(require_role("editor")),
) -> dict:
    """刪除照片 (editor+)：移除中繼資料列並盡力刪除磁碟檔。"""
    photo = await _get_photo_or_404(db, photo_id, ctx.tenant_id)
    path = _photo_path(photo)
    project_id = photo.project_id
    task_id = photo.task_id

    await db.delete(photo)
    await db.flush()

    await anyio.to_thread.run_sync(functools.partial(_unlink_best_effort, path))

    # 稽核 (best-effort): 失敗僅記錄, 絕不中斷主要操作。
    try:
        await audit.log_action(
            db,
            ctx,
            "PHOTO_DELETE",
            {"project_id": project_id, "task_id": task_id, "photo_id": photo_id},
        )
    except Exception as exc:  # noqa: BLE001 - 稽核失敗不可中斷主要操作
        logger.warning("audit PHOTO_DELETE failed (ignored): %s", exc)

    return {"ok": True}
