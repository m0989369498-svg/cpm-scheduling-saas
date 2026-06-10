"""自動化模組 (Automation package).

包含:
  - reports:     使用 reportlab 產生工期報表 PDF (Gantt/CPM 結果表)。
  - notifications: 透過 LINE (台灣) / 釘釘 DingTalk (中國大陸) 推播排程摘要。

對外公開的主要函式:
  - generate_schedule_pdf(project_out, region) -> bytes
  - notify_line(message) / notify_dingtalk(message)
  - build_schedule_summary(project_out, region) -> str
  - notify_schedule_update(project_out, region) -> dict   (依區域選擇通道, best-effort)
"""

from app.automation.reports import generate_schedule_pdf
from app.automation.notifications import (
    notify_line,
    notify_dingtalk,
    build_schedule_summary,
    notify_schedule_update,
)

__all__ = [
    "generate_schedule_pdf",
    "notify_line",
    "notify_dingtalk",
    "build_schedule_summary",
    "notify_schedule_update",
]
