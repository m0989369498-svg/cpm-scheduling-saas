// Pro Batch F1 — cpmLite.js（純 JS CPM 引擎）單元測試。
// 對照 fixtures/project__PRJ_2026_TW_PARALLEL.json 已知的 es/ef/ls/lf/float_time
// 快照值（後端 calculate_cpm 擷取當下的真實結果），並補上 SS+lag 與限制條件案例。
import { describe, it, expect } from 'vitest'
import { calculateCpm, projectDuration, criticalPath } from './cpmLite.js'
import parallelProject from './fixtures/project__PRJ_2026_TW_PARALLEL.json'

describe('calculateCpm — PARALLEL fixture 已知快照值', () => {
  const results = calculateCpm(parallelProject.tasks)

  it('每個任務的 es/ef/ls/lf/float_time/is_critical 與擷取快照一致', () => {
    for (const expected of parallelProject.tasks) {
      const actual = results[expected.task_id]
      expect(actual).toBeTruthy()
      expect(actual.es).toBe(expected.es)
      expect(actual.ef).toBe(expected.ef)
      expect(actual.ls).toBe(expected.ls)
      expect(actual.lf).toBe(expected.lf)
      expect(actual.float_time).toBe(expected.float_time)
      expect(actual.is_critical).toBe(expected.is_critical)
      expect(actual.constraint_violated).toBe(expected.constraint_violated)
    }
  })

  it('projectDuration 等於 fixture 的 project_duration', () => {
    expect(projectDuration(results)).toBe(parallelProject.project_duration)
  })

  it('criticalPath 依 es/ef/task_id 排序，命中 PA0->PA1->PA2->PF', () => {
    expect(criticalPath(results)).toEqual(['PA0', 'PA1', 'PA2', 'PF'])
  })
})

describe('calculateCpm — SS + lag_days', () => {
  it('SS+2：後繼 es 至少等於前置 es + lag（B 的 es 為 max(0, A.es+2)）', () => {
    const tasks = [
      { task_id: 'A', duration: 5, links: [] },
      {
        task_id: 'B',
        duration: 3,
        links: [{ predecessor_task_id: 'A', dep_type: 'SS', lag_days: 2 }],
      },
    ]
    const results = calculateCpm(tasks)
    expect(results.A.es).toBe(0)
    expect(results.A.ef).toBe(5)
    // SS+2：B 最早可於 A 開始後第 2 天開始 -> es=2, ef=5
    expect(results.B.es).toBe(2)
    expect(results.B.ef).toBe(5)
  })
})

describe('calculateCpm — 活動限制 (activity constraint)', () => {
  it('SNET（不得早於指定日開始）將 es 推遲至 constraint_day', () => {
    const tasks = [
      { task_id: 'A', duration: 3, links: [], constraint_type: 'SNET', constraint_day: 10 },
    ]
    const results = calculateCpm(tasks)
    expect(results.A.es).toBe(10)
    expect(results.A.ef).toBe(13)
  })

  it('FNLT（不得晚於指定日完成）造成 ls 收緊，float 可能為負 (constraint_violated)', () => {
    const tasks = [
      { task_id: 'A', duration: 5, links: [], constraint_type: 'FNLT', constraint_day: 2 },
    ]
    const results = calculateCpm(tasks)
    // es/ef 不受 FNLT 影響 (只影響後向掃描)：es=0, ef=5
    expect(results.A.es).toBe(0)
    expect(results.A.ef).toBe(5)
    // lf 被壓到 2，ls = 2-5 = -3，float = ls-es = -3 < 0 -> violated
    expect(results.A.lf).toBe(2)
    expect(results.A.ls).toBe(-3)
    expect(results.A.float_time).toBe(-3)
    expect(results.A.constraint_violated).toBe(true)
  })
})

describe('calculateCpm — 錯誤情境', () => {
  it('循環相依 (cycle) 拋出 Error', () => {
    const tasks = [
      { task_id: 'A', duration: 1, links: [{ predecessor_task_id: 'B', dep_type: 'FS', lag_days: 0 }] },
      { task_id: 'B', duration: 1, links: [{ predecessor_task_id: 'A', dep_type: 'FS', lag_days: 0 }] },
    ]
    expect(() => calculateCpm(tasks)).toThrow()
  })

  it('未知的前置任務拋出 Error', () => {
    const tasks = [{ task_id: 'A', duration: 1, predecessors: ['NOPE'] }]
    expect(() => calculateCpm(tasks)).toThrow()
  })

  it('空輸入回傳空物件', () => {
    expect(calculateCpm([])).toEqual({})
    expect(calculateCpm(null)).toEqual({})
  })
})
