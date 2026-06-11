// QUAL-2 (Batch 4)：zustand 排程 store 單元測試（vi.mock api/client.js）
// 涵蓋：登入/登出持久化、loadProject 重置分析狀態、scoped loading/errors
// 轉換、changeTaskDuration 樂觀更新（套用/回滾/409 衝突）、extractError
// 各種 FastAPI detail 形狀、session restore（持久化權杖 -> api.me()）。
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
}))

import * as api from '../api/client.js'
import {
  useScheduleStore,
  isLoading,
  getError,
  extractError,
} from './scheduleStore.js'
import { I18N } from '../i18n/index.js'

// 三任務示範專案（changeTaskDuration 測試共用）
function sampleProject() {
  return {
    project_id: 'P1',
    project_name: 'Demo',
    version: 3,
    tasks: [
      { task_id: 'T1', duration: 2, es: 0, ef: 2, float_time: 0, is_critical: true },
      { task_id: 'T2', duration: 4, es: 2, ef: 6, float_time: 0, is_critical: true },
    ],
  }
}

// 每個測試前重置 store（zustand 單例跨測試共享）+ mock 計數
beforeEach(() => {
  vi.clearAllMocks()
  useScheduleStore.setState({
    tenantId: 'TENT-9981',
    region: 'TW',
    token: null,
    role: null,
    username: null,
    projects: [],
    currentProject: null,
    loading: {},
    errors: {},
    loadingAny: false,
    dashboard: null,
    users: [],
    resources: null,
    leveling: null,
    risk: [],
    simulation: null,
    progress: [],
    baseline: null,
    evm: null,
    dataDate: null,
    trash: [],
  })
})

describe('store auth', () => {
  it('login stores token/role/username and persists them to localStorage', async () => {
    api.login.mockResolvedValue({
      access_token: 'tok-123',
      token_type: 'bearer',
      tenant_id: 'TENT-9981',
      region: 'TW',
      role: 'editor',
    })
    const data = await useScheduleStore.getState().login('user@tw', 'pw')
    expect(data.access_token).toBe('tok-123')
    const st = useScheduleStore.getState()
    expect(st.token).toBe('tok-123')
    expect(st.role).toBe('editor')
    expect(st.username).toBe('user@tw')
    expect(localStorage.getItem('cpm.token')).toBe('tok-123')
    expect(localStorage.getItem('cpm.role')).toBe('editor')
    expect(isLoading(st, 'auth')).toBe(false)
    expect(getError(st, 'auth')).toBeNull()
  })

  it('login failure sets errors.auth (scoped) and rethrows', async () => {
    api.login.mockRejectedValue({
      response: { status: 401, data: { detail: 'bad credentials' } },
      config: { url: '/auth/login' },
    })
    await expect(useScheduleStore.getState().login('u', 'wrong')).rejects.toBeTruthy()
    const st = useScheduleStore.getState()
    expect(getError(st, 'auth')).toBe('bad credentials')
    expect(isLoading(st, 'auth')).toBe(false)
    expect(st.token).toBeNull()
  })

  it('logout clears auth/project state and removes persisted token/role', () => {
    localStorage.setItem('cpm.token', 'tok-1')
    localStorage.setItem('cpm.role', 'admin')
    useScheduleStore.setState({
      token: 'tok-1',
      role: 'admin',
      username: 'admin@tw',
      currentProject: { project_id: 'P1' },
      projects: [{ project_id: 'P1' }],
      errors: { mutation: 'leftover' },
      loading: { mutation: true },
      loadingAny: true,
    })
    useScheduleStore.getState().logout()
    const st = useScheduleStore.getState()
    expect(st.token).toBeNull()
    expect(st.role).toBeNull()
    expect(st.username).toBeNull()
    expect(st.currentProject).toBeNull()
    expect(st.projects).toEqual([])
    expect(st.errors).toEqual({})
    expect(st.loadingAny).toBe(false)
    expect(localStorage.getItem('cpm.token')).toBeNull()
    expect(localStorage.getItem('cpm.role')).toBeNull()
  })
})

