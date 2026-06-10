"""本機免 Docker 啟動器 (SQLite Dev Mode launcher).

在「不裝 Docker、不裝 Postgres」的開發機 (例如 Windows-ARM64) 上，
以 sqlite (aiosqlite) 直接跑完整 App：開機自動建表 (create_all)、寫入種子
資料 (兩個租戶 / 範例專案 / app users)，並在 :8000 提供 Swagger。

快速開始 (2 步) — 於 backend/ 目錄下：
    pip install -r requirements-dev.txt
    python run_dev.py

接著開啟：
    http://localhost:8000/docs      Swagger / OpenAPI 文件
    http://localhost:8000/health    健康檢查

預設帳號 (密碼 demo1234)：admin@tw (TENT-9981 / TW)、admin@cn (TENT-CN-002 / CN)。
AUTH_REQUIRED 預設為 false，故仍可僅憑 X-Tenant-Id 標頭存取 (header mode)。

注意：本檔以 os.environ.setdefault 設定預設值 —— 已存在的環境變數不會被覆蓋，
      因此可用 env var 臨時改用 Postgres 或開啟 AUTH_REQUIRED 等。
"""
from __future__ import annotations

import os

# --- 必須在 import app 之前設定環境變數 (config.Settings 於匯入時讀取) ---------
# setdefault：若呼叫者已提供同名 env var，則尊重其值、不覆蓋。
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./cpm_dev.db")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("AUTH_REQUIRED", "false")
os.environ.setdefault("DEV_BOOTSTRAP", "true")


def main() -> None:
    """以 uvicorn 啟動 app.main:app (sqlite dev 模式)。"""
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
    )


if __name__ == "__main__":
    main()
