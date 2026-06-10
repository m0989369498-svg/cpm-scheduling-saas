"""各家 ERP 具體 Adapter 與工廠 (Concrete ERP adapters & factory).

每一家 ERP 的欄位命名 / 結構天差地遠，反腐層的價值就在於把這些差異全部
封裝在各自的 ``translate`` 中：

    SAP        : 以 WBS / NETWORK / ACTIVITY 為核心 (PS 模組語彙)
    DINGXIN_TW : 鼎新 (台灣常見 ERP)，使用其專案/工單編碼語彙
    YONYOU_CN  : 用友 (大陸常見 ERP)，使用 nc 風格欄位語彙

對外只暴露 ``get_adapter(erp_type)`` 工廠；未知類型回退至模擬 Adapter。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from app.config import settings

from .acl import CanonicalSyncItem, ErpAdapter

logger = logging.getLogger("app.erp.adapters")


# 排程狀態 -> 各家 ERP 狀態碼的對照表（示意，可依實際 ERP 設定調整）
_SAP_STATUS_MAP = {
    "PENDING": "REL",        # Released
    "IN_PROGRESS": "PCNF",   # Partially confirmed
    "COMPLETED": "CNF",      # Confirmed
    "DELAYED": "DLY",        # Delayed (自訂)
}

_DINGXIN_STATUS_MAP = {
    "PENDING": "未開工",
    "IN_PROGRESS": "施工中",
    "COMPLETED": "已完工",
    "DELAYED": "延遲",
}

_YONYOU_STATUS_MAP = {
    "PENDING": "未开工",
    "IN_PROGRESS": "进行中",
    "COMPLETED": "已完成",
    "DELAYED": "延期",
}


class SapAdapter(ErpAdapter):
    """SAP (PS 模組) Adapter。

    SAP 專案系統以 WBS (Work Breakdown Structure) 與 NETWORK / ACTIVITY 描述工項，
    工期以「天」為單位 (DURATION_UNIT='DAY')，狀態使用系統狀態碼 (REL/CNF...)。
    """

    erp_type = "SAP"

    def translate(self, canonical: CanonicalSyncItem) -> dict[str, Any]:
        return {
            "SAP_INTERFACE": "PS_SCHEDULE_SYNC",
            "TENANT": canonical.tenant_id,
            # SAP 以 WBS 元素為核心
            "WBS_ELEMENT": canonical.wbs_code,
            "NETWORK_ACTIVITY": canonical.task_id,
            "ACTIVITY_TEXT": canonical.task_name,
            "DURATION": canonical.duration,
            "DURATION_UNIT": "DAY",
            "SYSTEM_STATUS": _SAP_STATUS_MAP.get(canonical.status, "REL"),
            "EARLY_START": canonical.es,
            "EARLY_FINISH": canonical.ef,
            "TOTAL_FLOAT": canonical.float_time,
            "CRITICAL_FLAG": "X" if canonical.is_critical else "",
            "BASIC_START_DATE": canonical.start_date,
            "BASIC_FINISH_DATE": canonical.finish_date,
        }

    def build_headers(self) -> dict[str, Any]:
        """SAP OData / REST 介面：以 Bearer Token 認證。

        Token 由環境變數 ``SAP_API_TOKEN`` 注入。SAP Gateway 慣例上會要求
        ``Accept: application/json``，並對寫入操作回傳 OData 標準結構。
        """
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            # SAP Gateway 對部分服務要求關閉 CSRF 取得 (此處為單純寫入，標記 fetch)
            "x-csrf-token": "fetch",
        }
        token = (settings.sap_api_token or "").strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        else:
            logger.warning("[ERP:SAP] 未設定 SAP_API_TOKEN，將不帶 Authorization 推送")
        return headers

    def extract_erp_ref(self, body: dict[str, Any]) -> Optional[str]:
        """SAP 回應常見以 d.WBS_ELEMENT / NETWORK_ACTIVITY 或 d.results 表示。"""
        d = body.get("d") if isinstance(body.get("d"), dict) else body
        for key in ("WBS_ELEMENT", "NETWORK_ACTIVITY", "DocumentNumber", "Objnr"):
            val = d.get(key)
            if val not in (None, ""):
                return str(val)
        return super().extract_erp_ref(body)


class DingxinAdapter(ErpAdapter):
    """鼎新 (DINGXIN_TW) Adapter — 台灣常見 ERP。

    鼎新以「專案編號 / 工單 / 工序」描述，欄位多為中文語彙或鼎新自訂代碼。
    """

    erp_type = "DINGXIN_TW"

    def translate(self, canonical: CanonicalSyncItem) -> dict[str, Any]:
        return {
            "interface": "DINGXIN_PROJECT_SYNC",
            "租戶": canonical.tenant_id,
            # 鼎新以工作分解編碼對應工序
            "工序代號": canonical.task_id,
            "WBS編碼": canonical.wbs_code,
            "工序名稱": canonical.task_name,
            "預計工期天數": canonical.duration,
            "工序狀態": _DINGXIN_STATUS_MAP.get(canonical.status, "未開工"),
            "最早開工": canonical.es,
            "最早完工": canonical.ef,
            "寬裕時間": canonical.float_time,
            "是否要徑": "Y" if canonical.is_critical else "N",
            "計畫開工日": canonical.start_date,
            "計畫完工日": canonical.finish_date,
        }

    def build_headers(self) -> dict[str, Any]:
        """鼎新 (DINGXIN_TW)：以自訂 API-Key 標頭認證。

        鼎新 Web API 慣例以 ``X-Api-Key`` 傳遞金鑰，金鑰由環境變數
        ``DINGXIN_API_KEY`` 注入。
        """
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json",
        }
        api_key = (settings.dingxin_api_key or "").strip()
        if api_key:
            headers["X-Api-Key"] = api_key
        else:
            logger.warning("[ERP:DINGXIN_TW] 未設定 DINGXIN_API_KEY，將不帶 X-Api-Key 推送")
        return headers

    def extract_erp_ref(self, body: dict[str, Any]) -> Optional[str]:
        """鼎新回應常見以「單號」/「工單號」表示。"""
        for key in ("單號", "工單號", "doc_no", "billNo"):
            val = body.get(key)
            if val not in (None, ""):
                return str(val)
        return super().extract_erp_ref(body)


class YonyouAdapter(ErpAdapter):
    """用友 (YONYOU_CN) Adapter — 大陸常見 ERP (NC/U8 風格)。

    用友以 nc 風格欄位 (pk_*, 編碼採簡體) 描述項目工序。
    """

    erp_type = "YONYOU_CN"

    def translate(self, canonical: CanonicalSyncItem) -> dict[str, Any]:
        return {
            "billtype": "YONYOU_PROJECT_SYNC",
            "pk_tenant": canonical.tenant_id,
            # 用友以項目任務編碼描述
            "task_code": canonical.task_id,
            "wbs_code": canonical.wbs_code,
            "task_name": canonical.task_name,
            "plan_duration": canonical.duration,
            "task_status": _YONYOU_STATUS_MAP.get(canonical.status, "未开工"),
            "early_start": canonical.es,
            "early_finish": canonical.ef,
            "total_float": canonical.float_time,
            "is_key_path": 1 if canonical.is_critical else 0,
            "plan_begin_date": canonical.start_date,
            "plan_end_date": canonical.finish_date,
        }

    def build_headers(self) -> dict[str, Any]:
        """用友 (YONYOU_CN, NC/U8 風格)：以自訂 API-Key 標頭認證。

        用友開放平台慣例以 ``access_token`` 標頭傳遞金鑰，金鑰由環境變數
        ``YONYOU_API_KEY`` 注入。
        """
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json",
        }
        api_key = (settings.yonyou_api_key or "").strip()
        if api_key:
            # 用友 OpenAPI 以 access_token 標頭傳遞憑證
            headers["access_token"] = api_key
        else:
            logger.warning("[ERP:YONYOU_CN] 未設定 YONYOU_API_KEY，將不帶 access_token 推送")
        return headers

    def extract_erp_ref(self, body: dict[str, Any]) -> Optional[str]:
        """用友回應常見以 pk / billno / data.pk 表示。"""
        data = body.get("data") if isinstance(body.get("data"), dict) else body
        for key in ("pk", "pk_bill", "billno", "billNo", "code"):
            val = data.get(key)
            if val not in (None, ""):
                return str(val)
        return super().extract_erp_ref(body)


class SimulateAdapter(ErpAdapter):
    """模擬 Adapter — 未知 / 未設定 ERP 類型時的安全回退。

    直接以 canonical 模型內容作為 payload，搭配 ErpAdapter.push 的「無端點即模擬成功」
    行為，可在開發 / 測試環境完整跑通流程而不需真實 ERP。
    """

    erp_type = "SIMULATE"

    def translate(self, canonical: CanonicalSyncItem) -> dict[str, Any]:
        return {
            "interface": "SIMULATE_SYNC",
            "tenant_id": canonical.tenant_id,
            "task_id": canonical.task_id,
            "wbs_code": canonical.wbs_code,
            "task_name": canonical.task_name,
            "duration": canonical.duration,
            "status": canonical.status,
            "es": canonical.es,
            "ef": canonical.ef,
            "float_time": canonical.float_time,
            "is_critical": canonical.is_critical,
            "start_date": canonical.start_date,
            "finish_date": canonical.finish_date,
        }


# erp_type 字串 -> Adapter 類別
_ADAPTER_REGISTRY: dict[str, type[ErpAdapter]] = {
    "SAP": SapAdapter,
    "DINGXIN_TW": DingxinAdapter,
    "YONYOU_CN": YonyouAdapter,
    "SIMULATE": SimulateAdapter,
}


def get_adapter(erp_type: Optional[str], api_endpoint: Optional[str] = None) -> ErpAdapter:
    """ERP Adapter 工廠。

    依 ``erp_type`` (來自 erp_integration.tenant_erp_config.erp_type) 取得對應 Adapter
    實例；未知 / 空值一律回退至 :class:`SimulateAdapter`，確保流程不中斷。

    參數：
        erp_type     : "SAP" / "DINGXIN_TW" / "YONYOU_CN" (大小寫不敏感)
        api_endpoint : 該租戶設定的 ERP API 端點 (空 => Adapter.push 進入模擬模式)
    """
    key = (erp_type or "").strip().upper()
    adapter_cls = _ADAPTER_REGISTRY.get(key)
    if adapter_cls is None:
        logger.warning("未知的 erp_type=%r，回退至 SimulateAdapter", erp_type)
        adapter_cls = SimulateAdapter
    return adapter_cls(api_endpoint=api_endpoint)
