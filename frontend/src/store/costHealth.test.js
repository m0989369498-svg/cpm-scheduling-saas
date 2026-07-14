// Pro Batch D：zustand store 單元測試 — 資源成本負荷 (D1) + DCMA 14 點排程健康度 (D2)
// store 動作 (loadCost/loadHealth) + per-project 狀態重置 (loadProject 切換時 cost/health -> null)。
// 採用與 wbsBaselines.test.js 相同的 vi.mock(api/client.js) 手法（避免真正發出 HTTP 請求）。
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

function sampleCost() {
  return {
    total_cost: 4200,
    by_resource: { crane: 3000, manpower: 1200 },
    by_category: { equipment: 3000, labor: 1200 },
    by_wbs: { '1.1': 4200 },
    per_task: [{ task_id: 'T1', task_name: 'Dig', duration: 2, cost: 4200, per_resource: { crane: 3000 } }],
    cost_curve: [
      { day: 0, cost: 2100, cumulative: 2100 },
      { day: 1, cost: 2100, cumulative: 4200 },
    ],
  }
}

function sampleHealth() {
  return {
    data_date: 2,
    checks: [
      { key: 'logic', name: 'Logic', name_cn: '邏輯遺漏', value: 0, threshold: 0.05, comparison: 'lte', count: 0, total: 1, passed: true, detail: [] },
    ],
    score: 1.0,
    passed_count: 1,
    applicable_count: 1,
    total_count: 14,
  }
}

beforeEach(() => {
  vi.clearAllMocks()
  useScheduleStore.setState({
    currentProject: null,
    loading: {},
    errors: {},
    loadingAny: false,
    cost: null,
    health: null,
  })
})

describe('store loadCost (Pro Batch D Feature 1)', () => {
  it('is a no-op returning null when there is no current project', async () => {
    const result = await useScheduleStore.getState().loadCost()
    expect(result).toBeNull()
    expect(api.getCost).not.toHaveBeenCalled()
  })

  it('fetches CostResult and stores it under store.cost', async () => {
    useScheduleStore.setState({ currentProject: sampleProject() })
    const cost = sampleCost()
    api.getCost.mockResolvedValue(cost)
    const result = await useScheduleStore.getState().loadCost()
    expect(api.getCost).toHaveBeenCalledWith('P1')
    expect(result).toEqual(cost)
    const st = useScheduleStore.getState()
    expect(st.cost).toEqual(cost)
    expect(isLoading(st, 'cost')).toBe(false)
    expect(getError(st, 'cost')).toBeNull()
  })

  it('records the failure under errors.cost and leaves store.cost untouched on error', async () => {
    useScheduleStore.setState({ currentProject: sampleProject() })
    api.getCost.mockRejectedValue({ response: { data: { detail: 'boom' } } })
    await expect(useScheduleStore.getState().loadCost()).rejects.toBeTruthy()
    const st = useScheduleStore.getState()
    expect(getError(st, 'cost')).toBe('boom')
    expect(isLoading(st, 'cost')).toBe(false)
    expect(st.cost).toBeNull()
  })
})

describe('store loadHealth (Pro Batch D Feature 2)', () => {
  it('is a no-op returning null when there is no current project', async () => {
    const result = await useScheduleStore.getState().loadHealth()
    expect(result).toBeNull()
    expect(api.getHealth).not.toHaveBeenCalled()
  })

  it('fetches DcmaReport (default data_date) and stores it under store.health', async () => {
    useScheduleStore.setState({ currentProject: sampleProject() })
    const health = sampleHealth()
    api.getHealth.mockResolvedValue(health)
    const result = await useScheduleStore.getState().loadHealth()
    expect(api.getHealth).toHaveBeenCalledWith('P1', undefined)
    expect(result).toEqual(health)
    const st = useScheduleStore.getState()
    expect(st.health).toEqual(health)
    expect(isLoading(st, 'health')).toBe(false)
  })

  it('passes an explicit dataDate through to api.getHealth', async () => {
    useScheduleStore.setState({ currentProject: sampleProject() })
    api.getHealth.mockResolvedValue(sampleHealth())
    await useScheduleStore.getState().loadHealth(5)
    expect(api.getHealth).toHaveBeenCalledWith('P1', 5)
  })

  it('records the failure under errors.health on error', async () => {
    useScheduleStore.setState({ currentProject: sampleProject() })
    api.getHealth.mockRejectedValue({ response: { data: { detail: 'health boom' } } })
    await expect(useScheduleStore.getState().loadHealth()).rejects.toBeTruthy()
    const st = useScheduleStore.getState()
    expect(getError(st, 'health')).toBe('health boom')
    expect(isLoading(st, 'health')).toBe(false)
  })
})

describe('cost/health state reset on project switch / logout / setTenant', () => {
  it('loadProject resets stale cost/health before fetching the new project', async () => {
    useScheduleStore.setState({ cost: sampleCost(), health: sampleHealth() })
    api.getProject.mockResolvedValue(sampleProject())
    await useScheduleStore.getState().loadProject('P1')
    const st = useScheduleStore.getState()
    expect(st.cost).toBeNull()
    expect(st.health).toBeNull()
  })

  it('createProject resets stale cost/health', async () => {
    useScheduleStore.setState({ cost: sampleCost(), health: sampleHealth() })
    api.createProject.mockResolvedValue(sampleProject())
    api.listProjects.mockResolvedValue([])
    await useScheduleStore.getState().createProject({ project_name: 'X', tasks: [] })
    const st = useScheduleStore.getState()
    expect(st.cost).toBeNull()
    expect(st.health).toBeNull()
  })

  it('logout resets cost/health to null', () => {
    useScheduleStore.setState({
      currentProject: sampleProject(),
      cost: sampleCost(),
      health: sampleHealth(),
      token: 'tok',
      role: 'admin',
    })
    useScheduleStore.getState().logout()
    const st = useScheduleStore.getState()
    expect(st.cost).toBeNull()
    expect(st.health).toBeNull()
  })

  it('setTenant resets cost/health to null', () => {
    useScheduleStore.setState({ cost: sampleCost(), health: sampleHealth() })
    useScheduleStore.getState().setTenant('TENT-OTHER')
    const st = useScheduleStore.getState()
    expect(st.cost).toBeNull()
    expect(st.health).toBeNull()
  })
})
