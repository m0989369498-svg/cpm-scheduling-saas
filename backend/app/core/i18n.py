"""後端多語系字典（i18n）。

支援兩岸雙地區（cross-strait）：
    TW  繁體中文（台灣用語）
    CN  簡體中文（大陸用語）

鍵（key）必須與前端 frontend/src/i18n/index.js 完全一致，
讓 PDF 報表、通知訊息與 UI 使用同一套詞彙。

匯出：
    I18N  地區 -> {key: 文字} 字典
    t(region, key)  取字串；找不到時依序回退 region -> TW -> key 本身
"""

from __future__ import annotations

# 注意：TW 與 CN 的鍵集合必須一致（包含 statuses 子表）。
#  值（value）與前端 frontend/src/i18n/index.js 對齊，確保 UI、PDF 報表、
#  通知訊息對同一狀態/詞彙顯示一致；CN 採真正的簡體中文（大陸用語）。
I18N: dict[str, dict] = {
    "TW": {
        "appTitle": "CPM 工程排程與自動化平台",
        "project": "專案",
        "projectName": "專案名稱",
        "task": "任務",
        "taskId": "任務編號",
        "taskName": "任務名稱",
        "critical": "要徑",
        "criticalPath": "要徑",
        "floatTime": "寬裕時間",
        "duration": "工期",
        "status": "狀態",
        "day": "天",
        "days": "天",
        "region": "區域",
        "tenant": "租戶",
        "recalc": "重新計算",
        "addTask": "新增任務",
        "updateDuration": "更新工期",
        "syncErp": "拋轉ERP",
        "downloadReport": "下載報表",
        "reportTitle": "工期報表",
        "projectDuration": "專案總工期",
        "loading": "載入中…",
        "error": "錯誤",
        # Phase 8 — 風險分析 / 蒙地卡羅（用於風險卡片、PDF 報表）
        "criticalityIndex": "要徑機率",
        "riskProvision": "風險預警備料",
        "onTimeProbability": "準時完工機率",
        # Phase 9 — 實獲值管理（EVM；用於排程/成本超支風險卡片）
        "bac": "完工預算 (BAC)",
        "spi": "進度績效指標 (SPI)",
        "cpi": "成本績效指標 (CPI)",
        "eac": "完工估算 (EAC)",
        "scheduleVariance": "進度差異 (SV)",
        "costVariance": "成本差異 (CV)",
        # Batch 3 — 真實日期（FEAT-2；用於 Excel / PDF 匯出的日期欄位）
        "plannedStart": "計畫開工",
        "plannedFinish": "計畫完工",
        # Pro Batch B — WBS 階層（FEAT-1；用於 PDF 匯出的 WBS 欄位）
        "wbsCode": "WBS 編碼",
        "statuses": {
            "PENDING": "待辦",
            "IN_PROGRESS": "進行中",
            "COMPLETED": "已完成",
            "DELAYED": "延遲",
        },
    },
    "CN": {
        "appTitle": "CPM 工程进度与自动化平台",
        "project": "项目",
        "projectName": "项目名称",
        "task": "任务",
        "taskId": "任务编号",
        "taskName": "任务名称",
        "critical": "关键路径",
        "criticalPath": "关键路径",
        "floatTime": "总时差",
        "duration": "工期",
        "status": "状态",
        "day": "天",
        "days": "天",
        "region": "区域",
        "tenant": "租户",
        "recalc": "重新计算",
        "addTask": "新增任务",
        "updateDuration": "更新工期",
        "syncErp": "拋轉ERP",
        "downloadReport": "下载报表",
        "reportTitle": "工期报表",
        "projectDuration": "项目总工期",
        "loading": "加载中…",
        "error": "错误",
        # Phase 8 — 风险分析 / 蒙特卡洛（用于风险卡片、PDF 报表）
        "criticalityIndex": "关键路径概率",
        "riskProvision": "风险预警备料",
        "onTimeProbability": "按时完工概率",
        # Phase 9 — 挣值管理（EVM；用于进度/成本超支风险卡片）
        "bac": "完工预算 (BAC)",
        "spi": "进度绩效指数 (SPI)",
        "cpi": "成本绩效指数 (CPI)",
        "eac": "完工估算 (EAC)",
        "scheduleVariance": "进度偏差 (SV)",
        "costVariance": "成本偏差 (CV)",
        # Batch 3 — 真实日期（FEAT-2；用于 Excel / PDF 导出的日期列）
        "plannedStart": "计划开工",
        "plannedFinish": "计划完工",
        # Pro Batch B — WBS 层级（FEAT-1；用于 PDF 导出的 WBS 列）
        "wbsCode": "WBS 编码",
        "statuses": {
            "PENDING": "待办",
            "IN_PROGRESS": "进行中",
            "COMPLETED": "已完成",
            "DELAYED": "延期",
        },
    },
}

_DEFAULT_REGION = "TW"


def t(region: str, key: str) -> str:
    """取得指定地區、指定鍵的翻譯字串。

    回退策略（fallback）：
        1) 指定 region 的字典
        2) TW 字典
        3) key 本身（找不到時）

    狀態翻譯請使用點記法（dot notation），例如 t('CN', 'statuses.DELAYED')。
    """
    region_key = region if region in I18N else _DEFAULT_REGION

    def _lookup(table: dict, dotted: str):
        node = table
        for part in dotted.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                return None
        return node if isinstance(node, str) else None

    value = _lookup(I18N[region_key], key)
    if value is not None:
        return value

    # 回退到 TW
    if region_key != _DEFAULT_REGION:
        value = _lookup(I18N[_DEFAULT_REGION], key)
        if value is not None:
            return value

    # 最終回退：回傳 key 本身
    return key
