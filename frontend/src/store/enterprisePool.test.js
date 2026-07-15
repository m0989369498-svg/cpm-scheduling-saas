// Pro Batch E：zustand store 單元測試 — 租戶層級資源池 (E1) + 投資組合資源分配 (E1)
// store 動作 (loadPool/savePool/loadAllocation) + 租戶層級狀態重置
// （logout/setTenant -> pool:[]/allocation:null；loadProject/createProject 不重置，因非專案層級資料）。
// 採用與 costHealth.test.js 相同的 vi.mock(api/client.js) 手法（避免真正發出 HTTP 請求）。
import { describe, it, expect, vi, beforeEach } from 'vitest'

vi.mock('../api/client.js', () => ({
  __esModule: true,
  default: {},
  login: vi.fn(),
  me: vi.fn(),
  calculateSchedule: vi.fn(),
  listProjects: vi.fn(),
  getProject: vi.fn(),
  createProject: vi.fn(),
  addTask: vi.fn(),
  updateTask: vi.fn(),
  updateTaskDuration: vi.fn(),
  deleteTask: vi.fn(),
  deleteProject: vi.fn(),
  getHolidays: vi.fn(),
  saveHolidays: vi.fn(),
  getTrash: vi.fn(),
  restoreProject: vi.fn(),
  purgeProject: vi.fn(),
  syncErp: vi.fn(),
  getResources: vi.fn(),
  setResources: vi.fn(),
  levelResources: vi.fn(),
  getRisk: vi.fn(),
  setRisk: vi.fn(),
  simulate: vi.fn(),
  reportUrl: vi.fn(),
  getProgress: vi.fn(),
  saveProgress: vi.fn(),
  createBaseline: vi.fn(),
  getBaseline: vi.fn(),
  getEvm: vi.fn(),
  dispatchEvmAlert: vi.fn(),
  getDashboard: vi.fn(),
  listUsers: vi.fn(),
  createUser: vi.fn(),
  updateUser: vi.fn(),
  deleteUser: vi.fn(),
  exportXlsxUrl: vi.fn(),
  exportPdfUrl: vi.fn(),
  getWbs: vi.fn(),
  saveWbs: vi.fn(),
  listBaselines: vi.fn(),
  getBaselineById: vi.fn(),
  activateBaseline: vi.fn(),
  deleteBaseline: vi.fn(),
  getCost: vi.fn(),
  getHealth: vi.fn(),
  getPool: vi.fn(),
  savePool: vi.fn(),
  getAllocation: vi.fn(),
}))

import * as api from '../api/client.js'
import { useScheduleStore, isLoading, getError } from './scheduleStore.js'

function sampleProject() {
  return {
    project_id: 'P1',
    project_name: 'Demo',
    version: 3,
    tasks: [{ task_id: 'T1', duration: 2, es: 0, ef: 2, float_time: 0, is_critical: true }],
  }
}

function samplePool() {
  return [
    { resource_type: 'crane', name: 'Tower Crane', category: 'equipment', capacity: 2, unit_cost: 3200, work_days: '1111100' },
    { resource_type: 'manpower', name: 'Manpower', category: 'labor', capacity: 40, unit_cost: 260, work_days: '1111110' },
  ]
}

function sampleAllocation() {
  return {
    weeks: ['2026-W01', '2026-W02'],
    resources: [
      {
        resource_type: 'crane',
        name: 'Tower Crane',
        category: 'equipment',
        capacity: 2,
        unit_cost: 3200,
        by_week: { '2026-W01': 3, '2026-W02': 1 },
        peak: 3,
        over_weeks: ['2026-W01'],
      },
    ],
    unscheduled_projects: ['P9'],
    warnings: ['P9 未設定開工日，已排除於分配計算'],
  }
}

beforeEach(() => {
  vi.clearAllMocks()
  useScheduleStore.setState({
    currentProject: null,
    tenantId: 'TENT-9981',
    loading: {},
    errors: {},
    loadingAny: false,
    pool: [],
    allocation: null,
  })
})

describe('store loadPool (Pro Batch E Feature 1)', () => {
  it('fetches the tenant resource pool and stores it under store.pool', async () => {
    const pool = samplePool()
    api.getPool.mockResolvedValue(pool)
    const result = await useScheduleStore.getState().loadPool()
    expect(api.getPool).toHaveBeenCalledWith()
    expect(result).toEqual(pool)
    const st = useScheduleStore.getState()
    expect(st.pool).toEqual(pool)
    expect(isLoading(st, 'pool')).toBe(false)
    expect(getError(st, 'pool')).toBeNull()
  })

  it('records the failure under errors.pool and leaves store.pool untouched on error', async () => {
    api.getPool.mockRejectedValue({ response: { data: { detail: 'pool boom' } } })
    await expect(useScheduleStore.getState().loadPool()).rejects.toBeTruthy()
    const st = useScheduleStore.getState()
    expect(getError(st, 'pool')).toBe('pool boom')
    expect(isLoading(st, 'pool')).toBe(false)
    expect(st.pool).toEqual([])
  })
})

