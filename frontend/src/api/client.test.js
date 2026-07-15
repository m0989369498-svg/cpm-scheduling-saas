// QUAL-2 (Batch 4)：api/client.js 401 回應攔截器測試（使用真實 store）
// 驗證：非登入端點 401 -> 登出 + errors.auth = sessionExpired；
// /auth/login 的 401（帳密錯誤）不觸發登出；非 401 錯誤原樣透傳。
// Pro Batch D：getCost/getHealth 端點路徑 + query 參數組裝（vi.spyOn(apiClient,'get')，不發真實請求）。
// Pro Batch E：getPool/savePool/getAllocation 端點路徑（同一 vi.spyOn 手法）。
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { apiClient, getCost, getHealth, getPool, savePool, getAllocation } from './client.js'
import { useScheduleStore, getError } from '../store/scheduleStore.js'
import { I18N } from '../i18n/index.js'

// 取得 axios 回應攔截器的 rejected handler（不真正發出 HTTP 請求）
function rejectedHandler() {
  const handlers = apiClient.interceptors.response.handlers.filter(Boolean)
  const h = handlers.find((x) => typeof x.rejected === 'function')
  expect(h).toBeTruthy()
  return h.rejected
}

beforeEach(() => {
  useScheduleStore.setState({
    token: 'tok-1',
    role: 'admin',
    username: 'admin@tw',
    region: 'TW',
    currentProject: { project_id: 'P1' },
    projects: [{ project_id: 'P1' }],
    loading: {},
    errors: {},
    loadingAny: false,
  })
})

describe('401 response interceptor', () => {
  it('logs out and sets errors.auth = sessionExpired on 401 from a non-login endpoint', async () => {
    const reject = rejectedHandler()
    const err = { response: { status: 401 }, config: { url: '/projects/P1' } }
    await expect(reject(err)).rejects.toBe(err)
    const st = useScheduleStore.getState()
    expect(st.token).toBeNull()
    expect(st.username).toBeNull()
    expect(st.currentProject).toBeNull()
    expect(getError(st, 'auth')).toBe(I18N.TW.sessionExpired)
  })

  it('does not log out on 401 from /auth/login (wrong-credentials path)', async () => {
    const reject = rejectedHandler()
    const err = { response: { status: 401 }, config: { url: '/auth/login' } }
    await expect(reject(err)).rejects.toBe(err)
    const st = useScheduleStore.getState()
    expect(st.token).toBe('tok-1')
    expect(st.username).toBe('admin@tw')
    expect(getError(st, 'auth')).toBeNull()
  })

  it('passes non-401 errors through without touching auth state', async () => {
    const reject = rejectedHandler()
    const err = { response: { status: 500 }, config: { url: '/projects' } }
    await expect(reject(err)).rejects.toBe(err)
    const st = useScheduleStore.getState()
    expect(st.token).toBe('tok-1')
    expect(getError(st, 'auth')).toBeNull()
  })
})

describe('Pro Batch D: getCost / getHealth URL construction', () => {
  it('getCost hits GET /projects/{pid}/cost', async () => {
    const spy = vi.spyOn(apiClient, 'get').mockResolvedValue({ data: { total_cost: 100 } })
    const result = await getCost('P1')
    expect(spy).toHaveBeenCalledWith('/projects/P1/cost')
    expect(result).toEqual({ total_cost: 100 })
    spy.mockRestore()
  })

  it('getHealth hits GET /projects/{pid}/health without params when dataDate is omitted', async () => {
    const spy = vi.spyOn(apiClient, 'get').mockResolvedValue({ data: { score: 1 } })
    const result = await getHealth('P1')
    expect(spy).toHaveBeenCalledWith('/projects/P1/health', { params: {} })
    expect(result).toEqual({ score: 1 })
    spy.mockRestore()
  })

  it('getHealth passes data_date as a query param when provided', async () => {
    const spy = vi.spyOn(apiClient, 'get').mockResolvedValue({ data: { score: 0.9 } })
    await getHealth('P1', 12)
    expect(spy).toHaveBeenCalledWith('/projects/P1/health', { params: { data_date: 12 } })
    spy.mockRestore()
  })

  it('URL-encodes the project id for both endpoints', async () => {
    const spy = vi.spyOn(apiClient, 'get').mockResolvedValue({ data: {} })
    await getCost('P 1')
    await getHealth('P 1')
    expect(spy).toHaveBeenNthCalledWith(1, '/projects/P%201/cost')
    expect(spy).toHaveBeenNthCalledWith(2, '/projects/P%201/health', { params: {} })
    spy.mockRestore()
  })
})

describe('Pro Batch E: getPool / savePool / getAllocation URL construction', () => {
  it('getPool hits GET /resources/pool', async () => {
    const spy = vi.spyOn(apiClient, 'get').mockResolvedValue({ data: [{ resource_type: 'crane' }] })
    const result = await getPool()
    expect(spy).toHaveBeenCalledWith('/resources/pool')
    expect(result).toEqual([{ resource_type: 'crane' }])
    spy.mockRestore()
  })

  it('savePool hits PUT /resources/pool with the provided list', async () => {
    const spy = vi.spyOn(apiClient, 'put').mockResolvedValue({ data: [{ resource_type: 'crane', capacity: 2 }] })
    const list = [{ resource_type: 'crane', capacity: 2 }]
    const result = await savePool(list)
    expect(spy).toHaveBeenCalledWith('/resources/pool', list)
    expect(result).toEqual(list)
    spy.mockRestore()
  })

  it('getAllocation hits GET /resources/allocation', async () => {
    const spy = vi.spyOn(apiClient, 'get').mockResolvedValue({ data: { weeks: [], resources: [] } })
    const result = await getAllocation()
    expect(spy).toHaveBeenCalledWith('/resources/allocation')
    expect(result).toEqual({ weeks: [], resources: [] })
    spy.mockRestore()
  })
})
