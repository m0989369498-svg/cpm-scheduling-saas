"""Redis 非同步連線輔助（async Redis helpers）。

用途：提供惰性建立的全域 Redis client 與 ping / close 輔助函式
(供啟動時 best-effort 連線檢查等使用)。

Batch 4 (PERF-5)：移除從未被呼叫的甘特快取函式
(get_project_gantt / set_project_gantt / invalidate_project_gantt 及其
cache_get / cache_set / cache_delete 序列化輔助) —— 讀取路徑已改為直接
服務 tasks 表中持久化的 CPM 結果欄位，無需另一層 Redis 快取。

設計原則：
    - 全部以 graceful degradation 處理：Redis 不可用時不應讓呼叫端失敗，
      建立失敗回傳 None、ping 失敗回傳 False。
    - 使用單一惰性建立（lazy）的全域連線池（connection pool）。
"""

from __future__ import annotations

import logging
from typing import Any

try:
    # redis>=4.2 內建 asyncio 介面
    from redis import asyncio as aioredis
except ImportError:  # pragma: no cover - 套件缺失時的防護
    aioredis = None  # type: ignore[assignment]

from app.config import settings

logger = logging.getLogger("cpm.cache")

# 全域 client（惰性建立）
_redis_client: Any | None = None


async def get_redis() -> Any | None:
    """取得（或惰性建立）全域 Redis client。

    任何建立失敗都回傳 None，使呼叫端可優雅降級。
    """
    global _redis_client

    if aioredis is None:
        return None

    if _redis_client is None:
        try:
            _redis_client = aioredis.from_url(
                settings.redis_url,
                encoding="utf-8",
                decode_responses=True,
            )
        except Exception as exc:  # pragma: no cover - 連線設定錯誤
            logger.warning("建立 Redis client 失敗（cache disabled）：%s", exc)
            _redis_client = None

    return _redis_client


async def ping() -> bool:
    """測試 Redis 連線（供 main.py 啟動時 best-effort 呼叫）。"""
    client = await get_redis()
    if client is None:
        return False
    try:
        return bool(await client.ping())
    except Exception as exc:
        logger.warning("Redis ping 失敗：%s", exc)
        return False


async def close_redis() -> None:
    """關閉 Redis 連線（供應用關機時呼叫）。"""
    global _redis_client
    if _redis_client is not None:
        try:
            await _redis_client.aclose()
        except Exception:  # pragma: no cover
            pass
        finally:
            _redis_client = None
