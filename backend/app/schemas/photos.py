"""任務照片附件 schemas (Task photo attachment schemas) —— Pro Batch C FEATURE 1。"""
from __future__ import annotations

from pydantic import BaseModel


class PhotoOut(BaseModel):
    """任務照片附件回應 (POST /photos, GET .../photos 清單共用)。

    url：GET /photos/{id} 的相對路徑 (含 api_v1_prefix)，前端以既有的
    authenticated blob-download 慣例 (Bearer + X-Tenant 攔截器) 取用。
    """

    id: int
    task_id: str
    original_name: str
    content_type: str
    size_bytes: int
    note: str
    uploaded_by: str
    created_at: str
    url: str
