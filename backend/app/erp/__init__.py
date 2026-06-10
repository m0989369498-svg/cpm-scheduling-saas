"""ERP 整合套件 (ERP Integration Package).

本套件實作 ERP 反腐層 (Anti-Corruption Layer, ACL) 與背景同步 worker：

- ``acl``      : 定義內部正規模型 (CanonicalSyncItem) 與 ErpAdapter 抽象基底，
                 負責「領域模型 -> 各家 ERP payload」的翻譯與推送 (push)。
- ``adapters`` : 各家 ERP 的具體實作 (SAP / 鼎新 DINGXIN_TW / 用友 YONYOU_CN)，
                 以及 ``get_adapter`` 工廠函式。
- ``worker``   : 以 APScheduler 定期掃描 ``erp_integration.sync_event_log``
                 之 PENDING 事件，翻譯後推送至對應 ERP，並處理重試 / DEAD 狀態。

設計重點：worker 使用獨立的 AsyncSession，且「不」設定 RLS 的 app.current_tenant，
因為 erp_integration schema 並未啟用 RLS，可由跨租戶 worker 直接掃描，程式碼自行
以 tenant_id 過濾。
"""

from .acl import CanonicalSyncItem, ErpAdapter, ErpPushError, ErpPushResult
from .adapters import (
    DingxinAdapter,
    SapAdapter,
    SimulateAdapter,
    YonyouAdapter,
    get_adapter,
)

__all__ = [
    "CanonicalSyncItem",
    "ErpAdapter",
    "ErpPushError",
    "ErpPushResult",
    "SapAdapter",
    "DingxinAdapter",
    "YonyouAdapter",
    "SimulateAdapter",
    "get_adapter",
]