describe('store loadProject', () => {
  it('resets Phase 8/9 analytics state and sets currentProject', async () => {
    useScheduleStore.setState({
      resources: { limits: [] },
      leveling: { leveled_duration: 12 },
      risk: [{ task_id: 'T1' }],
      simulation: { p50: 10 },
      progress: [{ task_id: 'T1' }],
      baseline: { id: 1 },
      evm: { spi: 0.9 },
      dataDate: 7,
    })
    api.getProject.mockResolvedValue({ project_id: 'P1', tasks: [] })
    await useScheduleStore.getState().loadProject('P1')
    const st = useScheduleStore.getState()
    expect(st.currentProject.project_id).toBe('P1')
    expect(st.resources).toBeNull()
    expect(st.leveling).toBeNull()
    expect(st.risk).toEqual([])
    expect(st.simulation).toBeNull()
    expect(st.progress).toEqual([])
    expect(st.baseline).toBeNull()
    expect(st.evm).toBeNull()
    expect(st.dataDate).toBeNull()
    expect(isLoading(st, 'project')).toBe(false)
  })
})

describe('store scoped loading/errors', () => {
  it('tracks loading per scope and flips loadingAny across the transition', async () => {
    let resolveFn
    api.listProjects.mockReturnValue(
      new Promise((resolve) => {
        resolveFn = resolve
      }),
    )
    const pending = useScheduleStore.getState().loadProjects()
    let st = useScheduleStore.getState()
    expect(isLoading(st, 'projects')).toBe(true)
    expect(st.loadingAny).toBe(true)
    // 其他 scope 不受影響
    expect(isLoading(st, 'dashboard')).toBe(false)
    resolveFn([{ project_id: 'P1' }])
    await pending
    st = useScheduleStore.getState()
    expect(isLoading(st, 'projects')).toBe(false)
    expect(st.loadingAny).toBe(false)
    expect(st.projects).toEqual([{ project_id: 'P1' }])
  })

  it('records a failure under its own scope without touching other scopes', async () => {
    api.getDashboard.mockRejectedValue({ response: { data: { detail: 'boom' } } })
    await expect(useScheduleStore.getState().loadDashboard()).rejects.toBeTruthy()
    const st = useScheduleStore.getState()
    expect(getError(st, 'dashboard')).toBe('boom')
    expect(isLoading(st, 'dashboard')).toBe(false)
    expect(getError(st, 'projects')).toBeNull()
    expect(getError(st, 'auth')).toBeNull()
  })

  it('starting an action clears the previous error of that scope', async () => {
    useScheduleStore.setState({ errors: { users: 'old error' } })
    let resolveFn
    api.listUsers.mockReturnValue(
      new Promise((resolve) => {
        resolveFn = resolve
      }),
    )
    const pending = useScheduleStore.getState().loadUsers()
    expect(getError(useScheduleStore.getState(), 'users')).toBeNull()
    resolveFn([])
    await pending
  })

  it('clearError clears a single scope only', () => {
    useScheduleStore.setState({ errors: { mutation: 'm-err', auth: 'a-err' } })
    useScheduleStore.getState().clearError('mutation')
    const st = useScheduleStore.getState()
    expect(getError(st, 'mutation')).toBeNull()
    expect(getError(st, 'auth')).toBe('a-err')
  })
})

