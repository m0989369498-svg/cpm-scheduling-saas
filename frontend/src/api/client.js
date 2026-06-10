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

// ---- Phase 8：資源撫平 (resource leveling) ----

// 取得專案資源設定（資源上限 + 各任務資源需求）
export async function getResources(projectId) {
  const res = await apiClient.get(`/projects/${encodeURIComponent(projectId)}/resources`)
  return res.data
}

// 設定專案資源（upsert 資源上限 + 各任務 resource_demands）
export async function setResources(projectId, body) {
  const res = await apiClient.put(`/projects/${encodeURIComponent(projectId)}/resources`, body)
  return res.data
}

// 執行資源撫平（回傳 LevelingResult，含撫平後工期/逐日載荷/超載日）
export async function levelResources(projectId) {
  const res = await apiClient.post(`/projects/${encodeURIComponent(projectId)}/level`)
  return res.data
}

// ---- Phase 8：蒙地卡羅風險分析 (Monte Carlo) ----

// 取得三點估計參數（list[RiskParam]）
export async function getRisk(projectId) {
  const res = await apiClient.get(`/projects/${encodeURIComponent(projectId)}/risk`)
  return res.data
}

// 設定三點估計參數（upsert task_risk_parameters）
export async function setRisk(projectId, body) {
  const res = await apiClient.put(`/projects/${encodeURIComponent(projectId)}/risk`, body)
  return res.data
}

// 執行蒙地卡羅模擬（body SimulationRequest）-> SimulationResult
export async function simulate(projectId, body) {
  const res = await apiClient.post(`/projects/${encodeURIComponent(projectId)}/simulate`, body)
  return res.data
}

// 報表下載 URL（供 window.open / <a href> 直接開啟 PDF）
export function reportUrl(projectId) {
  const base = API_BASE_URL.replace(/\/$/, '')
  return `${base}/projects/${encodeURIComponent(projectId)}/report`
}

// ---- Phase 9：進度追蹤 + EVM（實獲值管理）----

// 取得每任務進度（list[ProgressEntry]；無資料列者預設 budget0/pct0）
export async function getProgress(projectId) {
  const res = await apiClient.get(`/projects/${encodeURIComponent(projectId)}/progress`)
  return res.data
}

// 儲存每任務進度（upsert；body list[ProgressEntry]）-> list[ProgressEntry]
export async function saveProgress(projectId, list) {
  const res = await apiClient.put(`/projects/${encodeURIComponent(projectId)}/progress`, list)
  return res.data
}

// 建立基準線（以目前 CPM es/ef/duration + 進度預算為快照）-> BaselineOut
export async function createBaseline(projectId, name) {
  const body = name ? { name } : {}
  const res = await apiClient.post(`/projects/${encodeURIComponent(projectId)}/baseline`, body)
  return res.data
}

// 取得最新基準線 -> BaselineOut（無基準線時後端回 404）
export async function getBaseline(projectId) {
  const res = await apiClient.get(`/projects/${encodeURIComponent(projectId)}/baseline`)
  return res.data
}

// 計算 EVM（依最新基準線 + 進度，data_date 選用，預設基準線總工期）-> EvmResult
export async function getEvm(projectId, dataDate) {
  const params = {}
  if (dataDate != null && dataDate !== '') params.data_date = dataDate
  const res = await apiClient.get(`/projects/${encodeURIComponent(projectId)}/evm`, { params })
  return res.data
}

// 拋轉 EVM 風險預警（若 risk_flagged 則排入 sync_event_log）-> {dispatched, ...}
export async function dispatchEvmAlert(projectId, dataDate) {
  const params = {}
  if (dataDate != null && dataDate !== '') params.data_date = dataDate
  const res = await apiClient.post(
    `/projects/${encodeURIComponent(projectId)}/evm/alert`,
    null,
    { params },
  )
  return res.data
}

// ---- Phase 10：儀表板（Dashboard）+ 使用者管理（Users）+ 匯出（Exports）----

// 取得租戶層級儀表板（投資組合 KPI 彙總）-> {projects:[ProjectKpi], totals:{...}}
export async function getDashboard() {
  const res = await apiClient.get('/dashboard')
  return res.data
}

// 列出本租戶使用者（需 admin）-> list[UserOut]
export async function listUsers() {
  const res = await apiClient.get('/users')
  return res.data
}

// 建立使用者（需 admin；body {username,password,role,region?}）-> UserOut
export async function createUser(body) {
  const res = await apiClient.post('/users', body)
  return res.data
}

// 更新使用者（需 admin；body {role?,is_active?,password?}）-> UserOut
export async function updateUser(id, body) {
  const res = await apiClient.put(`/users/${encodeURIComponent(id)}`, body)
  return res.data
}

// 刪除使用者（需 admin）-> {ok:true}
export async function deleteUser(id) {
  const res = await apiClient.delete(`/users/${encodeURIComponent(id)}`)
  return res.data
}

// Excel 匯出 URL（GET 檔案下載；需以 Authorization 標頭驗證後以 blob 觸發下載）
export function exportXlsxUrl(projectId) {
  const base = API_BASE_URL.replace(/\/$/, '')
  return `${base}/projects/${encodeURIComponent(projectId)}/export.xlsx`
}

// PDF 匯出 URL（GET 檔案下載；需以 Authorization 標頭驗證後以 blob 觸發下載）
export function exportPdfUrl(projectId) {
  const base = API_BASE_URL.replace(/\/$/, '')
  return `${base}/projects/${encodeURIComponent(projectId)}/export.pdf`
}

export default apiClient
