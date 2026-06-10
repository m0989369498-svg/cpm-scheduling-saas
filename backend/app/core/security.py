"""安全性工具（Security utilities）—— 密碼雜湊與 JWT 簽發 / 驗證。

設計約束（FROZEN）：
  - 密碼雜湊使用 passlib 的 CryptContext，schemes=["pbkdf2_sha256"]。
    刻意「不使用 bcrypt」—— pbkdf2_sha256 為純 Python 實作，可在
    Windows-ARM64 等無 bcrypt wheel 的開發機上直接安裝。
  - JWT 使用 PyJWT（import jwt）以 HS256 簽章，密鑰取自 settings.jwt_secret。

公開 API（其他模組依此契約，請勿變更簽名）：
  hash_password(p) -> str
  verify_password(p, hashed) -> bool
  create_access_token(*, sub, tenant_id, region) -> str
  decode_token(token) -> dict   # 失效 / 過期 -> 拋出例外
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import jwt
from passlib.context import CryptContext

from app.config import settings

# 純 Python 雜湊方案；避免 bcrypt 的原生相依（arm64 無 wheel）。
pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")


def hash_password(p: str) -> str:
    """回傳密碼之 pbkdf2_sha256 雜湊字串（含 salt 與參數）。"""
    return pwd_context.hash(p)


def verify_password(p: str, hashed: str) -> bool:
    """驗證明文密碼是否與雜湊相符。雜湊格式異常時回傳 False。"""
    try:
        return pwd_context.verify(p, hashed)
    except (ValueError, TypeError):
        # 雜湊格式無法辨識（例如空字串 / 損毀）—— 視為驗證失敗。
        return False


def create_access_token(*, sub: str, tenant_id: str, region: str) -> str:
    """簽發存取權杖（access token）。

    claims：
      sub        主體（通常為 username）
      tenant_id  租戶識別碼
      region     地區（TW / CN …）
      exp        過期時間 = 現在 + settings.jwt_expire_minutes 分鐘
    以 HS256 與 settings.jwt_secret 簽章。
    """
    now = datetime.now(timezone.utc)
    claims = {
        "sub": sub,
        "tenant_id": tenant_id,
        "region": region,
        "exp": now + timedelta(minutes=settings.jwt_expire_minutes),
    }
    return jwt.encode(
        claims,
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )


def decode_token(token: str) -> dict:
    """解碼並驗證權杖，回傳 claims dict。

    無效簽章 / 已過期 / 格式錯誤 -> 拋出 jwt 例外（屬 Exception 子類），
    呼叫端（deps.verify_tenant / auth router）應攔截並轉為 401。
    """
    return jwt.decode(
        token,
        settings.jwt_secret,
        algorithms=[settings.jwt_algorithm],
    )
