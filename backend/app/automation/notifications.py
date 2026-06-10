"""推播通知模組 (Notifications).

雙區域通知通道:
  - notify_line(message):     台灣 (TW) 透過 LINE Messaging API 廣播。
  - notify_dingtalk(message): 中國大陸 (CN) 透過釘釘 (DingTalk) 群機器人 Webhook。

無憑證時的行為 (重要):
  當 settings.line_channel_access_token 或 settings.dingtalk_webhook_url 為空,
  則「不發出任何網路請求」, 僅記錄 log 並回傳結果 dict, 通道視為 no-op。
  如此可在本機 / CI 無外網或無金鑰的情況下安全執行。

其他公開函式:
  - build_schedule_summary(project_out, region) -> str   組裝可讀的排程摘要文字。
  - notify_schedule_update(project_out, region) -> dict   依區域 (CN→釘釘, 其餘→LINE)
        於重算後 best-effort 推播; 任何例外都被吞掉, 不影響主流程。

設計重點:
  * 全為 async, 使用 httpx.AsyncClient。
  * 不拋例外給呼叫端 (通知失敗不應中斷排程業務); 失敗以 log + 回傳 dict 表達。
"""

from __future__ import annotations

import logging
from typing import Any, Iterable

import httpx

from app.config import settings
from app.core.i18n import t

logger = logging.getLogger("cpm.automation.notifications")

# 對外 HTTP 請求逾時 (秒)
_HTTP_TIMEOUT = 10.0

# LINE Messaging API 廣播端點
_LINE_BROADCAST_URL = "https://api.line.me/v2/bot/message/broadcast"


# ---------------------------------------------------------------------------
# 風險預警卡 (risk card) 的雙語文案
#   key: 內部 reason 代碼 -> {TW: 繁體, CN: 簡體}
#   reason 來源:
#     LEVELING_EXTENSION       資源撫平導致工期展延 (resource leveling extension)
#     LOW_ONTIME_PROBABILITY   蒙地卡羅模擬準時機率偏低 (low on-time probability)
# ---------------------------------------------------------------------------
_RISK_TITLE: dict[str, dict[str, str]] = {
    "TW": {
        "_default": "排程風險預警",
        "LEVELING_EXTENSION": "資源撫平導致工期展延",
        "LOW_ONTIME_PROBABILITY": "準時完工機率偏低",
    },
    "CN": {
        "_default": "进度风险预警",
        "LEVELING_EXTENSION": "资源平衡导致工期延期",
        "LOW_ONTIME_PROBABILITY": "按时完工概率偏低",
    },
}

# detail dict 中常見欄位的雙語標籤 (僅翻譯已知 key；未知 key 原樣顯示)
_RISK_FIELD_LABELS: dict[str, dict[str, str]] = {
    "TW": {
        "project_id": "專案",
        "original_duration": "原工期",
        "leveled_duration": "撫平後工期",
        "extended_by": "展延",
        "on_time_probability": "準時機率",
        "deadline": "合約工期",
        "p50": "P50 工期",
        "p90": "P90 工期",
        "mean": "平均工期",
        "unresolved": "未解衝突",
        "over_capacity_days": "超載天數",
    },
    "CN": {
        "project_id": "项目",
        "original_duration": "原工期",
        "leveled_duration": "平衡后工期",
        "extended_by": "延期",
        "on_time_probability": "按时概率",
        "deadline": "合约工期",
        "p50": "P50 工期",
        "p90": "P90 工期",
        "mean": "平均工期",
        "unresolved": "未解冲突",
        "over_capacity_days": "超载天数",
    },
}


