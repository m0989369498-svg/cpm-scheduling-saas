"""應用設定 (Settings) — 使用 pydantic-settings 由環境變數載入。

所有欄位採 snake_case，並與 .env.example / docker-compose.yml 完全對應。
"""
from __future__ import annotations

import logging
from typing import Annotated

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

logger = logging.getLogger("cpm.config")


class Settings(BaseSettings):
    """全域設定。欄位名稱與 SPEC 之 ENV VARS 一一對應。"""

    # --- 資料庫 / 快取 ---
    database_url: str = "postgresql+asyncpg://cpm_app:cpm_app_password@postgres:5432/cpm_saas"
    redis_url: str = "redis://redis:6379/0"

    # --- 應用 ---
    app_env: str = "development"
    api_v1_prefix: str = "/api/v1"

    # CORS 來源：以逗號分隔的字串解析為 list[str]。
    # Annotated[..., NoDecode]：關閉 pydantic-settings 對此複合欄位的「JSON 預解析」。
    # 否則 EnvSettingsSource 會在 validator 之前先對 CORS_ORIGINS 做 json.loads()，
    # 而 docker-compose/.env 給的是逗號分隔字串 (非 JSON)，會 JSONDecodeError →
    # SettingsError，導致後端容器一啟動就崩 (compose-e2e 即因此 502)。加上 NoDecode
    # 後，原始字串會原封不動交給下方 mode="before" 的 _split_cors_origins 自行切分。
    cors_origins: Annotated[list[str], NoDecode] = [
        "http://localhost:5173",
        "http://localhost:8080",
    ]

    # --- 地區 (台灣 TW / 中國大陸 CN) ---
    default_region: str = "TW"

    # --- JWT 認證 (功能旗標；預設關閉以維持既有 header 模式相容) ---
    # jwt_secret：HS256 簽章密鑰 (正式環境務必由 JWT_SECRET 覆寫為 >=32 bytes 隨機值)。
    # 預設值僅供本機開發；長度 >=32 bytes 以避免 PyJWT 弱密鑰警告。
    jwt_secret: str = "dev-only-insecure-secret-DO-NOT-USE-IN-PROD-change-me"  # env JWT_SECRET
    jwt_algorithm: str = "HS256"                      # env JWT_ALGORITHM
    jwt_expire_minutes: int = 720                     # env JWT_EXPIRE_MINUTES
    # auth_required：True => 端點必須帶 Bearer token；False (預設) => 允許 header 模式。
    auth_required: bool = False                       # env AUTH_REQUIRED
    # dev_bootstrap：在「非 sqlite」DB 上也強制 create_all + 種子 (預設關閉)。
    dev_bootstrap: bool = False                       # env DEV_BOOTSTRAP

    # --- 通知 (選填；空值代表 no-op / 僅記錄日誌) ---
    line_channel_access_token: str = ""  # LINE 推播 (台灣)
    dingtalk_webhook_url: str = ""       # 釘釘 webhook (中國大陸)

    # --- ERP 拋轉 worker ---
    erp_scan_interval_seconds: int = 300
    erp_max_retries: int = 5

    # --- ERP 真實端點憑證 (選填；留空 => Adapter 缺端點時走模擬模式) ---------
    # 各家 ERP 的 API 金鑰 / Token 由環境變數注入，絕不寫死於程式碼。
    # SAP (PS 模組)：OData / REST 介面以 Bearer Token 認證
    sap_api_token: str = ""
    # SAP 服務根網址 (選填；某些部署會以 base_url + 相對路徑組出端點，預設不使用)
    sap_base_url: str = ""
    # 鼎新 (DINGXIN_TW)：以自訂 API-Key header 認證
    dingxin_api_key: str = ""
    # 用友 (YONYOU_CN)：以自訂 API-Key header 認證
    yonyou_api_key: str = ""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_cors_origins(cls, v):
        """允許 CORS_ORIGINS 以逗號分隔之字串形式提供 (環境變數常見格式)。"""
        if isinstance(v, str):
            # 去除空白並過濾空項
            return [item.strip() for item in v.split(",") if item.strip()]
        return v

    @model_validator(mode="after")
    def _warn_weak_jwt_secret(self):
        """正式環境若使用弱 / 預設 JWT 密鑰則記錄警告 (不中斷啟動)。"""
        weak_defaults = {
            "dev-secret-change-me",
            "change-me-in-prod",
            "dev-only-insecure-secret-DO-NOT-USE-IN-PROD-change-me",
        }
        if self.app_env.lower() in {"production", "prod"} and (
            len(self.jwt_secret.encode()) < 32 or self.jwt_secret in weak_defaults
        ):
            logger.warning(
                "JWT_SECRET 偏弱 (app_env=%s)；正式環境請改用 >=32 bytes 的高強度隨機值，"
                '例如：python -c "import secrets; print(secrets.token_hex(32))"',
                self.app_env,
            )
        return self


# 單例：整個應用共用同一份設定
settings = Settings()


def is_sqlite() -> bool:
    """目前 DATABASE_URL 是否指向 sqlite (dev 原生模式)。

    sqlite 沒有 RLS / schema / set_config，故多處 (engine 設定、set_tenant_guc、
    啟動 create_all+seed) 皆以此判斷切換行為。
    """
    return (settings.database_url or "").lower().startswith("sqlite")
