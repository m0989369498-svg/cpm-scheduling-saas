"""工期報表 PDF 產生器 (Schedule Report Generator).

使用 reportlab 將 CPM 計算結果輸出為 PDF。報表包含:
  - 標題 (i18n reportTitle) 與專案基本資訊 (專案名稱 / 區域 / 工期)。
  - 任務明細表: task_id, task_name, duration, es, ef, float, critical。
  - 要徑 (Critical Path / 關鍵路徑) 以紅色標示。

公開 API:
  generate_schedule_pdf(project_out, region) -> bytes

設計重點:
  * 純函式, 不碰 DB。
  * project_out 可為 Pydantic ProjectOut 物件或等價 dict, 皆能處理。
  * 不依賴系統中文字型: reportlab 內建 Helvetica 對中文支援有限, 因此
    嘗試註冊 CID 字型 (STSong-Light) 以正確顯示中文; 若環境缺字型則
    回退英文鍵值, 確保「永遠可產生 PDF」而不致拋例外。
"""

from __future__ import annotations

import io
from typing import Any, Iterable

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    SimpleDocTemplate,
    Table,
    TableStyle,
    Paragraph,
    Spacer,
)

from app.core.i18n import t


# ---------------------------------------------------------------------------
# 中文字型註冊 (best-effort)
# ---------------------------------------------------------------------------
# reportlab 提供 Adobe 亞洲語系 CID 字型, 不需外部字型檔即可顯示中日韓文字。
# STSong-Light 為簡/繁中文常用之內建 CID 字型。註冊失敗 (極少數環境) 時
# 退回 Helvetica, 報表仍可產出 (中文可能顯示為方框, 但流程不中斷)。
_CJK_FONT_NAME = "Helvetica"  # 預設值; 註冊成功後改為 CID 字型名稱


def _register_cjk_font() -> str:
    """嘗試註冊內建 CID 中文字型, 回傳可用之字型名稱。"""
    global _CJK_FONT_NAME
    if _CJK_FONT_NAME != "Helvetica":
        return _CJK_FONT_NAME
    try:
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont

        font_name = "STSong-Light"
        pdfmetrics.registerFont(UnicodeCIDFont(font_name))
        _CJK_FONT_NAME = font_name
    except Exception:  # pragma: no cover - 視執行環境而定
        _CJK_FONT_NAME = "Helvetica"
    return _CJK_FONT_NAME


# ---------------------------------------------------------------------------
# project_out 欄位存取輔助 (容忍 Pydantic 物件 / dict 兩種型態)
# ---------------------------------------------------------------------------
def _get(obj: Any, key: str, default: Any = None) -> Any:
    """從 Pydantic 物件或 dict 取值。"""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _iter_tasks(project_out: Any) -> Iterable[Any]:
    tasks = _get(project_out, "tasks", []) or []
    return tasks


def _is_critical(task: Any) -> bool:
    """要徑判定: is_critical 為真, 或 float_time 為 0。"""
    if bool(_get(task, "is_critical", False)):
        return True
    return int(_get(task, "float_time", 0) or 0) == 0