# ---------------------------------------------------------------------------
# project_out 欄位存取輔助 (容忍 Pydantic 物件 / dict)
# ---------------------------------------------------------------------------
def _get(obj: Any, key: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _iter_tasks(project_out: Any) -> Iterable[Any]:
    return _get(project_out, "tasks", []) or []


def _is_critical(task: Any) -> bool:
    if bool(_get(task, "is_critical", False)):
        return True
    return int(_get(task, "float_time", 0) or 0) == 0


# ---------------------------------------------------------------------------
# 摘要文字
# ---------------------------------------------------------------------------
def build_schedule_summary(project_out: Any, region: str = "TW") -> str:
    """組裝排程摘要文字 (用於 LINE / 釘釘訊息本文)。

    內容: 報表標題、專案名稱、總工期、要徑 (關鍵路徑) 任務序列。
    """
    region = (region or "TW").upper()

    project_name = _get(project_out, "project_name", "") or ""
    project_id = _get(project_out, "project_id", "") or ""
    duration = int(_get(project_out, "project_duration", 0) or 0)

    crit_ids = [
        str(_get(tk, "task_id", ""))
        for tk in _iter_tasks(project_out)
        if _is_critical(tk)
    ]
    crit_path = " → ".join(crit_ids) if crit_ids else "-"

    lines = [
        f"📋 {t(region, 'reportTitle')}",
        f"{t(region, 'project')}: {project_name} ({project_id})",
        f"{t(region, 'projectDuration')}: {duration} {t(region, 'days')}",
        f"🔥 {t(region, 'criticalPath')}: {crit_path}",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# LINE (台灣)
# ---------------------------------------------------------------------------
async def notify_line(message: str) -> dict:
    """透過 LINE Messaging API 廣播文字訊息。

    無 token 時為 no-op (僅 log)。回傳結果 dict:
      {"channel": "line", "sent": bool, "skipped"?: True, "status_code"?: int, "error"?: str}
    """
    token = (settings.line_channel_access_token or "").strip()
    if not token:
        logger.info("[LINE] 未設定 LINE_CHANNEL_ACCESS_TOKEN, 略過推播 (no-op): %s", message)
        return {"channel": "line", "sent": False, "skipped": True}

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    payload = {"messages": [{"type": "text", "text": message[:4900]}]}

    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.post(_LINE_BROADCAST_URL, headers=headers, json=payload)
        ok = resp.status_code == 200
        if ok:
            logger.info("[LINE] 推播成功 (status=%s)", resp.status_code)
        else:
            logger.warning("[LINE] 推播失敗 (status=%s): %s", resp.status_code, resp.text[:300])
        return {"channel": "line", "sent": ok, "status_code": resp.status_code}
    except Exception as exc:  # 通知失敗不可中斷主流程
        logger.warning("[LINE] 推播例外: %s", exc)
        return {"channel": "line", "sent": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# 釘釘 DingTalk (中國大陸)
# ---------------------------------------------------------------------------
async def notify_dingtalk(message: str) -> dict:
    """透過釘釘群機器人 Webhook 推送文字訊息。

    無 webhook 時為 no-op (僅 log)。回傳結果 dict:
      {"channel": "dingtalk", "sent": bool, "skipped"?: True, "status_code"?: int,
       "errcode"?: int, "error"?: str}
    """
    webhook = (settings.dingtalk_webhook_url or "").strip()
    if not webhook:
        logger.info("[DingTalk] 未設定 DINGTALK_WEBHOOK_URL, 略過推播 (no-op): %s", message)
        return {"channel": "dingtalk", "sent": False, "skipped": True}

    # 釘釘自訂機器人 text 訊息格式
    payload = {"msgtype": "text", "text": {"content": message}}

    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.post(webhook, json=payload)
        result: dict = {"channel": "dingtalk", "status_code": resp.status_code}
        errcode = None
        try:
            body = resp.json()
            errcode = body.get("errcode")
            result["errcode"] = errcode
            if body.get("errmsg"):
                result["errmsg"] = body.get("errmsg")
        except Exception:
            body = None
        # 釘釘成功時 HTTP 200 且 errcode == 0
        ok = resp.status_code == 200 and (errcode in (0, None))
        result["sent"] = ok
        if ok:
            logger.info("[DingTalk] 推播成功 (status=%s, errcode=%s)", resp.status_code, errcode)
        else:
            logger.warning(
                "[DingTalk] 推播失敗 (status=%s, errcode=%s): %s",
                resp.status_code,
                errcode,
                resp.text[:300],
            )
        return result
    except Exception as exc:  # 通知失敗不可中斷主流程
        logger.warning("[DingTalk] 推播例外: %s", exc)
        return {"channel": "dingtalk", "sent": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# 企業微信 WeCom (中國大陸；選填，與釘釘並行)
# ---------------------------------------------------------------------------
async def notify_wecom(message: str) -> dict:
    """透過企業微信 (WeCom) 群機器人 Webhook 推送文字訊息。

    Webhook 取自 settings.wecom_webhook_url (env WECOM_WEBHOOK_URL)。
    無 webhook 時為 no-op (僅 log)。回傳結果 dict:
      {"channel": "wecom", "sent": bool, "skipped"?: True, "status_code"?: int,
       "errcode"?: int, "error"?: str}
    """
    webhook = (getattr(settings, "wecom_webhook_url", "") or "").strip()
    if not webhook:
        logger.info("[WeCom] 未設定 WECOM_WEBHOOK_URL, 略過推播 (no-op): %s", message)
        return {"channel": "wecom", "sent": False, "skipped": True}

    # 企業微信群機器人 text 訊息格式
    payload = {"msgtype": "text", "text": {"content": message}}

    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.post(webhook, json=payload)
        result: dict = {"channel": "wecom", "status_code": resp.status_code}
        errcode = None
        try:
            body = resp.json()
            errcode = body.get("errcode")
            result["errcode"] = errcode
            if body.get("errmsg"):
                result["errmsg"] = body.get("errmsg")
        except Exception:
            body = None
        # 企業微信成功時 HTTP 200 且 errcode == 0
        ok = resp.status_code == 200 and (errcode in (0, None))
        result["sent"] = ok
        if ok:
            logger.info("[WeCom] 推播成功 (status=%s, errcode=%s)", resp.status_code, errcode)
        else:
            logger.warning(
                "[WeCom] 推播失敗 (status=%s, errcode=%s): %s",
                resp.status_code,
                errcode,
                resp.text[:300],
            )
        return result
    except Exception as exc:  # 通知失敗不可中斷主流程
        logger.warning("[WeCom] 推播例外: %s", exc)
        return {"channel": "wecom", "sent": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# 風險預警卡 (risk card)
# ---------------------------------------------------------------------------
def _fmt_risk_value(key: str, value: Any) -> str:
    """格式化 detail 值供卡片顯示 (機率轉百分比、list 以逗號連接)。"""
    if value is None:
        return "-"
    if key in ("on_time_probability",) and isinstance(value, (int, float)):
        return f"{value * 100:.1f}%"
    if isinstance(value, float):
        # 避免浮點長尾
        return f"{value:.2f}"
    if isinstance(value, (list, tuple)):
        return ", ".join(str(x) for x in value) if value else "-"
    return str(value)


def build_risk_card(region: str, reason: str, detail: dict | None = None) -> str:
    """組裝雙語 (繁/簡) 風險預警卡文字 (用於 LINE / 釘釘 / 企業微信 訊息本文)。

    region == 'CN' 用簡體 (大陸用語)，其餘用繁體 (台灣用語)。
    reason 對應 _RISK_TITLE；detail 內已知 key 以 _RISK_FIELD_LABELS 翻譯，
    未知 key 原樣顯示 (確保不漏資訊)。
    """
    region = (region or "TW").upper()
    table_region = region if region in _RISK_TITLE else "TW"
    titles = _RISK_TITLE[table_region]
    labels = _RISK_FIELD_LABELS[table_region]

    title = titles.get(reason, titles["_default"])
    header_icon = "⚠️"
    label_word = "風險原因" if table_region == "TW" else "风险原因"

    lines = [f"{header_icon} {title}", f"{label_word}: {reason}"]

    detail = detail or {}
    for key, value in detail.items():
        label = labels.get(key, key)
        lines.append(f"{label}: {_fmt_risk_value(key, value)}")

    return "\n".join(lines)


async def notify_risk(region: str, message: str) -> dict:
    """區域感知的風險通知路由 (best-effort, 非阻斷)。

    路由規則:
      region == 'CN' -> 釘釘 DingTalk (+ 若 settings.wecom_webhook_url 有設定, 並推企業微信)
      其餘 (預設 TW) -> LINE

    回傳主要通道結果 dict; 若 CN 額外發了企業微信, 以 "extra" 帶回其結果。
    任何例外皆被吞掉, 不影響主業務流程。
    """
    region = (region or "TW").upper()
    try:
        if region in ("CN", "CHINA"):
            result = await notify_dingtalk(message)
            # 企業微信為選配的並行通道; 有設定才嘗試
            if (getattr(settings, "wecom_webhook_url", "") or "").strip():
                wecom_result = await notify_wecom(message)
                result = dict(result)
                result["extra"] = wecom_result
            return result
        return await notify_line(message)
    except Exception as exc:  # 全面保護: 通知絕不影響主業務
        logger.warning("[notify_risk] 風險通知例外, 已忽略: %s", exc)
        return {"channel": "none", "sent": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# 區域感知的整合 hook (供 recalc 之後 best-effort 呼叫)
# ---------------------------------------------------------------------------
async def notify_schedule_update(project_out: Any, region: str | None = None) -> dict:
    """排程重算後的通知 hook (best-effort, 非阻斷)。

    路由規則:
      region == 'CN'  -> 釘釘 DingTalk (中國大陸)
      其餘 (預設 TW)  -> LINE (台灣)

    任何例外皆被吞掉, 確保不影響排程主流程; 回傳通道結果 dict。
    """
    # 優先使用傳入 region, 否則取 project_out.region, 最後預設 TW
    eff_region = (region or _get(project_out, "region", "TW") or "TW").upper()
    try:
        summary = build_schedule_summary(project_out, eff_region)
        if eff_region == "CN":
            return await notify_dingtalk(summary)
        return await notify_line(summary)
    except Exception as exc:  # 全面保護: 通知絕不影響主業務
        logger.warning("[notify_schedule_update] 通知 hook 例外, 已忽略: %s", exc)
        return {"channel": "none", "sent": False, "error": str(exc)}
