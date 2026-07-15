// Pro Batch F1 — 純 JS 版工作日曆，忠實移植自 backend/app/core/workcal.py 的
// day_dates（見該檔文件字串）。供 demo/mockApi.js 在瀏覽器端重算 CPM 後，
// 重建 ProjectOut.day_dates（日偏移 -> 實際日期），與後端語義一致：
//   * work_days 為 7 碼字串，依序對應 週一..週日 (Mon..Sun)，'1'=工作日、'0'=休息日。
//   * holidays 為例外假日（'YYYY-MM-DD' 字串或 Date），即使落在工作日也跳過。
//   * offset 0 = start_date 當日或之後的第一個工作日。
//   * 防護：work_days 不含任何 '1'（或型別/長度異常）時視為「全週皆工作日」。
//
// 全程以 UTC 日期運算（避免瀏覽器所在時區造成 DST / 日界線偏移），
// 輸出一律為 'YYYY-MM-DD' 字串（與 fixtures / 後端 JSON 序列化一致）。

function normalizeWorkDays(workDays) {
  if (typeof workDays !== 'string' || workDays.length !== 7 || !workDays.includes('1')) {
    return '1111111'
  }
  return workDays
}

// 'YYYY-MM-DD' -> UTC 午夜 Date
function parseUtcDate(dateStr) {
  if (dateStr instanceof Date) {
    return new Date(Date.UTC(dateStr.getUTCFullYear(), dateStr.getUTCMonth(), dateStr.getUTCDate()))
  }
  const [y, m, d] = String(dateStr).split('-').map(Number)
  return new Date(Date.UTC(y, (m || 1) - 1, d || 1))
}

// UTC Date -> 'YYYY-MM-DD'
function formatUtcDate(d) {
  return d.toISOString().slice(0, 10)
}

// JS getUTCDay()：Sun=0..Sat=6 -> Python weekday()：Mon=0..Sun=6
function pyWeekday(d) {
  return (d.getUTCDay() + 6) % 7
}

function isWorkday(d, mask, holidaySet) {
  return mask[pyWeekday(d)] === '1' && !holidaySet.has(formatUtcDate(d))
}

// 回傳偏移 0..nDays 各自對應的日期清單（長度 nDays+1，'YYYY-MM-DD' 字串）。
export function dayDates(startDate, nDays, workDays, holidays = []) {
  const hset = new Set((holidays || []).map((h) => (h instanceof Date ? formatUtcDate(h) : String(h))))
  const mask = normalizeWorkDays(workDays)
  const n = Math.max(0, Math.trunc(Number(nDays) || 0))

  const out = []
  let d = parseUtcDate(startDate)
  while (out.length < n + 1) {
    if (isWorkday(d, mask, hset)) out.push(formatUtcDate(d))
    d = new Date(d.getTime() + 86400000)
  }
  return out
}

// 第 N 個工作日的日期（offset 0 = start_date 當日或之後的第一個工作日）。
// offsetToDate(s, k, ...) === dayDates(s, k, ...)[k]（與 day_dates 語義一致）。
export function offsetToDate(startDate, offset, workDays, holidays = []) {
  return dayDates(startDate, Math.max(0, Number(offset) || 0), workDays, holidays).slice(-1)[0]
}
