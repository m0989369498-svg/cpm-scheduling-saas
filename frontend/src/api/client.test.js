// QUAL-2 (Batch 4)：api/client.js 401 回應攔截器測試（使用真實 store）
// 驗證：非登入端點 401 -> 登出 + errors.auth = sessionExpired；
// /auth/login 的 401（帳密錯誤）不觸發登出；非 401 錯誤原樣透傳。
import { describe, it, expect, beforeEach } from 'vitest'
import { apiClient } from './client.js'
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