# ---------------------------------------------------------------------------
# 主函式
# ---------------------------------------------------------------------------
def generate_schedule_pdf(project_out: Any, region: str = "TW") -> bytes:
    """產生工期報表 PDF, 回傳 bytes (供 StreamingResponse 使用)。

    參數:
      project_out: ProjectOut 物件或等價 dict, 需含 project_name / region /
                   project_duration / tasks(list[TaskResult])。
      region:      'TW' 或 'CN', 決定 i18n 標籤語系。
    """
    region = (region or "TW").upper()
    font = _register_cjk_font()

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        title=t(region, "reportTitle"),
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
    )

    # ---- 樣式 ----
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "CPMTitle",
        parent=styles["Title"],
        fontName=font,
        fontSize=18,
        leading=22,
        spaceAfter=6,
    )
    meta_style = ParagraphStyle(
        "CPMMeta",
        parent=styles["Normal"],
        fontName=font,
        fontSize=10,
        leading=14,
        textColor=colors.HexColor("#555555"),
    )
    cell_style = ParagraphStyle(
        "CPMCell",
        parent=styles["Normal"],
        fontName=font,
        fontSize=9,
        leading=11,
    )

    story: list[Any] = []

    # ---- 標題 ----
    story.append(Paragraph(t(region, "reportTitle"), title_style))

    # ---- 專案資訊列 ----
    project_name = _get(project_out, "project_name", "") or ""
    project_id = _get(project_out, "project_id", "") or ""
    proj_region = _get(project_out, "region", region) or region
    duration = int(_get(project_out, "project_duration", 0) or 0)

    meta_bits = [
        f"{t(region, 'project')}: {project_name} ({project_id})",
        f"{t(region, 'region')}: {proj_region}",
        f"{t(region, 'projectDuration')}: {duration} {t(region, 'days')}",
    ]
    story.append(Paragraph("&nbsp;&nbsp;|&nbsp;&nbsp;".join(meta_bits), meta_style))
    story.append(Spacer(1, 8 * mm))

    # ---- 任務明細表 ----
    header = [
        Paragraph(t(region, "taskId"), cell_style),
        Paragraph(t(region, "taskName"), cell_style),
        Paragraph(t(region, "duration"), cell_style),
        Paragraph("ES", cell_style),
        Paragraph("EF", cell_style),
        Paragraph(t(region, "floatTime"), cell_style),
        Paragraph(t(region, "critical"), cell_style),
    ]

    rows: list[list[Any]] = [header]
    critical_row_indexes: list[int] = []

    for idx, task in enumerate(_iter_tasks(project_out), start=1):
        crit = _is_critical(task)
        if crit:
            critical_row_indexes.append(idx)
        # 要徑標記: 中文以「是 / 否」呈現, 並加上火焰符號強調
        crit_label = "🔥 " + ("是" if crit else "")
        crit_text = "🔥" if crit else "-"
        rows.append(
            [
                Paragraph(str(_get(task, "task_id", "")), cell_style),
                Paragraph(str(_get(task, "task_name", "") or ""), cell_style),
                Paragraph(str(int(_get(task, "duration", 0) or 0)), cell_style),
                Paragraph(str(int(_get(task, "es", 0) or 0)), cell_style),
                Paragraph(str(int(_get(task, "ef", 0) or 0)), cell_style),
                Paragraph(str(int(_get(task, "float_time", 0) or 0)), cell_style),
                Paragraph(crit_text, cell_style),
            ]
        )

    # 欄寬 (A4 可用寬度約 174mm)
    col_widths = [
        22 * mm,  # task_id
        54 * mm,  # task_name
        18 * mm,  # duration
        16 * mm,  # es
        16 * mm,  # ef
        24 * mm,  # float
        24 * mm,  # critical
    ]
    table = Table(rows, colWidths=col_widths, repeatRows=1)

    style_cmds = [
        # 表頭
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2c3e50")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, -1), font),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (2, 0), (-1, -1), "CENTER"),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#bdc3c7")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f5f7fa")]),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]
    # 要徑列以紅底白字標示 (關鍵路徑高亮)
    for r in critical_row_indexes:
        style_cmds.append(("BACKGROUND", (0, r), (-1, r), colors.HexColor("#e74c3c")))
        style_cmds.append(("TEXTCOLOR", (0, r), (-1, r), colors.white))

    table.setStyle(TableStyle(style_cmds))
    story.append(table)
    story.append(Spacer(1, 8 * mm))

    # ---- 要徑摘要 ----
    crit_ids = [
        str(_get(tk, "task_id", ""))
        for tk in _iter_tasks(project_out)
        if _is_critical(tk)
    ]
    crit_summary = " → ".join(crit_ids) if crit_ids else "-"
    story.append(
        Paragraph(
            f"<b>{t(region, 'criticalPath')}:</b> {crit_summary}",
            ParagraphStyle(
                "CritSummary",
                parent=cell_style,
                textColor=colors.HexColor("#c0392b"),
                fontSize=10,
                leading=14,
            ),
        )
    )

    doc.build(story)
    pdf_bytes = buf.getvalue()
    buf.close()
    return pdf_bytes
