// Pro Batch C — 離線佇列單元測試 (frontend/src/offline/fieldQueue.js)
//
// 測試環境為 vitest + jsdom；jsdom 未實作 indexedDB（typeof indexedDB === 'undefined'），
// 因此以下測試天然地涵蓋「記憶體內備援」路徑，符合規格要求。
import { describe, it, expect, beforeEach } from 'vitest'
import { enqueue, listPending, remove, replay, __testing } from './fieldQueue.js'

beforeEach(() => {
  __testing.resetMemoryStoreForTests()
})

describe('fieldQueue — in-memory fallback (jsdom has no indexedDB)', () => {
  it('confirms this environment lacks indexedDB, so the in-memory path is what is under test', () => {
    expect(__testing.hasIndexedDb()).toBe(false)
  })

  it('enqueue assigns an incrementing id and stores the item fields', async () => {
    const stored = await enqueue({
      type: 'progress',
      projectId: 'P1',
      taskId: 'T1',
      payload: { percent_complete: 50 },
    })
    expect(stored.id).toBeTypeOf('number')
    expect(stored.type).toBe('progress')
    expect(stored.projectId).toBe('P1')
    expect(stored.taskId).toBe('T1')
    expect(stored.payload).toEqual({ percent_complete: 50 })
    expect(stored.queuedAt).toBeTypeOf('number')
  })

  it('listPending returns an empty array when nothing has been queued', async () => {
    expect(await listPending()).toEqual([])
  })

  it('listPending returns items in FIFO order regardless of insertion timing jitter', async () => {
    const a = await enqueue({ type: 'progress', projectId: 'P1', taskId: 'T1', payload: {}, queuedAt: 100 })
    const b = await enqueue({ type: 'progress', projectId: 'P1', taskId: 'T2', payload: {}, queuedAt: 200 })
    const c = await enqueue({ type: 'photo', projectId: 'P1', taskId: 'T3', payload: {}, queuedAt: 300 })

    const pending = await listPending()
    expect(pending.map((it) => it.id)).toEqual([a.id, b.id, c.id])
    expect(pending.map((it) => it.taskId)).toEqual(['T1', 'T2', 'T3'])
  })

  it('remove deletes a single item by id and leaves the rest untouched', async () => {
    const a = await enqueue({ type: 'progress', projectId: 'P1', taskId: 'T1', payload: {} })
    const b = await enqueue({ type: 'progress', projectId: 'P1', taskId: 'T2', payload: {} })

    await remove(a.id)

    const pending = await listPending()
    expect(pending).toHaveLength(1)
    expect(pending[0].id).toBe(b.id)
  })

  it('replay processes all items successfully, removing each and reporting {ok, failed:0}', async () => {
    await enqueue({ type: 'progress', projectId: 'P1', taskId: 'T1', payload: { percent_complete: 10 } })
    await enqueue({ type: 'progress', projectId: 'P1', taskId: 'T2', payload: { percent_complete: 20 } })
    await enqueue({ type: 'photo', projectId: 'P1', taskId: 'T3', payload: { note: 'ok' } })

    const processedOrder = []
    const result = await replay({
      progress: async (item) => {
        processedOrder.push(item.taskId)
      },
      photo: async (item) => {
        processedOrder.push(item.taskId)
      },
    })

    expect(result).toEqual({ ok: 3, failed: 0 })
    expect(processedOrder).toEqual(['T1', 'T2', 'T3']) // FIFO 呼叫順序
    expect(await listPending()).toEqual([])
  })

  it('replay stops on the first failure and keeps that item plus all later items queued (stop-and-keep)', async () => {
    const first = await enqueue({ type: 'progress', projectId: 'P1', taskId: 'T1', payload: {} })
    const second = await enqueue({ type: 'progress', projectId: 'P1', taskId: 'T2', payload: {} })
    const third = await enqueue({ type: 'progress', projectId: 'P1', taskId: 'T3', payload: {} })

    const attempted = []
    const result = await replay({
      progress: async (item) => {
        attempted.push(item.taskId)
        if (item.taskId === 'T2') {
          throw new Error('network down')
        }
      },
    })

    expect(result).toEqual({ ok: 1, failed: 2 })
    expect(attempted).toEqual(['T1', 'T2']) // 從未嘗試處理 T3，因為在 T2 就停止了
    const remaining = await listPending()
    expect(remaining.map((it) => it.id)).toEqual([second.id, third.id])
  })

  it('replay treats a missing handler for an item type as a stopping failure', async () => {
    await enqueue({ type: 'progress', projectId: 'P1', taskId: 'T1', payload: {} })
    await enqueue({ type: 'photo', projectId: 'P1', taskId: 'T2', payload: {} })

    const result = await replay({
      progress: async () => {},
      // 'photo' handler 故意缺席
    })

    expect(result).toEqual({ ok: 1, failed: 1 })
    const remaining = await listPending()
    expect(remaining).toHaveLength(1)
    expect(remaining[0].taskId).toBe('T2')
  })

  it('replay on an empty queue is a no-op returning {ok:0, failed:0}', async () => {
    const result = await replay({ progress: async () => {} })
    expect(result).toEqual({ ok: 0, failed: 0 })
  })
})
