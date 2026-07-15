// Pro Batch F1 — mockApi.js（瀏覽器端模擬後端）單元測試。
// 直接使用真實 apiClient（安裝 install() 的自訂 adapter 後，所有請求皆在記憶體
// 中處理，不發出網路請求），驗證：登入角色、專案 GET 與 fixture 一致、
// 工期變更觸發下游 es/ef 重算、expected_version 過期回 409、照片 data URI 往返。
import { describe, it, expect, beforeEach } from 'vitest'
import { apiClient } from '../api/client.js'
import { install, _getStateForTests } from './mockApi.js'
import project001Fixture from './fixtures/project__PRJ_2026_TW_001.json'

beforeEach(() => {
  install()
})

async function login(username, password = 'demo1234') {
  return apiClient.post('/auth/login', { username, password })
}

describe('install() / 示範模式提示橫幅', () => {
  it('於 document.body 注入一次性的 banner', () => {
    const banners = document.querySelectorAll('#cpm-demo-banner')
    expect(banners.length).toBe(1)
  })
})

describe('POST /auth/login — 角色 (admin/editor/viewer)', () => {
  it('admin@tw / demo1234 -> role admin', async () => {
    const res = await login('admin@tw')
    expect(res.status).toBe(200)
    expect(res.data.role).toBe('admin')
    expect(res.data.access_token).toBeTruthy()
    expect(res.data.tenant_id).toBe('TENT-9981')
  })

  it('editor@tw / demo1234 -> role editor', async () => {
    const res = await login('editor@tw')
    expect(res.data.role).toBe('editor')
  })

  it('viewer@tw / demo1234 -> role viewer', async () => {
    const res = await login('viewer@tw')
    expect(res.data.role).toBe('viewer')
  })

  it('錯誤密碼 -> 401 with detail', async () => {
    await expect(login('admin@tw', 'wrong')).rejects.toMatchObject({
      response: { status: 401, data: { detail: expect.any(String) } },
    })
  })

  it('未知帳號 -> 401', async () => {
    await expect(login('nobody@tw')).rejects.toMatchObject({
      response: { status: 401 },
    })
  })
})

describe('GET /auth/me', () => {
  it('登入後帶 Bearer token 可取得目前身分', async () => {
    const loginRes = await login('admin@tw')
    const res = await apiClient.get('/auth/me', {
      headers: { Authorization: `Bearer ${loginRes.data.access_token}` },
    })
    expect(res.data).toMatchObject({ username: 'admin@tw', role: 'admin', tenant_id: 'TENT-9981' })
  })

  it('未帶 token -> 401', async () => {
    await expect(apiClient.get('/auth/me', { headers: {} })).rejects.toMatchObject({
      response: { status: 401 },
    })
  })
})

describe('GET /projects/{pid} — 與 fixture 一致', () => {
  it('PRJ-2026-TW-001 的任務數/工期/首個任務 es-ef 與 fixture 相符', async () => {
    const res = await apiClient.get('/projects/PRJ-2026-TW-001')
    expect(res.data.project_id).toBe('PRJ-2026-TW-001')
    expect(res.data.tasks.length).toBe(project001Fixture.tasks.length)
    expect(res.data.project_duration).toBe(project001Fixture.project_duration)
    const t01 = res.data.tasks.find((t) => t.task_id === 'T-01')
    const t01Fixture = project001Fixture.tasks.find((t) => t.task_id === 'T-01')
    expect(t01.es).toBe(t01Fixture.es)
    expect(t01.ef).toBe(t01Fixture.ef)
  })

  it('不存在的專案 -> 404', async () => {
    await expect(apiClient.get('/projects/NOPE')).rejects.toMatchObject({
      response: { status: 404 },
    })
  })
})

describe('PUT /projects/{pid}/tasks/{tid}/duration — 即時重算下游 es/ef', () => {
  it('T-01 工期 5 -> 8：T-02 (FS 相依) es/ef 同步後移 3 天', async () => {
    const before = await apiClient.get('/projects/PRJ-2026-TW-001')
    const t02Before = before.data.tasks.find((t) => t.task_id === 'T-02')
    expect(t02Before.es).toBe(5)
    expect(t02Before.ef).toBe(8)
    // 注意：mock 回傳的是記憶體 state 的即時參照（非快照），PUT 之後 pstate.project
    // 會被就地重算並改寫同一個物件；比較用的舊值需在 PUT 之前先擷取成純值。
    const durationBefore = before.data.project_duration
    const versionBefore = before.data.version

    const res = await apiClient.put('/projects/PRJ-2026-TW-001/tasks/T-01/duration', { duration: 8 })
    const t01After = res.data.tasks.find((t) => t.task_id === 'T-01')
    const t02After = res.data.tasks.find((t) => t.task_id === 'T-02')
    expect(t01After.ef).toBe(8)
    expect(t02After.es).toBe(8)
    expect(t02After.ef).toBe(11)
    // project_duration 與 version 同步更新
    expect(res.data.project_duration).toBe(durationBefore + 3)
    expect(res.data.version).toBe(versionBefore + 1)
  })

  it('day_dates 隨 start_date 一併重建（長度 = project_duration+1）', async () => {
    const res = await apiClient.put('/projects/PRJ-2026-TW-001/tasks/T-01/duration', { duration: 8 })
    expect(Array.isArray(res.data.day_dates)).toBe(true)
    expect(res.data.day_dates.length).toBe(res.data.project_duration + 1)
  })
})

