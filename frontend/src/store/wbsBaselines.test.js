// Pro Batch B：zustand store 單元測試 — WBS 階層 store 動作
// (loadWbs/saveWbs) + 多重命名基準線 store 動作 (loadBaselines/activateBaseline/
// deleteBaseline/createBaseline 背景刷新) + updateTaskLinks 併帶 constraint extra。
// 採用與 scheduleStore.test.js 相同的 vi.mock(api/client.js) 手法（避免真正發出 HTTP 請求）。
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

beforeEach(() => {
  vi.clearAllMocks()
  useScheduleStore.setState({
    currentProject: null,
    loading: {},
    errors: {},
    loadingAny: false,
    wbs: [],
    baselines: [],
    baseline: null,
  })
})

describe('store WBS actions', () => {
  it('loadWbs is a no-op returning [] when there is no current project', async () => {
    const result = await useScheduleStore.getState().loadWbs()
    expect(result).toEqual([])
    expect(api.getWbs).not.toHaveBeenCalled()
  })

  it('loadWbs fetches the flat node list and stores it under store.wbs', async () => {
    useScheduleStore.setState({ currentProject: sampleProject() })
    const nodes = [
      { wbs_code: '1', name: 'Phase 1', parent_code: null, sort_order: 0 },
      { wbs_code: '1.1', name: 'Design', parent_code: '1', sort_order: 0 },
    ]
    api.getWbs.mockResolvedValue(nodes)
    const result = await useScheduleStore.getState().loadWbs()
    expect(api.getWbs).toHaveBeenCalledWith('P1')
    expect(result).toEqual(nodes)
    const st = useScheduleStore.getState()
    expect(st.wbs).toEqual(nodes)
    expect(isLoading(st, 'wbs')).toBe(false)
  })

  it('saveWbs PUTs the draft list and replaces store.wbs with the normalized response', async () => {
    useScheduleStore.setState({ currentProject: sampleProject() })
    const draft = [{ wbs_code: '1', name: 'Phase 1', parent_code: null, sort_order: 0 }]
    api.saveWbs.mockResolvedValue(draft)
    const result = await useScheduleStore.getState().saveWbs(draft)
    expect(api.saveWbs).toHaveBeenCalledWith('P1', draft)
    expect(result).toEqual(draft)
    expect(useScheduleStore.getState().wbs).toEqual(draft)
  })

  it('saveWbs records the failure under errors.wbs on a 422 validation error', async () => {
    useScheduleStore.setState({ currentProject: sampleProject() })
    api.saveWbs.mockRejectedValue({ response: { data: { detail: 'cycle detected' } } })
    await expect(useScheduleStore.getState().saveWbs([])).rejects.toBeTruthy()
    const st = useScheduleStore.getState()
    expect(getError(st, 'wbs')).toBe('cycle detected')
    expect(isLoading(st, 'wbs')).toBe(false)
  })
})

describe('store baseline list actions', () => {
  it('loadBaselines fetches the picker list and stores it under store.baselines', async () => {
    useScheduleStore.setState({ currentProject: sampleProject() })
    const list = [
      { id: 1, name: 'v1', created_at: '2026-01-01', is_active: false, project_duration: 10 },
      { id: 2, name: 'v2', created_at: '2026-02-01', is_active: true, project_duration: 12 },
    ]
    api.listBaselines.mockResolvedValue(list)
    const result = await useScheduleStore.getState().loadBaselines()
    expect(api.listBaselines).toHaveBeenCalledWith('P1')
    expect(result).toEqual(list)
    expect(useScheduleStore.getState().baselines).toEqual(list)
  })

  it('activateBaseline activates the target then refreshes both the list and the active baseline', async () => {
    useScheduleStore.setState({ currentProject: sampleProject() })
    api.activateBaseline.mockResolvedValue({ ok: true })
    api.listBaselines.mockResolvedValue([{ id: 1, name: 'v1', is_active: true, project_duration: 10 }])
    api.getBaseline.mockResolvedValue({ id: 1, name: 'v1', project_duration: 10, tasks: [] })
    const result = await useScheduleStore.getState().activateBaseline(1)
    expect(api.activateBaseline).toHaveBeenCalledWith('P1', 1)
    expect(api.listBaselines).toHaveBeenCalledWith('P1')
    expect(api.getBaseline).toHaveBeenCalledWith('P1')
    expect(result).toEqual({ ok: true })
    const st = useScheduleStore.getState()
    expect(st.baselines).toEqual([{ id: 1, name: 'v1', is_active: true, project_duration: 10 }])
    expect(st.baseline).toEqual({ id: 1, name: 'v1', project_duration: 10, tasks: [] })
    expect(isLoading(st, 'baselines')).toBe(false)
  })

  it('deleteBaseline removes the target then refreshes the list and active baseline (auto-activated remaining)', async () => {
    useScheduleStore.setState({ currentProject: sampleProject() })
    api.deleteBaseline.mockResolvedValue({ ok: true })
    api.listBaselines.mockResolvedValue([{ id: 2, name: 'v2', is_active: true, project_duration: 8 }])
    api.getBaseline.mockResolvedValue({ id: 2, name: 'v2', project_duration: 8, tasks: [] })
    await useScheduleStore.getState().deleteBaseline(1)
    expect(api.deleteBaseline).toHaveBeenCalledWith('P1', 1)
    const st = useScheduleStore.getState()
    expect(st.baselines).toEqual([{ id: 2, name: 'v2', is_active: true, project_duration: 8 }])
    expect(st.baseline.id).toBe(2)
  })

  it('createBaseline sets store.baseline and fires a background refresh of store.baselines', async () => {
    useScheduleStore.setState({ currentProject: sampleProject() })
    api.createBaseline.mockResolvedValue({ id: 3, name: 'v3', project_duration: 9, tasks: [] })
    api.listBaselines.mockResolvedValue([{ id: 3, name: 'v3', is_active: true, project_duration: 9 }])
    await useScheduleStore.getState().createBaseline('v3')
    expect(useScheduleStore.getState().baseline).toEqual({ id: 3, name: 'v3', project_duration: 9, tasks: [] })
    // 背景刷新為 fire-and-forget；flush microtask/macrotask 佇列後應已呼叫
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(api.listBaselines).toHaveBeenCalledWith('P1')
  })
})

describe('store updateTaskLinks with constraint extra (Pro Batch B)', () => {
  it('merges the optional extra fields (constraint_type/constraint_day) into a single PATCH', async () => {
    useScheduleStore.setState({ currentProject: sampleProject() })
    const updated = { ...sampleProject(), version: 4 }
    api.updateTask.mockResolvedValue(updated)
    await useScheduleStore
      .getState()
      .updateTaskLinks('T1', [], { constraint_type: 'SNET', constraint_day: 5 })
    expect(api.updateTask).toHaveBeenCalledWith('P1', 'T1', {
      links: [],
      constraint_type: 'SNET',
      constraint_day: 5,
      expected_version: 3,
    })
    expect(useScheduleStore.getState().currentProject).toEqual(updated)
  })

  it('omits extra fields entirely when called without a third argument (backward compatible)', async () => {
    useScheduleStore.setState({ currentProject: sampleProject() })
    api.updateTask.mockResolvedValue(sampleProject())
    await useScheduleStore.getState().updateTaskLinks('T1', [])
    expect(api.updateTask).toHaveBeenCalledWith('P1', 'T1', { links: [], expected_version: 3 })
  })
})
