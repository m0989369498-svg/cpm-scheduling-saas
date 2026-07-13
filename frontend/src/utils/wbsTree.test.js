// Pro Batch B · Feature 1：WBS 純函式輔助單元測試
// 涵蓋：buildWbsTree 巢狀 (nesting) / 孤兒節點歸根 (orphan -> root) /
// 同層排序 (sort_order + wbs_code tiebreaker)，以及 groupTasksByWbs
// 任務分組（含未分類 bucket 置於最後）。
import { describe, it, expect } from 'vitest'
import { buildWbsTree, groupTasksByWbs } from './wbsTree.js'

describe('buildWbsTree', () => {
  it('nests children under their parent_code', () => {
    const flat = [
      { wbs_code: '1', name: 'Phase 1', parent_code: null, sort_order: 0 },
      { wbs_code: '1.1', name: 'Design', parent_code: '1', sort_order: 0 },
      { wbs_code: '1.2', name: 'Build', parent_code: '1', sort_order: 1 },
    ]
    const tree = buildWbsTree(flat)
    expect(tree).toHaveLength(1)
    expect(tree[0].wbs_code).toBe('1')
    expect(tree[0].children.map((c) => c.wbs_code)).toEqual(['1.1', '1.2'])
  })

  it('treats a dangling parent_code (not present in the list) as a root', () => {
    const flat = [
      { wbs_code: 'A', name: 'Orphan', parent_code: 'DOES-NOT-EXIST', sort_order: 0 },
      { wbs_code: 'B', name: 'Real root', parent_code: null, sort_order: 1 },
    ]
    const tree = buildWbsTree(flat)
    const codes = tree.map((n) => n.wbs_code).sort()
    expect(codes).toEqual(['A', 'B'])
  })

  it('treats a self-referencing parent_code as a root (defensive against cycles)', () => {
    const flat = [{ wbs_code: 'X', name: 'Self', parent_code: 'X', sort_order: 0 }]
    const tree = buildWbsTree(flat)
    expect(tree).toHaveLength(1)
    expect(tree[0].wbs_code).toBe('X')
    expect(tree[0].children).toEqual([])
  })

  it('sorts siblings by sort_order, then by wbs_code when tied', () => {
    const flat = [
      { wbs_code: 'C', name: 'C', parent_code: null, sort_order: 5 },
      { wbs_code: 'A', name: 'A', parent_code: null, sort_order: 5 },
      { wbs_code: 'B', name: 'B', parent_code: null, sort_order: 1 },
    ]
    const tree = buildWbsTree(flat)
    expect(tree.map((n) => n.wbs_code)).toEqual(['B', 'A', 'C'])
  })
})

describe('groupTasksByWbs', () => {
  const wbs = [
    { wbs_code: '1', name: 'Phase 1', parent_code: null, sort_order: 0 },
    { wbs_code: '1.1', name: 'Design', parent_code: '1', sort_order: 0 },
  ]

  it('returns a flat task-only list when there are no WBS nodes', () => {
    const tasks = [{ task_id: 'T1' }, { task_id: 'T2' }]
    const rows = groupTasksByWbs(tasks, [], '未分類')
    expect(rows).toEqual([
      { type: 'task', task: tasks[0] },
      { type: 'task', task: tasks[1] },
    ])
  })

  it('buckets tasks under their matching WBS header in tree order', () => {
    const tasks = [
      { task_id: 'T1', wbs_code: '1.1' },
      { task_id: 'T2', wbs_code: '1' },
    ]
    const rows = groupTasksByWbs(tasks, wbs, '未分類')
    // 標頭順序：1（根）-> 1.1（子）；每個標頭之後接該碼底下的任務
    expect(rows.map((r) => (r.type === 'header' ? `H:${r.code}` : `T:${r.task.task_id}`))).toEqual([
      'H:1',
      'T:T2',
      'H:1.1',
      'T:T1',
    ])
  })

  it('places tasks with a missing/unknown wbs_code under an uncategorized group at the end', () => {
    const tasks = [
      { task_id: 'T1', wbs_code: '1' },
      { task_id: 'T2', wbs_code: null },
      { task_id: 'T3', wbs_code: 'NOT-A-REAL-CODE' },
    ]
    const rows = groupTasksByWbs(tasks, wbs, '未分類')
    const last3 = rows.slice(-3)
    expect(last3[0]).toEqual({ type: 'header', code: '__uncategorized__', name: '未分類', depth: 0 })
    expect(last3.slice(1).map((r) => r.task.task_id).sort()).toEqual(['T2', 'T3'])
  })
})
