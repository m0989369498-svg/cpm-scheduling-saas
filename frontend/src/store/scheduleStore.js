import { create } from 'zustand'
import * as api from '../api/client.js'

// localStorage 鍵（與 api/client.js 攔截器共用）
const LS_TENANT_KEY = 'cpm.tenantId'
const LS_REGION_KEY = 'cpm.region'
const LS_TOKEN_KEY = 'cpm.token'
// Phase 10：角色（role）持久化，使重新整理後仍可正確顯示/隱藏管理介面與寫入動作。
const LS_ROLE_KEY = 'cpm.role'

// 預設值：示範租戶 TENT-9981，預設區域 TW
const DEFAULT_TENANT = 'TENT-9981'
const DEFAULT_REGION = 'TW'

function readLS(key, fallback) {
  try {
    if (typeof localStorage !== 'undefined') {
      const v = localStorage.getItem(key)
      if (v != null && v !== '') return v
    }
  } catch (e) {
    /* 忽略 */
  }
  return fallback
}

function writeLS(key, value) {
  try {
    if (typeof localStorage !== 'undefined') localStorage.setItem(key, value)
  } catch (e) {
    /* 忽略 */
  }
}

function removeLS(key) {
  try {
    if (typeof localStorage !== 'undefined') localStorage.removeItem(key)
  } catch (e) {
    /* 忽略 */
  }
}