describe('store changeTaskDuration (optimistic)', () => {
  it('applies the new duration immediately, then replaces with the server project', async () => {
    useScheduleStore.setState({ currentProject: sampleProject() })
    let resolveFn
    api.updateTaskDuration.mockReturnValue(
      new Promise((resolve) => {
        resolveFn = resolve
      }),
    )
    const pending = useScheduleStore.getState().changeTaskDuration('T1', 5)
    // 樂觀套用：API 尚未完成，甘特圖立即看到新工期；其他任務不變
    let st = useScheduleStore.getState()
    expect(st.currentProject.tasks[0].duration).toBe(5)
    expect(st.currentProject.tasks[1].duration).toBe(4)
    expect(isLoading(st, 'mutation')).toBe(true)
    const serverProject = {
      project_id: 'P1',
      project_name: 'Demo',
      version: 4,
      tasks: [
        { task_id: 'T1', duration: 5, es: 0, ef: 5, float_time: 0, is_critical: true },
        { task_id: 'T2', duration: 4, es: 5, ef: 9, float_time: 0, is_critical: true },
      ],
    }
    resolveFn(serverProject)
    await pending
    st = useScheduleStore.getState()
    expect(st.currentProject).toEqual(serverProject)
    expect(isLoading(st, 'mutation')).toBe(false)
    // 樂觀鎖：以快照版本送出 expected_version
    expect(api.updateTaskDuration).toHaveBeenCalledWith('P1', 'T1', 5, 3)
  })

  it('rolls back to the snapshot and sets errors.mutation on rejection', async () => {
    useScheduleStore.setState({ currentProject: sampleProject() })
    api.updateTaskDuration.mockRejectedValue({
      response: { status: 422, data: { detail: 'invalid duration' } },
    })
    await expect(
      useScheduleStore.getState().changeTaskDuration('T1', 99),
    ).rejects.toBeTruthy()
    const st = useScheduleStore.getState()
    expect(st.currentProject.tasks[0].duration).toBe(2) // 還原快照
    expect(getError(st, 'mutation')).toBe('invalid duration')
    expect(isLoading(st, 'mutation')).toBe(false)
  })

  it('on 409 reloads the project and sets the conflictReloaded message', async () => {
    useScheduleStore.setState({ currentProject: sampleProject(), region: 'TW' })
    api.updateTaskDuration.mockRejectedValue({
      response: { status: 409, data: { detail: 'version conflict' } },
    })
    const reloaded = {
      project_id: 'P1',
      project_name: 'Demo',
      version: 9,
      tasks: [{ task_id: 'T1', duration: 7, es: 0, ef: 7, float_time: 0, is_critical: true }],
    }
    api.getProject.mockResolvedValue(reloaded)
    const out = await useScheduleStore.getState().changeTaskDuration('T1', 5)
    expect(out).toBeNull()
    const st = useScheduleStore.getState()
    expect(api.getProject).toHaveBeenCalledWith('P1')
    expect(st.currentProject).toEqual(reloaded)
    expect(getError(st, 'mutation')).toBe(I18N.TW.conflictReloaded)
    expect(isLoading(st, 'mutation')).toBe(false)
  })
})

describe('extractError', () => {
  it('returns string detail unchanged', () => {
    expect(extractError({ response: { data: { detail: 'plain message' } } })).toBe(
      'plain message',
    )
    expect(extractError({ response: { data: 'raw string body' } })).toBe('raw string body')
  })

  it('keeps object detail as JSON (unchanged behavior)', () => {
    expect(extractError({ response: { data: { detail: { code: 'X1' } } } })).toBe(
      JSON.stringify({ code: 'X1' }),
    )
  })

  it('humanizes array-shaped FastAPI detail as "loc.path: msg; ..."', () => {
    const detail = [
      { loc: ['body', 'duration'], msg: 'ensure this value is >= 0' },
      { loc: ['body', 'task_id'], msg: 'field required' },
    ]
    expect(extractError({ response: { data: { detail } } })).toBe(
      'body.duration: ensure this value is >= 0; body.task_id: field required',
    )
  })

  it('falls back to err.message when there is no response payload', () => {
    expect(extractError(new Error('network down'))).toBe('network down')
  })
})

describe('store session restore', () => {
  it('fires api.me() on store creation with a persisted token and restores identity', async () => {
    vi.resetModules()
    localStorage.setItem('cpm.token', 'tok-restored')
    // resetModules 後 mock 工廠重建：取得「新」mock 實例並於 store 匯入前設定 me()
    const apiFresh = await import('../api/client.js')
    apiFresh.me.mockResolvedValue({
      username: 'admin@tw',
      tenant_id: 'TENT-9981',
      region: 'TW',
      role: 'admin',
    })
    const mod = await import('./scheduleStore.js')
    // restoreSession 為非同步：flush microtask/macrotask
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(apiFresh.me).toHaveBeenCalledTimes(1)
    const st = mod.useScheduleStore.getState()
    expect(st.token).toBe('tok-restored')
    expect(st.username).toBe('admin@tw')
    expect(st.role).toBe('admin')
  })
})
