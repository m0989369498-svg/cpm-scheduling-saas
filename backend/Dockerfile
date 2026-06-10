# 後端服務映像檔 (Backend service image)
# 用於 FastAPI 應用程式與 ERP 同步 worker (共用同一映像檔)
FROM python:3.11-slim

# 系統環境設定：不寫入 .pyc、即時輸出 log
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# 安裝 asyncpg / psycopg2 等套件可能需要的系統相依
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先安裝相依，利用 Docker layer cache
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# 複製應用程式原始碼
COPY . .

EXPOSE 8000

# 預設啟動 FastAPI；worker 由 docker-compose 以 command 覆寫為 python -m app.erp.worker
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
