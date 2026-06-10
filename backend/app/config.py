"""應用設定 (Settings) — 使用 pydantic-settings 由環境變數載入。

所有欄位採 snake_case，並與 .env.example / docker-compose.yml 完全對應。
"""
from __future__ import annotations

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """全域設定。欄位名稱與 SPEC 之 ENV VARS 一一對應。"""

    # --- 資料庫 / 快取 ---
    database_url: str = "postgresql+asyncpg://cpm_app:cpm_app_password@postgres:5432/cpm_saas"
    redis_url: str = "redis://redis:6379/0"

    # --- 應用 ---
    app_env: str = "development"
    api_v1_prefix: str = "/api/v1"

    # CORS 來源：以逗號分隔的字串解析為 list[str]
    cors_origins: list[str] = ["http://localhost:5173", "http://localhost:8080"]

    # --- 地區 (台灣 TW / 中國大陸 CN) ---
    default_region: str = "TW"

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


# 單例：整個應用共用同一份設定
settings = Settings()