// zustand 排程狀態：
// state { tenantId, region, token, username, projects, currentProject, loading, error }
export const useScheduleStore = create((set, get) => ({
  tenantId: readLS(LS_TENANT_KEY, DEFAULT_TENANT),
  region: readLS(LS_REGION_KEY, DEFAULT_REGION),
  // 由 localStorage 初始化權杖（重新整理後維持登入狀態）；未登入為 null。
  token: readLS(LS_TOKEN_KEY, null),
  // 角色：viewer | editor | admin。由 localStorage 初始化（重新整理後復原）；未登入為 null。
  role: readLS(LS_ROLE_KEY, null),
  username: null,
  projects: [],
  currentProject: null,
  loading: false,
  error: null,

  // ---- Phase 10：儀表板（投資組合 KPI）+ 使用者管理 ----
  // dashboard : {projects:[ProjectKpi], totals:{...}} | null
  // users     : list[UserOut]（僅 admin 載入）
  dashboard: null,
  users: [],

  // ---- Phase 8：資源撫平 + 蒙地卡羅 ----
  // resources  : ResourceConfig { limits:[{resource_type,max_capacity}], demands:{taskId:{res:qty}} } | null
  // leveling   : LevelingResult（含 over_capacity_days 供甘特圖警示帶） | null
  // risk       : list[RiskParam]（三點估計 + criticality_index）
  // simulation : SimulationResult（s_curve / p10/p50/p90 / criticality / on_time_probability） | null
  resources: null,
  leveling: null,
  risk: [],
  simulation: null,

  // ---- Phase 9：進度追蹤 + EVM（實獲值管理）----
  // progress : list[ProgressEntry]（每任務 budget/percent_complete/actual_cost/…） | []
  // baseline : BaselineOut（最新基準線；含 project_duration + tasks[es/ef/duration/budget]） | null
  // evm      : EvmResult（BAC/PV/EV/AC/SPI/CPI/EAC/… + pv_curve + per_task + risk_flagged） | null
  // dataDate : int（EVM 計算的資料日；null 表示使用基準線總工期）
  progress: [],
  baseline: null,
  evm: null,
  dataDate: null,

  // 登入：以帳號/密碼換取 JWT。成功後設定 token + 由權杖回傳的 tenantId/region + username，
  // 並將 token 持久化至 localStorage（攔截器與重新整理後復原皆使用）。
  // 同時持久化 tenantId/region，使標頭回退與 token 一致。
  login: async (username, password) => {
    set({ loading: true, error: null })
    try {
      const data = await api.login(username, password)
      const tenantId = data.tenant_id
      const region = data.region
      // 角色由登入回應決定（舊權杖/後端缺欄位時退回 admin，與後端預設一致）。
      const role = data.role || 'admin'
      writeLS(LS_TOKEN_KEY, data.access_token)
      writeLS(LS_ROLE_KEY, role)
      if (tenantId) writeLS(LS_TENANT_KEY, tenantId)
      if (region) writeLS(LS_REGION_KEY, region)
      set({
        token: data.access_token,
        role,
        username,
        tenantId: tenantId || get().tenantId,
        region: region || get().region,
        loading: false,
      })
      return data
    } catch (err) {
      set({ error: extractError(err), loading: false })
      throw err
    }
  },

  // 登出：清除權杖/使用者與當前專案狀態，並移除 localStorage 權杖/角色。
  logout: () => {
    removeLS(LS_TOKEN_KEY)
    removeLS(LS_ROLE_KEY)
    set({
      token: null,
      role: null,
      username: null,
      currentProject: null,
      projects: [],
      error: null,
      // 重置 Phase 8 分析狀態
      resources: null,
      leveling: null,
      risk: [],
      simulation: null,
      // 重置 Phase 9 進度/EVM 狀態
      progress: [],
      baseline: null,
      evm: null,
      dataDate: null,
      // 重置 Phase 10 儀表板/使用者狀態
      dashboard: null,
      users: [],
    })
  },

  // 切換租戶並持久化（攔截器下次請求即帶上新 X-Tenant-Id）。
  // 同時清空舊租戶的專案/清單，避免顯示到其他租戶的殘留資料（RLS 隔離）。
  setTenant: (id) => {
    writeLS(LS_TENANT_KEY, id)
    set({
      tenantId: id,
      currentProject: null,
      projects: [],
      // 切換租戶：清空 Phase 8 分析狀態（避免跨租戶殘留）
      resources: null,
      leveling: null,
      risk: [],
      simulation: null,
      // 切換租戶：清空 Phase 9 進度/EVM 狀態（避免跨租戶殘留）
      progress: [],
      baseline: null,
      evm: null,
      dataDate: null,
      // 切換租戶：清空 Phase 10 儀表板/使用者狀態（避免跨租戶殘留）
      dashboard: null,
      users: [],
    })
  },

  // 切換區域並持久化（驅動 i18n 與 X-Region 標頭）
  setRegion: (r) => {
    writeLS(LS_REGION_KEY, r)
    set({ region: r })
  },

  // 載入專案清單
  loadProjects: async () => {
    set({ loading: true, error: null })
    try {
      const projects = await api.listProjects()
      set({ projects, loading: false })
      return projects
    } catch (err) {
      set({ error: extractError(err), loading: false })
      throw err
    }
  },

  // 載入單一專案（含 CPM 結果），設為當前專案
  loadProject: async (id) => {
    // 切換專案：重置 Phase 8/9 分析狀態（撫平/模擬/進度/EVM 結果不可跨專案沿用）
    set({
      loading: true,
      error: null,
      resources: null,
      leveling: null,
      risk: [],
      simulation: null,
      progress: [],
      baseline: null,
      evm: null,
      dataDate: null,
    })
    try {
      const project = await api.getProject(id)
      set({ currentProject: project, loading: false })
      return project
    } catch (err) {
      set({ error: extractError(err), loading: false })
      throw err
    }
  },

  // 建立專案，設為當前並刷新清單
  createProject: async (payload) => {
    set({
      loading: true,
      error: null,
      resources: null,
      leveling: null,
      risk: [],
      simulation: null,
      progress: [],
      baseline: null,
      evm: null,
      dataDate: null,
    })
    try {
      const project = await api.createProject(payload)
      set({ currentProject: project, loading: false })
      // 背景刷新清單（不阻塞）
      get()
        .loadProjects()
        .catch(() => {})
      return project
    } catch (err) {
      set({ error: extractError(err), loading: false })
      throw err
    }
  },

  // 拖曳/輸入改工期 -> 後端整案重算 CPM -> 以回傳 ProjectOut 更新當前專案
  changeTaskDuration: async (taskId, duration) => {
    const cur = get().currentProject
    if (!cur) return null
    set({ loading: true, error: null })
    try {
      const project = await api.updateTaskDuration(cur.project_id, taskId, Number(duration))
      set({ currentProject: project, loading: false })
      return project
    } catch (err) {
      set({ error: extractError(err), loading: false })
      throw err
    }
  },

  // 新增任務 -> 後端重算 -> 更新當前專案
  addTask: async (task) => {
    const cur = get().currentProject
    if (!cur) return null
    set({ loading: true, error: null })
    try {
      const project = await api.addTask(cur.project_id, task)
      set({ currentProject: project, loading: false })
      return project
    } catch (err) {
      set({ error: extractError(err), loading: false })
      throw err
    }
  },

  // 刪除任務 -> 後端重算 -> 更新當前專案
  removeTask: async (taskId) => {
    const cur = get().currentProject
    if (!cur) return null
    set({ loading: true, error: null })
    try {
      const project = await api.deleteTask(cur.project_id, taskId)
      set({ currentProject: project, loading: false })
      return project
    } catch (err) {
      set({ error: extractError(err), loading: false })
      throw err
    }
  },

  // 拋轉當前專案至 ERP（排入同步事件）
  syncErp: async (syncType = 'SCHEDULE_PUSH') => {
    const cur = get().currentProject
    if (!cur) return null
    set({ loading: true, error: null })
    try {
      const result = await api.syncErp(cur.project_id, syncType)
      set({ loading: false })
      return result
    } catch (err) {
      set({ error: extractError(err), loading: false })
      throw err
    }
  },

  // ---- Phase 8：資源撫平 (resource leveling) ----

  // 載入當前專案資源設定（資源上限 + 各任務需求）
  loadResources: async () => {
    const cur = get().currentProject
    if (!cur) return null
    set({ loading: true, error: null })
    try {
      const resources = await api.getResources(cur.project_id)
      set({ resources, loading: false })
      return resources
    } catch (err) {
      set({ error: extractError(err), loading: false })
      throw err
    }
  },

  // 儲存資源設定（upsert 上限 + 各任務 resource_demands）；回傳並更新 store.resources
  saveResources: async (cfg) => {
    const cur = get().currentProject
    if (!cur) return null
    set({ loading: true, error: null })
    try {
      const resources = await api.setResources(cur.project_id, cfg)
      set({ resources, loading: false })
      return resources
    } catch (err) {
      set({ error: extractError(err), loading: false })
      throw err
    }
  },

  // 執行資源撫平；結果（含 over_capacity_days）存入 store.leveling 供甘特圖警示帶
  runLeveling: async () => {
    const cur = get().currentProject
    if (!cur) return null
    set({ loading: true, error: null })
    try {
      const leveling = await api.levelResources(cur.project_id)
      set({ leveling, loading: false })
      return leveling
    } catch (err) {
      set({ error: extractError(err), loading: false })
      throw err
    }
  },

  // ---- Phase 8：蒙地卡羅風險分析 (Monte Carlo) ----

  // 載入三點估計參數（list[RiskParam]）
  loadRisk: async () => {
    const cur = get().currentProject
    if (!cur) return []
    set({ loading: true, error: null })
    try {
      const risk = await api.getRisk(cur.project_id)
      set({ risk: Array.isArray(risk) ? risk : [], loading: false })
      return risk
    } catch (err) {
      set({ error: extractError(err), loading: false })
      throw err
    }
  },

  // 儲存三點估計參數（upsert）；回傳並更新 store.risk
  saveRisk: async (list) => {
    const cur = get().currentProject
    if (!cur) return []
    set({ loading: true, error: null })
    try {
      const risk = await api.setRisk(cur.project_id, list)
      set({ risk: Array.isArray(risk) ? risk : [], loading: false })
      return risk
    } catch (err) {
      set({ error: extractError(err), loading: false })
      throw err
    }
  },

  // 執行蒙地卡羅模擬（req SimulationRequest）；結果存入 store.simulation
  runSimulation: async (req = {}) => {
    const cur = get().currentProject
    if (!cur) return null
    set({ loading: true, error: null })
    try {
      const simulation = await api.simulate(cur.project_id, req)
      set({ simulation, loading: false })
      return simulation
    } catch (err) {
      set({ error: extractError(err), loading: false })
      throw err
    }
  },

  // ---- Phase 9：進度追蹤 + EVM（實獲值管理）----

  // 載入當前專案每任務進度（list[ProgressEntry]）；存入 store.progress
  loadProgress: async () => {
    const cur = get().currentProject
    if (!cur) return []
    set({ loading: true, error: null })
    try {
      const progress = await api.getProgress(cur.project_id)
      set({ progress: Array.isArray(progress) ? progress : [], loading: false })
      return progress
    } catch (err) {
      set({ error: extractError(err), loading: false })
      throw err
    }
  },

  // 儲存每任務進度（upsert）；回傳並更新 store.progress
  saveProgress: async (list) => {
    const cur = get().currentProject
    if (!cur) return []
    set({ loading: true, error: null })
    try {
      const progress = await api.saveProgress(cur.project_id, list)
      set({ progress: Array.isArray(progress) ? progress : [], loading: false })
      return progress
    } catch (err) {
      set({ error: extractError(err), loading: false })
      throw err
    }
  },

  // 建立基準線（以目前 CPM + 進度預算為快照）；存入 store.baseline 並設為最新
  createBaseline: async (name) => {
    const cur = get().currentProject
    if (!cur) return null
    set({ loading: true, error: null })
    try {
      const baseline = await api.createBaseline(cur.project_id, name)
      set({ baseline, loading: false })
      return baseline
    } catch (err) {
      set({ error: extractError(err), loading: false })
      throw err
    }
  },

  // 載入最新基準線；存入 store.baseline。無基準線（404）視為 null（不視為錯誤）。
  loadBaseline: async () => {
    const cur = get().currentProject
    if (!cur) return null
    set({ loading: true, error: null })
    try {
      const baseline = await api.getBaseline(cur.project_id)
      set({ baseline, loading: false })
      return baseline
    } catch (err) {
      // 尚無基準線（404）為正常狀態：清空 baseline、不顯示錯誤
      if (err && err.response && err.response.status === 404) {
        set({ baseline: null, loading: false })
        return null
      }
      set({ error: extractError(err), loading: false })
      throw err
    }
  },

  // 計算 EVM（dataDate 選用，預設基準線總工期）；結果存入 store.evm，並記錄使用的 dataDate
  runEvm: async (dataDate) => {
    const cur = get().currentProject
    if (!cur) return null
    set({ loading: true, error: null })
    try {
      const evm = await api.getEvm(cur.project_id, dataDate)
      set({
        evm,
        // 以後端回傳的 data_date 為準（後端可能套用預設）；否則沿用傳入值
        dataDate: evm && evm.data_date != null ? evm.data_date : (dataDate ?? get().dataDate),
        loading: false,
      })
      return evm
    } catch (err) {
      set({ error: extractError(err), loading: false })
      throw err
    }
  },

  // 拋轉 EVM 風險預警（若 risk_flagged 後端才會排入同步事件）-> {dispatched, ...}
  dispatchEvmAlert: async (dataDate) => {
    const cur = get().currentProject
    if (!cur) return null
    set({ loading: true, error: null })
    try {
      const result = await api.dispatchEvmAlert(cur.project_id, dataDate)
      set({ loading: false })
      return result
    } catch (err) {
      set({ error: extractError(err), loading: false })
      throw err
    }
  },

  // ---- Phase 10：儀表板（投資組合 KPI）----

  // 載入租戶層級儀表板彙總；存入 store.dashboard
  loadDashboard: async () => {
    set({ loading: true, error: null })
    try {
      const dashboard = await api.getDashboard()
      set({ dashboard, loading: false })
      return dashboard
    } catch (err) {
      set({ error: extractError(err), loading: false })
      throw err
    }
  },

  // ---- Phase 10：使用者管理（僅 admin）----

  // 載入本租戶使用者清單；存入 store.users
  loadUsers: async () => {
    set({ loading: true, error: null })
    try {
      const users = await api.listUsers()
      set({ users: Array.isArray(users) ? users : [], loading: false })
      return users
    } catch (err) {
      set({ error: extractError(err), loading: false })
      throw err
    }
  },

  // 建立使用者（body {username,password,role,region?}）；成功後刷新清單
  createUser: async (body) => {
    set({ loading: true, error: null })
    try {
      const user = await api.createUser(body)
      set({ loading: false })
      get()
        .loadUsers()
        .catch(() => {})
      return user
    } catch (err) {
      set({ error: extractError(err), loading: false })
      throw err
    }
  },

  // 更新使用者（body {role?,is_active?,password?}）；成功後刷新清單
  updateUser: async (id, body) => {
    set({ loading: true, error: null })
    try {
      const user = await api.updateUser(id, body)
      set({ loading: false })
      get()
        .loadUsers()
        .catch(() => {})
      return user
    } catch (err) {
      set({ error: extractError(err), loading: false })
      throw err
    }
  },

  // 刪除使用者；成功後刷新清單
  deleteUser: async (id) => {
    set({ loading: true, error: null })
    try {
      const result = await api.deleteUser(id)
      set({ loading: false })
      get()
        .loadUsers()
        .catch(() => {})
      return result
    } catch (err) {
      set({ error: extractError(err), loading: false })
      throw err
    }
  },
}))

// 將 store 暴露於全域，供 api/client.js 攔截器讀取 tenantId/region，
// 避免 store 與 client 模組相互循環匯入。
if (typeof globalThis !== 'undefined') {
  globalThis.__cpmScheduleStore = useScheduleStore
}

// 從 axios 錯誤萃取可讀訊息
function extractError(err) {
  if (err && err.response && err.response.data) {
    const d = err.response.data
    if (typeof d === 'string') return d
    if (d.detail) {
      if (typeof d.detail === 'string') return d.detail
      try {
        return JSON.stringify(d.detail)
      } catch (e) {
        return 'Request failed'
      }
    }
  }
  return (err && err.message) || 'Request failed'
}

export default useScheduleStore
