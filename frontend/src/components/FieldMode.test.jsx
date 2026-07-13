// Pro Batch C：FieldMode 行動現場回報 — smoke render 測試（mock store）
// 驗證：
//   1. 渲染標題 / 連線徽章 / 任務卡片（task_id + 名稱 + 完成度）
//   2. 點任務卡片開啟回報單（滑桿帶入現有完成度 + 載入該任務照片）
//   3. 線上送出：先「重新抓取」目前進度（loadProgress），以最新清單合併後
//      saveProgress —— 不得沿用選案當下的過期 progress 快照（防止其他任務
//      的伺服器端進度被舊快照悄悄蓋回）。
import React from 'react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent, waitFor } from '@testing-library/react'
import { t } from '../i18n/index.js'

// ---- mock store（hoisted 可變狀態物件，各測試於 beforeEach 重建）----
const { mockState } = vi.hoisted(() => ({ mockState: {} }))

vi.mock('../store/scheduleStore', () => {
  const useScheduleStore = () => mockState
  useScheduleStore.getState = () => mockState
  return {
    useScheduleStore,
    isLoading: () => false,
    getError: () => null,
  }
})

// 避免載入 axios 實體 client（FieldPhotoThumb 才會用到 photoUrl）。
vi.mock('../api/client', () => ({
  photoUrl: (id) => `/api/v1/photos/${id}`,
}))

import FieldMode from './FieldMode.jsx'

// 伺服器端「目前」進度（loadProgress 送出前重新抓取的結果）——
// 刻意與 store 內的過期快照 (mockState.progress) 不同，
// 供測試 3 驗證合併來源是「新抓的清單」而非快照。
const FRESH_PROGRESS = [
  { task_id: 'T1', budget: 10, percent_complete: 20, actual_cost: 5, actual_start_day: 2, actual_finish_day: null },
  { task_id: 'T2', budget: 7, percent_complete: 30, actual_cost: 1, actual_start_day: 1, actual_finish_day: null },
]

function buildState() {
  return {
    region: 'TW',
    role: 'admin',
    username: 'admin@tw',
    token: 'test-token',
    tenantId: 'TENT-9981',
    projects: [{ project_id: 'P1', project_name: '示範工程' }],
    currentProject: {
      project_id: 'P1',
      project_name: '示範工程',
      tasks: [
        { task_id: 'T1', task_name: '基礎開挖', status: 'IN_PROGRESS' },
        { task_id: 'T2', task_name: '鋼筋綁紮', status: 'PENDING' },
      ],
    },
    // 過期快照：只有 T2（且完成度 30%）；T1 不在快照內。
    progress: [
      { task_id: 'T2', budget: 7, percent_complete: 30, actual_cost: 1, actual_start_day: 1, actual_finish_day: null },
    ],
    photosByTask: {},
    fieldQueueCount: 0,
    logout: vi.fn(),
    loadProjects: vi.fn().mockResolvedValue([]),
    loadProject: vi.fn().mockResolvedValue(null),
    loadProgress: vi.fn().mockResolvedValue(FRESH_PROGRESS),
    saveProgress: vi.fn().mockResolvedValue([]),
    loadTaskPhotos: vi.fn().mockResolvedValue([]),
    uploadTaskPhoto: vi.fn().mockResolvedValue({}),
    deleteTaskPhoto: vi.fn().mockResolvedValue({}),
    refreshFieldQueueCount: vi.fn().mockResolvedValue([]),
    syncFieldQueue: vi.fn().mockResolvedValue({ ok: 0, failed: 0 }),
  }
}

beforeEach(() => {
  // 重建 mock 狀態（保留同一物件參照，vi.mock 工廠閉包才拿得到）
  Object.keys(mockState).forEach((k) => delete mockState[k])
  Object.assign(mockState, buildState())
})

describe('FieldMode smoke render', () => {
  it('renders header, online badge and task cards', async () => {
    const { findByText, getByText } = render(<FieldMode />)
    // 標題 + 連線徽章（用完整字串精準匹配：「退出現場模式」按鈕也含「現場模式」）
    expect(await findByText(`📱 ${t('TW', 'fieldMode')}`)).toBeInTheDocument()
    expect(getByText(`🟢 ${t('TW', 'online')}`)).toBeInTheDocument()
    // 任務卡片：task_id + 名稱
    expect(getByText('T1')).toBeInTheDocument()
    expect(getByText('基礎開挖')).toBeInTheDocument()
    expect(getByText('T2')).toBeInTheDocument()
    expect(getByText('鋼筋綁紮')).toBeInTheDocument()
  })

  it('opens the report sheet with the existing percent when a card is tapped', async () => {
    const { findByText, container } = render(<FieldMode />)
    fireEvent.click(await findByText('鋼筋綁紮'))
    // 回報單標題（fieldReport — task_id）
    expect(await findByText(`${t('TW', 'fieldReport')} — T2`)).toBeInTheDocument()
    // 滑桿帶入現有完成度（來自 store.progress 快照的 30%）
    const slider = container.querySelector('input[type="range"]')
    expect(slider).not.toBeNull()
    expect(slider.value).toBe('30')
    // 開啟回報單會載入該任務照片
    expect(mockState.loadTaskPhotos).toHaveBeenCalledWith('T2')
  })

  it('re-fetches the current progress list before merging on online submit', async () => {
    const { findByText } = render(<FieldMode />)
    fireEvent.click(await findByText('鋼筋綁紮'))
    await findByText(`${t('TW', 'fieldReport')} — T2`)
    const callsBefore = mockState.loadProgress.mock.calls.length
    fireEvent.click(await findByText(`✓ ${t('TW', 'submitReport')}`))
    await waitFor(() => expect(mockState.saveProgress).toHaveBeenCalledTimes(1))
    // 送出前必須重新抓取進度（而非沿用快照）
    expect(mockState.loadProgress.mock.calls.length).toBeGreaterThan(callsBefore)

    const merged = mockState.saveProgress.mock.calls[0][0]
    // 其他任務（T1）的進度列必須來自「新抓的清單」——快照內根本沒有 T1。
    const t1 = merged.find((p) => p.task_id === 'T1')
    expect(t1).toBeTruthy()
    expect(t1.percent_complete).toBe(20)
    expect(t1.budget).toBe(10)
    // 回報任務（T2）：未編輯欄位（budget / 實際起訖日）以新清單值補齊
    const t2 = merged.find((p) => p.task_id === 'T2')
    expect(t2).toBeTruthy()
    expect(t2.budget).toBe(7)
    expect(t2.actual_start_day).toBe(1)
    expect(t2.percent_complete).toBe(30)
    expect(t2.actual_cost).toBe(1)
  })
})
