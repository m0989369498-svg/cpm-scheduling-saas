import axios from 'axios'

// API 基底路徑：開發時可由 VITE_API_BASE_URL 覆寫；
// 生產環境經 gateway 反向代理，預設 '/api/v1'。
const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || '/api/v1'

// localStorage 鍵（與 store 共用），作為攔截器讀取租戶/區域/權杖的回退來源。
const LS_TENANT_KEY = 'cpm.tenantId'
const LS_REGION_KEY = 'cpm.region'
const LS_TOKEN_KEY = 'cpm.token'

export const apiClient = axios.create({
  baseURL: API_BASE_URL,
  headers: { 'Content-Type': 'application/json' },
})

// 請求攔截器：從 zustand store（若已掛載）或 localStorage 注入
// X-Tenant-Id、X-Region 與 Authorization。store 為單一真實來源，localStorage 為回退。
// 後端規則：若帶 Bearer 權杖則優先採用其租戶/區域；否則回退標頭模式。
apiClient.interceptors.request.use((config) => {
  let tenantId = null
  let region = null
  let token = null

  // 動態取得 store，避免與 store 模組相互循環匯入。
  try {
    // eslint-disable-next-line global-require
    const mod = globalThis.__cpmScheduleStore
    if (mod && typeof mod.getState === 'function') {
      const st = mod.getState()
      tenantId = st.tenantId
      region = st.region
      token = st.token
    }
  } catch (e) {
    // 忽略：改用 localStorage 回退
  }

  if (!tenantId) {
    tenantId = (typeof localStorage !== 'undefined' && localStorage.getItem(LS_TENANT_KEY)) || 'TENT-9981'
  }
  if (!region) {
    region = (typeof localStorage !== 'undefined' && localStorage.getItem(LS_REGION_KEY)) || 'TW'
  }
  if (!token) {
    token = (typeof localStorage !== 'undefined' && localStorage.getItem(LS_TOKEN_KEY)) || null
  }

  config.headers = config.headers || {}
  config.headers['X-Tenant-Id'] = tenantId
  config.headers['X-Region'] = region
  // 帶上 Bearer 權杖（若存在）；後端 Bearer 優先於標頭。
  if (token) {
    config.headers['Authorization'] = `Bearer ${token}`
  }
  return config
})

// ---- 匯出 API 函式（每個皆回傳 response.data） ----

// 登入：以帳號/密碼換取 JWT 權杖。回傳 {access_token, token_type, tenant_id, region}。
export async function login(username, password) {
  const res = await apiClient.post('/auth/login', { username, password })
  return res.data
}

// 取得目前登入身分（依 Bearer 權杖解析）。回傳 {username, tenant_id, region}。
export async function me() {
  const res = await apiClient.get('/auth/me')
  return res.data
}

// 無狀態 CPM 計算（不落地 DB）
export async function calculateSchedule(tasks) {
  const res = await apiClient.post('/schedule/calculate', tasks)
  return res.data
}

// 專案清單摘要
export async function listProjects() {
  const res = await apiClient.get('/projects')
  return res.data
}

// 取得單一專案（含 CPM 結果）
export async function getProject(projectId) {
  const res = await apiClient.get(`/projects/${encodeURIComponent(projectId)}`)
  return res.data
}

// 建立專案（持久化任務+相依，執行 CPM）
export async function createProject(payload) {
  const res = await apiClient.post('/projects', payload)
  return res.data
}

// 新增任務並重算 CPM
export async function addTask(projectId, task) {
  const res = await apiClient.post(`/projects/${encodeURIComponent(projectId)}/tasks`, task)
  return res.data
}

// 更新任務（任意欄位）並重算 CPM
export async function updateTask(projectId, taskId, patch) {
  const res = await apiClient.put(
    `/projects/${encodeURIComponent(projectId)}/tasks/${encodeURIComponent(taskId)}`,
    patch,
  )
  return res.data
}

// 拖曳改工期專用路徑：更新工期後整案重算 CPM
export async function updateTaskDuration(projectId, taskId, duration) {
  const res = await apiClient.put(
    `/projects/${encodeURIComponent(projectId)}/tasks/${encodeURIComponent(taskId)}/duration`,
    { duration },
  )
  return res.data
}

// 刪除任務（含其相依）並重算
export async function deleteTask(projectId, taskId) {
  const res = await apiClient.delete(
    `/projects/${encodeURIComponent(projectId)}/tasks/${encodeURIComponent(taskId)}`,
  )
  return res.data
}

// 刪除專案
export async function deleteProject(projectId) {
  const res = await apiClient.delete(`/projects/${encodeURIComponent(projectId)}`)
  return res.data
}

// 拋轉 ERP：將任務排入 sync_event_log（PENDING）
export async function syncErp(projectId, syncType = 'SCHEDULE_PUSH') {
  const res = await apiClient.post(`/projects/${encodeURIComponent(projectId)}/erp/sync`, {
    sync_type: syncType,
  })
  return res.data
}

// 報表下載 URL（供 window.open / <a href> 直接開啟 PDF）
export function reportUrl(projectId) {
  const base = API_BASE_URL.replace(/\/$/, '')
  return `${base}/projects/${encodeURIComponent(projectId)}/report`
}

export default apiClient
