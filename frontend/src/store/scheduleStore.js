import { create } from 'zustand'
import * as api from '../api/client.js'

// localStorage 鍵（與 api/client.js 攔截器共用）
const LS_TENANT_KEY = 'cpm.tenantId'
const LS_REGION_KEY = 'cpm.region'
const LS_TOKEN_KEY = 'cpm.token'

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
  username: null,
  projects: [],
  currentProject: null,
  loading: false,
  error: null,

  // 登入：以帳號/密碼換取 JWT。成功後設定 token + 由權杖回傳的 tenantId/region + username，
  // 並將 token 持久化至 localStorage（攔截器與重新整理後復原皆使用）。
  // 同時持久化 tenantId/region，使標頭回退與 token 一致。
  login: async (username, password) => {
    set({ loading: true, error: null })
    try {
      const data = await api.login(username, password)
      const tenantId = data.tenant_id
      const region = data.region
      writeLS(LS_TOKEN_KEY, data.access_token)
      if (tenantId) writeLS(LS_TENANT_KEY, tenantId)
      if (region) writeLS(LS_REGION_KEY, region)
      set({
        token: data.access_token,
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

  // 登出：清除權杖/使用者與當前專案狀態，並移除 localStorage 權杖。
  logout: () => {
    removeLS(LS_TOKEN_KEY)
    set({ token: null, username: null, currentProject: null, projects: [], error: null })
  },

  // 切換租戶並持久化（攔截器下次請求即帶上新 X-Tenant-Id）。
  // 同時清空舊租戶的專案/清單，避免顯示到其他租戶的殘留資料（RLS 隔離）。
  setTenant: (id) => {
    writeLS(LS_TENANT_KEY, id)
    set({ tenantId: id, currentProject: null, projects: [] })
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
    set({ loading: true, error: null })
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
    set({ loading: true, error: null })
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
