"""HTTP 標頭工具（HTTP header utilities）。

safe_filename：供 Content-Disposition 標頭使用的檔名消毒。

背景：多個匯出端點（exports.py / interop.py / projects.py / tasks.py qr.png）
以使用者可控的 project_id / task_id 內插組出 `filename="..."`。這些 id 為一般
字串欄位（無 charset 限制），若含 `"` 可跳出 quoted-string、含 CR/LF 則會在
標頭層級造成錯誤（h11 會拒絕非法標頭而使請求 500）。此 helper 以白名單字元
（ASCII 英數 + `.` `_` `-`）取代其餘字元，徹底杜絕標頭注入 / 跳脫，
且對既有合法檔名（`export_P1.xlsx`、`PRJ-001.xer`…）完全不變。
"""

from __future__ import annotations

import re

# 白名單以外的連續字元一律折疊為單一底線。
_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._-]+")

# 防止極端長 id 造出超長標頭；120 字元對人類可讀檔名綽綽有餘。
_MAX_FILENAME_LEN = 120


def safe_filename(name: str, fallback: str = "file") -> str:
    """消毒後回傳可安全放入 Content-Disposition quoted-string 的檔名。

    僅保留 ASCII 英數與 `.` `_` `-`；其餘（含引號、CR/LF、控制字元、非 ASCII）
    以底線取代。消毒後為空字串時回傳 fallback。
    """
    cleaned = _UNSAFE_FILENAME_CHARS.sub("_", name or "")[:_MAX_FILENAME_LEN]
    return cleaned or fallback
