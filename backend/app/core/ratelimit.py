"""登入失敗速率限制 / 帳號鎖定 (login rate-limit / lockout)。

用途：對「同一帳號 + 來源 IP」的登入失敗次數計數，超過
``settings.login_max_failures`` 後於 ``settings.login_lockout_seconds`` 秒內
鎖定 (回 429)，緩解暴力破解 / 撞庫攻擊。

儲存後端 (兩段式 fallback)：
  1) Redis (settings.redis_url)：跨行程 / 跨副本共享計數，正式環境首選。
     以 INCR 累加、首次失敗時 EXPIRE 設定 TTL；查鎖定以 GET + TTL 判斷。
  2) 行程內 dict (in-process)：當「未設定 redis_url」「redis 套件未安裝」或
     「任何 redis 連線 / 指令錯誤」時自動退回。單機開發 / 測試 (無 redis) 亦可運作。
     以 {key: (count, expiry_monotonic)} 保存；逾時即視為重置。

設計重點：
  * 完全 async；對外 API 皆為 coroutine，便於於 async login handler 直接 await。
  * 對 redis 的任何例外一律「吞掉並退回 in-process」，速率限制本身絕不可使登入崩潰。
  * key 由呼叫端組為 f"{username.lower()}|{ip}"，本模組不關心其組成。

公開 API：
  async register_failure(key) -> int   # 累加並回傳目前失敗次數
  async is_locked(key) -> int          # 回傳剩餘鎖定秒數 (0 表示未鎖定)
  async reset(key) -> None             # 清除該 key 的失敗計數 (登入成功時呼叫)
"""

from __future__ import annotations

import logging
import math
import time
from threading import Lock

from app.config import settings

logger = logging.getLogger("cpm.core.ratelimit")

# Redis key 前綴 (與其他用途隔離，便於維運辨識 / 清除)。
_REDIS_PREFIX = "cpm:login:fail:"

# ---------------------------------------------------------------------------
# Redis client (LAZY)：首次使用時才嘗試建立。建立或指令失敗即標記為不可用，
# 後續一律走 in-process fallback (避免每次請求都重試連線拖慢登入)。
# ---------------------------------------------------------------------------
_redis_client = None          # 已建立的 async redis client (或 None)
_redis_unavailable = False     # True 表示放棄 redis、永久走 in-process fallback


def _window_seconds() -> int:
    """鎖定 / 計數視窗秒數 (至少 1，避免 EXPIRE 0 立即過期)。"""
    return max(1, int(settings.login_lockout_seconds))


def _get_redis():
    """回傳 (必要時建立) async redis client；不可用時回傳 None。

    任何匯入 / 連線設定錯誤都會吞掉並把 _redis_unavailable 設為 True，
    使呼叫端退回 in-process fallback。實際連線錯誤 (server 未啟動) 會在
    第一個指令執行時才浮現，由各 API 的 try/except 攔截後同樣退回 fallback。
    """
    global _redis_client, _redis_unavailable
    if _redis_unavailable:
        return None
    if _redis_client is not None:
        return _redis_client
    url = (settings.redis_url or "").strip()
    if not url:
        _redis_unavailable = True
        return None
    try:
        # redis-py 4.2+ 內建 asyncio 介面 (redis.asyncio)。延遲匯入：未安裝
        # redis 套件 (例如純 sqlite 測試環境) 時不致於 import 階段失敗。
        from redis.asyncio import Redis  # type: ignore

        _redis_client = Redis.from_url(
            url,
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=0.5,
            socket_timeout=0.5,
        )
        return _redis_client
    except Exception as exc:  # noqa: BLE001 - redis 不可用一律退回 in-process
        logger.warning(
            "ratelimit: redis 不可用 (%s)，改用行程內計數 (in-process fallback)。",
            exc,
        )
        _redis_unavailable = True
        return None


def _mark_redis_down(exc: Exception) -> None:
    """標記 redis 為不可用並記錄一次警告 (之後一律走 in-process)。"""
    global _redis_unavailable
    if not _redis_unavailable:
        logger.warning(
            "ratelimit: redis 指令失敗 (%s)，改用行程內計數 (in-process fallback)。",
            exc,
        )
    _redis_unavailable = True


# ---------------------------------------------------------------------------
# In-process fallback：{key: (count, expiry_monotonic)}。
# 以 time.monotonic() 計算到期 (不受系統時鐘調整影響)；逾時的 entry 視為已重置。
# 以 Lock 保護，確保多執行緒 (TestClient / uvicorn workers in-thread) 下計數正確。
# ---------------------------------------------------------------------------
_local_store: dict[str, tuple[int, float]] = {}
_local_lock = Lock()


def _local_register_failure(key: str) -> int:
    window = _window_seconds()
    now = time.monotonic()
    with _local_lock:
        count, expiry = _local_store.get(key, (0, 0.0))
        if expiry <= now:
            # 視窗已過 (或首次)：重新計數並設定新到期。
            count = 0
            expiry = now + window
        count += 1
        _local_store[key] = (count, expiry)
        return count


def _local_is_locked(key: str) -> int:
    now = time.monotonic()
    with _local_lock:
        count, expiry = _local_store.get(key, (0, 0.0))
        if expiry <= now:
            # 過期：順手清除，避免 dict 無限成長。
            _local_store.pop(key, None)
            return 0
        if count >= settings.login_max_failures:
            return max(1, int(math.ceil(expiry - now)))
        return 0


def _local_reset(key: str) -> None:
    with _local_lock:
        _local_store.pop(key, None)


# ---------------------------------------------------------------------------
# 公開 API
# ---------------------------------------------------------------------------
async def register_failure(key: str) -> int:
    """累加一次登入失敗，回傳目前 (視窗內) 失敗次數。

    Redis：INCR 後若為首次 (==1) 則 EXPIRE 設定視窗 TTL，使計數於視窗結束後自動歸零。
    任何 redis 錯誤 -> 退回 in-process。
    """
    redis = _get_redis()
    if redis is not None:
        try:
            rkey = _REDIS_PREFIX + key
            count = int(await redis.incr(rkey))
            if count == 1:
                await redis.expire(rkey, _window_seconds())
            return count
        except Exception as exc:  # noqa: BLE001
            _mark_redis_down(exc)
    return _local_register_failure(key)


async def is_locked(key: str) -> int:
    """回傳該 key 目前的剩餘鎖定秒數；未達門檻 / 未鎖定回 0。

    Redis：以 GET 取得目前計數，達門檻則以 TTL 換算剩餘秒數。
    任何 redis 錯誤 -> 退回 in-process。
    """
    redis = _get_redis()
    if redis is not None:
        try:
            rkey = _REDIS_PREFIX + key
            raw = await redis.get(rkey)
            count = int(raw) if raw is not None else 0
            if count >= settings.login_max_failures:
                ttl = int(await redis.ttl(rkey))
                # ttl: -1 無過期, -2 不存在；皆退回視窗長度作為保守剩餘秒數。
                if ttl < 0:
                    ttl = _window_seconds()
                return max(1, ttl)
            return 0
        except Exception as exc:  # noqa: BLE001
            _mark_redis_down(exc)
    return _local_is_locked(key)


async def reset(key: str) -> None:
    """清除該 key 的失敗計數 (登入成功時呼叫)。任何 redis 錯誤 -> 退回 in-process。"""
    redis = _get_redis()
    if redis is not None:
        try:
            await redis.delete(_REDIS_PREFIX + key)
            return
        except Exception as exc:  # noqa: BLE001
            _mark_redis_down(exc)
    _local_reset(key)
