"""ERP 反腐層 (Anti-Corruption Layer, ACL).

此模組定義「內部正規模型 (canonical model)」與 ERP 介接的抽象介面。

核心理念 (DDD ACL)：排程系統內部只認識自己的領域語言 (task_id / wbs_code /
duration / status / 日期)，絕不讓任何一家 ERP 的欄位命名 (SAP 的 WBS/NETWORK、
鼎新的 PROJ_NO、用友的 nc 編碼…) 滲透進核心。各家 ERP 的差異全部封裝在
``ErpAdapter`` 子類別的 ``translate`` 中，對外只暴露一致的 canonical 介面。

公開 API：
    CanonicalSyncItem  : 內部正規同步單元 (Pydantic model)
    ErpPushResult      : push() 的標準回傳結果
    ErpAdapter         : 抽象基底，定義 translate() 與 push()
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import httpx
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger("app.erp.acl")

# httpx 推送逾時 (秒)：總逾時 (相容舊行為)
DEFAULT_HTTP_TIMEOUT = 15.0
# 連線 (connect) 與讀取 (read) 逾時 — 真實 ERP 端點建議分別設限，避免單一卡點拖垮 worker
DEFAULT_CONNECT_TIMEOUT = 10.0
DEFAULT_READ_TIMEOUT = 10.0


class ErpPushError(RuntimeError):
    """ERP 推送失敗例外 (transport 失敗或 ERP 回非 2xx)。

    刻意以例外向 worker 拋出：worker 的 ``_process_event`` 會攔截任何例外並呼叫
    ``_mark_failure``，使 retry_count 遞增、寫入 last_error，達上限時翻為 DEAD。
    例外訊息即為要寫入 ``sync_event_log.last_error`` 的內容，務求清楚可診斷。
    """

    def __init__(self, message: str, *, erp_type: str = "", status_code: Optional[int] = None):
        super().__init__(message)
        self.erp_type = erp_type
        self.status_code = status_code


class CanonicalSyncItem(BaseModel):
    """內部正規同步單元 (Canonical Sync Item).

    這是排程領域對「一筆要拋轉到 ERP 的工項」的標準描述，與任何特定 ERP 解耦。
    Adapter 會把這個 canonical 物件翻譯成各家 ERP 真正吃的 payload。

    欄位：
        tenant_id   : 租戶代碼 (多租戶隔離)
        task_id     : 排程任務代碼 (e.g. T-01)
        wbs_code    : ERP 端對應的 WBS / 工作分解結構編碼
        task_name   : 任務名稱
        duration    : 工期 (天)
        status      : 排程狀態 (PENDING / IN_PROGRESS / COMPLETED / DELAYED)
        es / ef     : 最早開始 / 最早完成 (CPM 計算值，以「天」為單位的偏移)
        is_critical : 是否位於要徑 (關鍵路徑)
        float_time  : 寬裕時間 / 總時差
        start_date  : 計畫開始日期 (ISO 字串，可選；若無則交由 ERP 端決定)
        finish_date : 計畫完成日期 (ISO 字串，可選)
        extra       : 額外自訂資料 (例如 sync_type)
    """

    model_config = ConfigDict(extra="ignore")

    tenant_id: str
    task_id: str
    wbs_code: str
    task_name: str = ""
    duration: int = 0
    status: str = "PENDING"
    es: int = 0
    ef: int = 0
    is_critical: bool = False
    float_time: int = 0
    start_date: Optional[str] = None
    finish_date: Optional[str] = None
    extra: dict[str, Any] = Field(default_factory=dict)


class ErpPushResult(BaseModel):
    """ERP 推送結果 (標準化回傳)。

    無論底層是真正的 HTTP 呼叫或是模擬 (simulate)，都回傳此統一結構，
    讓 worker 能以一致方式判斷成功 / 失敗。
    """

    ok: bool
    erp_type: str
    message: str = ""
    # HTTP 狀態碼 (模擬模式為 None)
    status_code: Optional[int] = None
    # ERP 端回傳的單號 / 參考碼 (若 ERP 回傳)，供後續對帳追蹤
    erp_ref: Optional[str] = None
    # ERP 端回傳的原始資料 (供除錯 / 稽核)；亦即正規化結果中的 raw
    response: dict[str, Any] = Field(default_factory=dict)
    # 實際送出的 payload (供 sync_event_log 稽核)
    sent_payload: dict[str, Any] = Field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """正規化結果字典 {ok, status_code, erp_ref?, raw}，供需要純 dict 的呼叫端使用。"""
        out: dict[str, Any] = {
            "ok": self.ok,
            "status_code": self.status_code,
            "raw": self.response,
        }
        if self.erp_ref is not None:
            out["erp_ref"] = self.erp_ref
        return out


class ErpAdapter:
    """ERP 介接抽象基底 (Anti-Corruption Layer adapter)。

    子類別必須實作 :meth:`translate`，把 canonical 物件翻成該 ERP 的 payload。
    :meth:`push` 為共用實作：若 ``api_endpoint`` 為空字串 / None，則「模擬成功」
    (在缺乏實際 ERP 憑證 / 端點時，仍完整跑完翻譯與流程邏輯)；否則以 httpx 實際推送。
    """

    #: ERP 類型識別碼 (子類別覆寫，例如 "SAP")
    erp_type: str = "BASE"

    def __init__(
        self,
        api_endpoint: Optional[str] = None,
        *,
        http_timeout: float = DEFAULT_HTTP_TIMEOUT,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        read_timeout: float = DEFAULT_READ_TIMEOUT,
    ):
        # api_endpoint 來自 erp_integration.tenant_erp_config.api_endpoint
        self.api_endpoint = (api_endpoint or "").strip()
        # 保留 http_timeout 以相容舊呼叫；實際推送以 connect/read 分別設限
        self.http_timeout = http_timeout
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout

    # ------------------------------------------------------------------ #
    # 必須由子類別實作：翻譯
    # ------------------------------------------------------------------ #
    def translate(self, canonical: CanonicalSyncItem) -> dict[str, Any]:
        """把 canonical 物件翻譯成該 ERP 專屬的 payload (dict)。

        子類別必須覆寫此方法。這是反腐層的核心：所有 ERP 欄位命名差異
        都被侷限在此處。
        """
        raise NotImplementedError("子類別必須實作 translate()")

    # ------------------------------------------------------------------ #
    # 可由子類別覆寫：認證 / 內容型別 標頭
    # ------------------------------------------------------------------ #
    def build_headers(self) -> dict[str, str]:
        """組出該 ERP 推送所需的 HTTP 標頭 (認證 + content-type)。

        基底僅提供 JSON content-type；各家 ERP 子類別覆寫以加上其專屬的
        認證標頭 (Bearer Token / API-Key…)。憑證一律從 Settings (環境變數) 讀取，
        絕不寫死於程式碼。
        """
        return {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    # ------------------------------------------------------------------ #
    # 可由子類別覆寫：自 ERP 回應萃取單號 / 參考碼
    # ------------------------------------------------------------------ #
    def extract_erp_ref(self, body: dict[str, Any]) -> Optional[str]:
        """嘗試從 ERP 回應 body 取出可對帳的單號 / 參考碼 (找不到回 None)。

        各家 ERP 回傳欄位名不同，子類別可覆寫。基底涵蓋幾個常見命名。
        """
        for key in ("erp_ref", "ref", "doc_no", "document_number", "id", "ID", "key"):
            val = body.get(key)
            if val not in (None, ""):
                return str(val)
        return None

    # ------------------------------------------------------------------ #
    # 共用：推送
    # ------------------------------------------------------------------ #
    async def push(self, payload: dict[str, Any]) -> ErpPushResult:
        """將翻譯後的 payload 真正推送至 ERP。

        - 若無 ``api_endpoint`` (缺端點 / 缺憑證情境) -> 模擬成功並記錄 log
          (回傳 ``response`` 標記 ``simulated:true``，讓 demo 在無憑證時仍可跑通)。
        - 若有端點 -> 以 ``httpx.AsyncClient`` 非同步 POST 推送：
            * 設定 connect / read 逾時 (各 10s)
            * 帶上子類別 :meth:`build_headers` 提供的認證 + content-type 標頭
            * 以 JSON 送出 :meth:`translate` 產生的 per-ERP 請求 body
            * 非 2xx 或 transport 例外 -> 拋出 :class:`ErpPushError` (清楚訊息)，
              交由 worker 記錄 last_error 並進行重試 / DEAD 計數
            * 成功 -> 回傳正規化結果 (ok / status_code / erp_ref / raw)
        """
        if not self.api_endpoint:
            # 模擬模式：沒有真實端點時，視為成功 (但仍記錄，便於追蹤)。
            logger.info(
                "[ERP:%s] 無 api_endpoint，模擬推送成功 (simulate). payload=%s",
                self.erp_type,
                payload,
            )
            return ErpPushResult(
                ok=True,
                erp_type=self.erp_type,
                message="simulated (no endpoint configured)",
                status_code=None,
                response={"simulated": True},
                sent_payload=payload,
            )

        # 分別設定 connect / read 逾時，避免單一網路卡點拖垮 worker
        timeout = httpx.Timeout(
            self.read_timeout,
            connect=self.connect_timeout,
            read=self.read_timeout,
        )
        headers = self.build_headers()

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(self.api_endpoint, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            # 連線逾時 / DNS / 拒絕連線等 transport 失敗：拋例外讓 worker 重試。
            msg = f"[ERP:{self.erp_type}] transport 失敗 endpoint={self.api_endpoint} err={exc}"
            logger.warning(msg)
            raise ErpPushError(msg, erp_type=self.erp_type) from exc

        # 解析回應 body (ERP 可能回非 JSON)
        try:
            body = resp.json()
            if not isinstance(body, dict):
                body = {"data": body}
        except Exception:  # noqa: BLE001 - ERP 可能回非 JSON
            body = {"text": resp.text}

        if not resp.is_success:
            # 非 2xx：拋例外讓 worker 記錄 last_error 並重試 (達上限翻 DEAD)。
            msg = (
                f"[ERP:{self.erp_type}] 推送失敗 status={resp.status_code} "
                f"endpoint={self.api_endpoint} body={body}"
            )
            logger.warning(msg)
            raise ErpPushError(msg, erp_type=self.erp_type, status_code=resp.status_code)

        erp_ref = self.extract_erp_ref(body)
        logger.info(
            "[ERP:%s] 推送成功 status=%s endpoint=%s erp_ref=%s",
            self.erp_type,
            resp.status_code,
            self.api_endpoint,
            erp_ref,
        )
        return ErpPushResult(
            ok=True,
            erp_type=self.erp_type,
            message=f"HTTP {resp.status_code}",
            status_code=resp.status_code,
            erp_ref=erp_ref,
            response=body,
            sent_payload=payload,
        )

    # ------------------------------------------------------------------ #
    # 便利方法：一步完成 翻譯 + 推送
    # ------------------------------------------------------------------ #
    async def translate_and_push(self, canonical: CanonicalSyncItem) -> ErpPushResult:
        """翻譯並推送的便利封裝，worker 主要呼叫此方法。

        失敗時 :meth:`push` 會拋出 :class:`ErpPushError`，由 worker 的 try/except 攔截。
        """
        payload = self.translate(canonical)
        return await self.push(payload)
