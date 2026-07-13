"""工作日曆 (working calendar) —— 純函式，無 DB / 無 I/O。

將 CPM 的「日偏移 (day offset)」換算為實際日期 (real dates)：
  * work_days 為 7 碼字串，依序對應 週一..週日 (Mon..Sun)，'1'=工作日、'0'=休息日。
    營造業預設 '1111110' (週一至週六上工、週日休)。
  * holidays 為例外假日集合 (set[date])：即使落在工作日也跳過 (颱風假 / 國定假日)。
  * offset 0 = start_date 「當日或之後」的第一個工作日。
  * 防護 (guard)：work_days 不含任何 '1' (或長度 / 型別異常) 時視為「全週皆工作日」，
    避免在尋找下一個工作日時無窮迴圈。

Pure working-calendar helpers: map CPM day offsets onto calendar dates,
skipping non-workdays and explicit holidays.
"""

from __future__ import annotations

from datetime import date, timedelta

__all__ = ["offset_to_date", "day_dates", "date_to_offset"]


def _normalize_work_days(work_days: str) -> str:
    """正規化 work_days；異常 (非 7 碼 / 無任何 '1') 時退回全週工作日。

    Guard: a mask with no workday at all would make the scan below loop
    forever — treat it as "every day is a workday" instead.
    """
    if (
        not isinstance(work_days, str)
        or len(work_days) != 7
        or "1" not in work_days
    ):
        return "1111111"
    return work_days


def _is_workday(d: date, work_days: str, holidays: set[date]) -> bool:
    """是否為工作日：週型態 (work_days) 為 '1' 且不在例外假日 (holidays) 中。

    date.weekday()：Mon=0 .. Sun=6，與 work_days 的字元順序一致。
    """
    return work_days[d.weekday()] == "1" and d not in holidays


def day_dates(
    start_date: date,
    n_days: int,
    work_days: str,
    holidays: set[date] | None = None,
) -> list[date]:
    """回傳偏移 0..n_days 各自對應的日期清單 (長度 n_days+1)。

    索引 i 即「第 i 個工作日」的日期；offset 0 = start_date 當日或之後的
    第一個工作日。跳過非工作日 (work_days='0') 與例外假日 (holidays)。
    """
    hset = holidays or set()
    mask = _normalize_work_days(work_days)
    n = max(0, int(n_days))

    out: list[date] = []
    d = start_date
    while len(out) < n + 1:
        if _is_workday(d, mask, hset):
            out.append(d)
        d += timedelta(days=1)
    return out


def offset_to_date(
    start_date: date,
    offset: int,
    work_days: str,
    holidays: set[date] | None = None,
) -> date:
    """第 N 個工作日的日期 (offset 0 = start_date 當日或之後的第一個工作日)。

    與 day_dates 完全一致的語義：offset_to_date(s, k, ...) == day_dates(s, k, ...)[k]。
    負偏移以 0 視之 (CPM 的 es/ef 皆 >= 0；防衛性處理)。
    """
    return day_dates(start_date, max(0, int(offset)), work_days, holidays)[-1]


def date_to_offset(
    start_date: date,
    target_date: date,
    work_days: str,
    holidays: set[date] | None = None,
) -> int:
    """offset_to_date 的反函式：回傳 target_date 對應的日偏移 (day offset)。

    語義 (與 offset_to_date 精確互逆)：
      * target_date <= start_date 時夾在 0 (clamp below start to 0)。
      * target_date 落在非工作日 (週末 / 例外假日) 時，視同其後第一個工作日
        (use the NEXT working day)：回傳該工作日的 offset。
      * 對任意 k >= 0，date_to_offset(s, offset_to_date(s, k, wd, hol), wd, hol) == k
        (精確互逆 / exact inverse)。

    採用與 day_dates 相同的線性掃描策略 (逐日前進)，避免與其語義出現偏差。
    """
    hset = holidays or set()
    mask = _normalize_work_days(work_days)
    if target_date <= start_date:
        return 0

    offset = -1
    d = start_date
    while True:
        if _is_workday(d, mask, hset):
            offset += 1
            if d >= target_date:
                return offset
        d += timedelta(days=1)