describe('expected_version 樂觀鎖 — 409 衝突', () => {
  it('expected_version 與目前 version 不符 -> 409 帶 current_version', async () => {
    await expect(
      apiClient.put('/projects/PRJ-2026-TW-001/tasks/T-01/duration', {
        duration: 9,
        expected_version: 999,
      }),
    ).rejects.toMatchObject({
      response: { status: 409, data: { current_version: 0 } },
    })
  })

  it('expected_version 與目前 version 相符 -> 成功並 version+1', async () => {
    const res = await apiClient.put('/projects/PRJ-2026-TW-001/tasks/T-01/duration', {
      duration: 9,
      expected_version: 0,
    })
    expect(res.data.version).toBe(1)
  })
})

describe('照片上傳 (multipart) — data URI 往返', () => {
  it('上傳後 GET 清單可見，url 為 data: URI；DELETE 後移除', async () => {
    const blob = new Blob(['fake-image-bytes'], { type: 'image/png' })
    const form = new FormData()
    form.append('file', blob, 'site.png')
    form.append('note', '工地照片')

    const loginRes = await login('editor@tw')
    const authHeaders = { Authorization: `Bearer ${loginRes.data.access_token}` }

    const uploadRes = await apiClient.post('/projects/PRJ-2026-TW-001/tasks/T-01/photos', form, {
      headers: { ...authHeaders, 'Content-Type': undefined },
    })
    expect(uploadRes.data.url.startsWith('data:')).toBe(true)
    expect(uploadRes.data.task_id).toBe('T-01')
    expect(uploadRes.data.note).toBe('工地照片')

    const listRes = await apiClient.get('/projects/PRJ-2026-TW-001/tasks/T-01/photos')
    expect(listRes.data.length).toBe(1)
    expect(listRes.data[0].id).toBe(uploadRes.data.id)

    await apiClient.delete(`/photos/${uploadRes.data.id}`)
    const listAfter = await apiClient.get('/projects/PRJ-2026-TW-001/tasks/T-01/photos')
    expect(listAfter.data.length).toBe(0)
  })
})

describe('POST /projects/import — P6 XER / MSPDI 匯入（demo 固定示範結果）', () => {
  it('上傳 .xer -> {project, report}：format=xer、專案已重算並加入清單、可再 GET', async () => {
    const form = new FormData()
    form.append('file', new File(['fake-xer-bytes'], 'sample.xer', { type: 'application/octet-stream' }))
    form.append('format', 'auto')

    const res = await apiClient.post('/projects/import', form, {
      headers: { 'Content-Type': undefined },
    })
    expect(res.status).toBe(200)
    expect(res.data.report.format).toBe('xer')
    expect(res.data.report.tasks).toBe(res.data.project.tasks.length)
    expect(res.data.report.wbs).toBeGreaterThan(0)
    expect(res.data.report.links).toBeGreaterThan(0)
    expect(res.data.report.warnings.length).toBeGreaterThan(0)
    // CPM 已重算：總工期 > 0、每個任務皆有 es/ef
    expect(res.data.project.project_duration).toBeGreaterThan(0)
    for (const task of res.data.project.tasks) {
      expect(task.ef).toBeGreaterThanOrEqual(task.es)
    }
    // day_dates 依 start_date 重建
    expect(Array.isArray(res.data.project.day_dates)).toBe(true)

    const pid = res.data.project.project_id
    const list = await apiClient.get('/projects')
    expect(list.data.some((p) => p.project_id === pid)).toBe(true)
    const got = await apiClient.get(`/projects/${pid}`)
    expect(got.data.tasks.length).toBe(res.data.report.tasks)
  })

  it('.mspdi.xml -> format=mspdi；未附檔案 -> 422', async () => {
    const form = new FormData()
    form.append('file', new File(['<Project/>'], 'sample.mspdi.xml', { type: 'text/xml' }))
    const res = await apiClient.post('/projects/import', form, {
      headers: { 'Content-Type': undefined },
    })
    expect(res.data.report.format).toBe('mspdi')

    const empty = new FormData()
    await expect(
      apiClient.post('/projects/import', empty, { headers: { 'Content-Type': undefined } }),
    ).rejects.toMatchObject({ response: { status: 422, data: { detail: expect.any(String) } } })
  })
})

describe('reset 語義 — 每次 install() 重建全新記憶體 state', () => {
  it('前一測試的變更不會外洩至下一次 install()', async () => {
    const res = await apiClient.get('/projects/PRJ-2026-TW-001')
    expect(res.data.version).toBe(0)
  })

  it('_getStateForTests 回傳目前記憶體 state（供測試內省）', () => {
    const state = _getStateForTests()
    expect(state.projects['PRJ-2026-TW-001']).toBeTruthy()
  })
})

describe('未實作路由 -> 404', () => {
  it('未知路徑回 404 且帶 detail', async () => {
    await expect(apiClient.get('/no/such/route')).rejects.toMatchObject({
      response: { status: 404, data: { detail: expect.any(String) } },
    })
  })
})
