// Pro Batch F1 — workcalLite.js（純 JS 工作日曆）單元測試。
// 對照 fixtures/project__PRJ_2026_TW_PARALLEL.json 的 start_date/work_days/
// project_duration（2026-03-02 為週一，work_days '1111110' = 週一~週六工作）。
import { describe, it, expect } from 'vitest'
import { dayDates, offsetToDate } from './workcalLite.js'

describe('dayDates — 基本工作日推算', () => {
  it('全週皆工作日：offset 0..3 為連續日期', () => {
    const dates = dayDates('2026-03-02', 3, '1111111', [])
    expect(dates).toEqual(['2026-03-02', '2026-03-03', '2026-03-04', '2026-03-05'])
  })

  it('週日休息（1111110）：跳過週日', () => {
    // 2026-03-02 週一 .. 2026-03-08 週日：offset 5 應落在週六 03-07（週日 03-08 被跳過）
    const dates = dayDates('2026-03-02', 5, '1111110', [])
    expect(dates).toEqual(['2026-03-02', '2026-03-03', '2026-03-04', '2026-03-05', '2026-03-06', '2026-03-07'])
  })

  it('holidays 例外假日：即使落在工作日仍跳過', () => {
    const dates = dayDates('2026-03-02', 2, '1111111', ['2026-03-03'])
    expect(dates).toEqual(['2026-03-02', '2026-03-04', '2026-03-05'])
  })

  it('start_date 本身為非工作日時，offset 0 順延至下一個工作日', () => {
    // 2026-03-08 為週日 -> work_days 1111110 跳過 -> offset0 = 03-09（週一）
    const dates = dayDates('2026-03-08', 0, '1111110', [])
    expect(dates).toEqual(['2026-03-09'])
  })

  it('work_days 全為 0（或型別異常）時視為全週皆工作日（防護）', () => {
    const dates = dayDates('2026-03-02', 1, '0000000', [])
    expect(dates).toEqual(['2026-03-02', '2026-03-03'])
    const dates2 = dayDates('2026-03-02', 1, null, [])
    expect(dates2).toEqual(['2026-03-02', '2026-03-03'])
  })
})

describe('offsetToDate', () => {
  it('等同 dayDates(...) 的最後一筆', () => {
    expect(offsetToDate('2026-03-02', 5, '1111110', [])).toBe('2026-03-07')
  })

  it('負值 offset 視為 0', () => {
    expect(offsetToDate('2026-03-02', -3, '1111111', [])).toBe('2026-03-02')
  })
})

describe('dayDates — PARALLEL fixture 專案總工期覆蓋範圍', () => {
  it('project_duration=12 天可完整推算日期清單且不拋錯', () => {
    const dates = dayDates('2026-03-02', 12, '1111110', [])
    expect(dates.length).toBe(13)
    expect(dates[0]).toBe('2026-03-02')
  })
})
