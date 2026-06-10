"""Redis 非同步快取層（async cache）。

用途：快取每個專案的甘特 / CPM 計算結果，降低重複運算與 DB 負載。

設計原則：
    - 全部以 graceful degradation 處理：Redis 不可用時不應讓 API 失敗，
      讀取回傳 None、寫入靜默忽略（best-effort），由呼叫端 fallback 重算。
    - 使用單一惰性建立（lazy）的全域連線池（connection pool）。
"""

from __future__ import annotations

import json
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

# 預設 TTL（秒）：甘特快取 1 小時
DEFAULT_TTL_SECONDS = 3600


def _project_key(tenant_id: str, project_id: str) -> str:
    """組出每租戶、每專案的甘特快取鍵（避免跨租戶汙染）。"""
    return f"gantt:{tenant_id}:{project_id}"


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


async def cache_get(key: str) -> Any | None:
    """讀取並反序列化（JSON）；失敗或不存在回傳 None。"""
    client = await get_redis()
    if client is None:
        return None
    try:
        raw = await client.get(key)
    except Exception as exc:
        logger.warning("Redis GET 失敗 key=%s：%s", key, exc)
        return None
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


async def cache_set(key: str, value: Any, ttl: int = DEFAULT_TTL_SECONDS) -> bool:
    """序列化（JSON）後寫入並設定 TTL；任何失敗回傳 False（不拋例外）。"""
    client = await get_redis()
    if client is None:
        return False
    try:
        payload = json.dumps(value, ensure_ascii=False, default=str)
        await client.set(key, payload, ex=ttl)
        return True
    except Exception as exc:
        logger.warning("Redis SET 失敗 key=%s：%s", key, exc)
        return False


async def cache_delete(key: str) -> bool:
    """刪除指定鍵；失敗回傳 False。"""
    client = await get_redis()
    if client is None:
        return False
    try:
        await client.delete(key)
        return True
    except Exception as exc:
        logger.warning("Redis DEL 失敗 key=%s：%s", key, exc)
        return False


# ---- 專案甘特快取的高階輔助函式 ----


async def get_project_gantt(tenant_id: str, project_id: str) -> Any | None:
    """讀取某專案的甘特 / CPM 快取結果。"""
    return await cache_get(_project_key(tenant_id, project_id))


async def set_project_gantt(
    tenant_id: str,
    project_id: str,
    data: Any,
    ttl: int = DEFAULT_TTL_SECONDS,
) -> bool:
    """寫入某專案的甘特 / CPM 快取結果。"""
    return await cache_set(_project_key(tenant_id, project_id), data, ttl)


async def invalidate_project_gantt(tenant_id: str, project_id: str) -> bool:
    """使某專案的甘特快取失效（任務 / 工期變動後呼叫）。"""
    return await cache_delete(_project_key(tenant_id, project_id))


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
