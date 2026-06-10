"""認證相關的 Pydantic v2 結構定義（Auth schemas / DTO）。

契約（FROZEN）—— 前端登入流程與 routers/auth.py 皆依此：
  LoginRequest   登入請求（帳號 + 密碼）
  TokenResponse  登入成功回應（Bearer 權杖 + 租戶 / 地區）
  MeResponse     目前身分（GET /auth/me）
"""

from __future__ import annotations

from pydantic import BaseModel


class LoginRequest(BaseModel):
    """登入請求。"""

    username: str
    password: str


class TokenResponse(BaseModel):
    """登入成功回應；token_type 固定為 "bearer"。"""

    access_token: str
    token_type: str = "bearer"
    tenant_id: str
    region: str
    # 角色（admin / editor / viewer）；舊用戶或缺欄位時預設 admin，向後相容。
    role: str = "admin"


class MeResponse(BaseModel):
    """目前已驗證身分（由 TenantContext 解析而來）。"""

    username: str
    tenant_id: str
    region: str
    # 角色（admin / editor / viewer）；header/dev 模式為 admin。
    role: str = "admin"
