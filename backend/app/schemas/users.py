"""使用者管理相關的 Pydantic v2 結構定義（Users schemas / DTO）。

供 admin-only 的使用者 CRUD（app/routers/users.py）使用：

  UserOut     使用者輸出（「不」含 password_hash —— 絕不外洩雜湊）。
  UserCreate  建立使用者（username + password + role；region 選填）。
  UserUpdate  更新使用者（role / is_active / password 皆選填，部分更新）。

角色（role）：admin > editor > viewer。
  此處以 Literal 限定合法值，於 schema 層即擋下非法角色（422），
  與 app.deps.ROLE_ORDER 一致。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# 合法角色字面值（與 app.deps.ROLE_ORDER 的 key 一致）。
RoleLiteral = Literal["viewer", "editor", "admin"]


class UserOut(BaseModel):
    """使用者輸出 DTO。

    刻意「不」包含 password_hash —— 任何使用者列表 / 詳情皆不外洩密碼雜湊。
    model_config(from_attributes=True) 使其可直接由 ORM AppUser 實例序列化。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    tenant_id: str
    username: str
    role: str
    region: str
    is_active: bool
    created_at: datetime | None = None


class UserCreate(BaseModel):
    """建立使用者請求。

    username  租戶內（實務上全域）唯一；重複時端點回 409。
    password  明文密碼，端點以 hash_password 雜湊後存入（絕不存明文）。
    role      admin / editor / viewer 之一。
    region    選填；未提供時由端點以 ctx.region 補上（租戶預設地區）。
    """

    username: str = Field(min_length=1, max_length=150)
    password: str = Field(min_length=1, max_length=255)
    role: RoleLiteral
    region: str | None = Field(default=None, max_length=20)


class UserUpdate(BaseModel):
    """更新使用者請求（部分更新；未提供之欄位保持不變）。

    role       選填，變更角色。
    is_active  選填，啟用 / 停用帳號。
    password   選填，重設密碼（端點以 hash_password 雜湊後存入）。
    """

    role: RoleLiteral | None = None
    is_active: bool | None = None
    password: str | None = Field(default=None, min_length=1, max_length=255)