describe('store savePool (Pro Batch E Feature 1)', () => {
  it('saves the pool (upsert by resource_type) and updates store.pool with the server response', async () => {
    const draft = [{ resource_type: 'crane', name: '', category: 'labor', capacity: 5, unit_cost: 0, work_days: '1111100' }]
    const saved = samplePool()
    api.savePool.mockResolvedValue(saved)
    const result = await useScheduleStore.getState().savePool(draft)
    expect(api.savePool).toHaveBeenCalledWith(draft)
    expect(result).toEqual(saved)
    const st = useScheduleStore.getState()
    expect(st.pool).toEqual(saved)
    expect(isLoading(st, 'pool')).toBe(false)
  })

  it('records the failure under errors.pool on error', async () => {
    api.savePool.mockRejectedValue({ response: { data: { detail: 'save boom' } } })
    await expect(useScheduleStore.getState().savePool([])).rejects.toBeTruthy()
    const st = useScheduleStore.getState()
    expect(getError(st, 'pool')).toBe('save boom')
    expect(isLoading(st, 'pool')).toBe(false)
  })
})

describe('store loadAllocation (Pro Batch E Feature 1)', () => {
  it('fetches the portfolio resource allocation and stores it under store.allocation', async () => {
    const allocation = sampleAllocation()
    api.getAllocation.mockResolvedValue(allocation)
    const result = await useScheduleStore.getState().loadAllocation()
    expect(api.getAllocation).toHaveBeenCalledWith()
    expect(result).toEqual(allocation)
    const st = useScheduleStore.getState()
    expect(st.allocation).toEqual(allocation)
    expect(isLoading(st, 'allocation')).toBe(false)
    expect(getError(st, 'allocation')).toBeNull()
  })

  it('records the failure under errors.allocation and leaves store.allocation untouched on error', async () => {
    api.getAllocation.mockRejectedValue({ response: { data: { detail: 'alloc boom' } } })
    await expect(useScheduleStore.getState().loadAllocation()).rejects.toBeTruthy()
    const st = useScheduleStore.getState()
    expect(getError(st, 'allocation')).toBe('alloc boom')
    expect(isLoading(st, 'allocation')).toBe(false)
    expect(st.allocation).toBeNull()
  })
})

describe('pool/allocation are tenant-level: reset on logout/setTenant, NOT on loadProject/createProject', () => {
  it('logout resets pool/allocation to []/null', () => {
    useScheduleStore.setState({
      currentProject: sampleProject(),
      pool: samplePool(),
      allocation: sampleAllocation(),
      token: 'tok',
      role: 'admin',
    })
    useScheduleStore.getState().logout()
    const st = useScheduleStore.getState()
    expect(st.pool).toEqual([])
    expect(st.allocation).toBeNull()
  })

  it('setTenant resets pool/allocation to []/null', () => {
    useScheduleStore.setState({ pool: samplePool(), allocation: sampleAllocation() })
    useScheduleStore.getState().setTenant('TENT-OTHER')
    const st = useScheduleStore.getState()
    expect(st.pool).toEqual([])
    expect(st.allocation).toBeNull()
  })

  it('loadProject does NOT reset pool/allocation (tenant-level, not project-level)', async () => {
    const pool = samplePool()
    const allocation = sampleAllocation()
    useScheduleStore.setState({ pool, allocation })
    api.getProject.mockResolvedValue(sampleProject())
    await useScheduleStore.getState().loadProject('P1')
    const st = useScheduleStore.getState()
    expect(st.pool).toEqual(pool)
    expect(st.allocation).toEqual(allocation)
  })

  it('createProject does NOT reset pool/allocation (tenant-level, not project-level)', async () => {
    const pool = samplePool()
    const allocation = sampleAllocation()
    useScheduleStore.setState({ pool, allocation })
    api.createProject.mockResolvedValue(sampleProject())
    api.listProjects.mockResolvedValue([])
    await useScheduleStore.getState().createProject({ project_name: 'X', tasks: [] })
    const st = useScheduleStore.getState()
    expect(st.pool).toEqual(pool)
    expect(st.allocation).toEqual(allocation)
  })
})
