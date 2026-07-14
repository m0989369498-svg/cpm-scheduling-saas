import { create } from 'zustand'
import * as api from '../api/client.js'
import { t } from '../i18n/index.js'

// localStorage 鍵（與 api/client.js 攔截器共用）
const LS_TENANT_KEY = 'cpm.tenantId'
const LS_REGION_KEY = 'cpm.region'
const LS_TOKEN_KEY = 'cpm.token'
// Phase 10：角色（role）持久化，使重新整理後仍可正確顯示/隱藏管理介面與寫入動作。
const LS_ROLE_KEY = 'cpm.role'

// 預設值：示範租戶 TENT-9981，預設區域 TW
const DEFAULT_TENANT = 'TENT-9981'
const DEFAULT_REGION = 'TW'

// Batch 4：載入/錯誤狀態的固定範圍（scope）集合。
// 每個非同步 action 皆屬於其中一個 scope，UI 依 scope 各自顯示
// spinner/錯誤（不再共用單一全域布林）。
export const LOADING_SCOPES = [
  'auth',
  'projects',
  'project',
  'mutation',
  'resources',
  'leveling',
  'risk',
  'simulation',
  'progress',
  'evm',
  'dashboard',
  'users',
  'trash',
  // ---- Pro Batch B：WBS 階層 + 多重命名基準線 ----
  'wbs',
  'baselines',
  // ---- Pro Batch A：P6 XER + MS Project MSPDI XML 匯入 ----
  'import',
  // ---- Pro Batch C：行動裝置現場回報（任務照片 + 離線佇列同步）----
  'photos',
  'fieldQueue',
  // ---- Pro Batch D：資源成本負荷 + DCMA 14 點排程健康度 ----
  'cost',
  'health',
]

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

// Pro Batch C：syncFieldQueue 併發防護旗標（module-level，非 UI 狀態）。
// 行動瀏覽器（Android Chrome / iOS Safari）在分頁喚醒時常誤發 'online' 事件，
// 可能與 FieldMode 掛載時的同步重疊；fieldQueue.replay 是「讀清單 -> 逐項
// 處理 -> 成功才移除」，兩個並行 replay 會在移除前各自讀到同一批項目而重複
// 上傳。以 in-flight 旗標讓重疊觸發直接成為 no-op。
let fieldQueueSyncInFlight = false

// zustand 排程狀態：
// state { tenantId, region, token, username, projects, currentProject,
//         loading: {scope:bool}, errors: {scope:string|null}, loadingAny }
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

  // ---- Batch 4：範圍化 (scoped) 載入/錯誤狀態 ----
  // loading : { [scope]: bool }   — 各 scope 獨立的進行中旗標
  // errors  : { [scope]: string|null } — 各 scope 獨立的錯誤訊息
  // loadingAny : bool（向後相容捷徑：任一 scope 進行中即 true）
  loading: {},
  errors: {},
  loadingAny: false,

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

  // ---- Batch 3：回收桶（軟刪除專案清單，僅 admin 載入）----
  trash: [],

  // ---- Pro Batch B：WBS 階層 + 多重命名基準線 ----
  // wbs       : list[{wbs_code,name,parent_code,sort_order}]（扁平清單；前端以 buildWbsTree 建樹）
  // baselines : list[{id,name,created_at,is_active,project_duration}]（基準線選單）
  wbs: [],
  baselines: [],

  // ---- Pro Batch A：P6 XER + MS Project MSPDI XML 匯入 ----
  // importReport : {format, tasks, wbs, links, constraints, warnings:[string]} | null
  // 最近一次匯入的結果摘要，供 ScheduleBoard 顯示可關閉的報告橫幅/彈窗。
  importReport: null,

  // ---- Pro Batch C：行動裝置現場回報（任務照片 + 離線佇列同步）----
  // photosByTask     : { [taskId]: PhotoOut[] }（lazy；board 徽章展開 / FieldMode 皆共用同一快取）
  // fieldQueueCount  : number（離線佇列待同步筆數；FieldMode 掛載 + 上線事件時刷新）
  photosByTask: {},
  fieldQueueCount: 0,

  // ---- Pro Batch D：資源成本負荷 (D1) + DCMA 14 點排程健康度 (D2) ----
  // cost   : CostResult（total_cost / by_resource / by_category / by_wbs / per_task / cost_curve） | null
  // health : DcmaReport（14 項檢查 + score/passed_count/applicable_count） | null
  cost: null,
  health: null,

  // ---- Batch 4：scoped loading/error 內部輔助 ----

  // _start(scope)：標記 scope 進行中並清除該 scope 的錯誤
  _start: (scope) =>
    set((state) => {
      const loading = { ...state.loading, [scope]: true }
      return {
        loading,
        errors: { ...state.errors, [scope]: null },
        loadingAny: true,
      }
    }),

  // _ok(scope)：標記 scope 完成（成功）
  _ok: (scope) =>
    set((state) => {
      const loading = { ...state.loading, [scope]: false }
      return { loading, loadingAny: anyLoading(loading) }
    }),

  // _fail(scope, err)：標記 scope 完成（失敗）並記錄可讀錯誤訊息
  // err 可為 axios 錯誤物件或已格式化的字串。
  _fail: (scope, err) =>
    set((state) => {
      const loading = { ...state.loading, [scope]: false }
      return {
        loading,
        errors: {
          ...state.errors,
          [scope]: typeof err === 'string' ? err : extractError(err),
        },
        loadingAny: anyLoading(loading),
      }
    }),

  // 清除單一 scope 的錯誤（切換分頁 / 開啟表單時呼叫）
  clearError: (scope) =>
    set((state) => ({ errors: { ...state.errors, [scope]: null } })),

  // 登入：以帳號/密碼換取 JWT。成功後設定 token + 由權杖回傳的 tenantId/region + username，
  // 並將 token 持久化至 localStorage（攔截器與重新整理後復原皆使用）。
  // 同時持久化 tenantId/region，使標頭回退與 token 一致。
  login: async (username, password) => {
    get()._start('auth')
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
      })
      get()._ok('auth')
      return data
    } catch (err) {
      get()._fail('auth', err)
      throw err
    }
  },

  // 登出：清除權杖/使用者與當前專案狀態，並移除 localStorage 權杖/角色。
  logout: () => {
    removeLS(LS_TOKEN_KEY)
    removeLS(LS_ROLE_KEY)
    // Pro Batch C：清除 Service Worker 的 /api/* 回應快取（所有租戶桶，
    // 名稱前綴須與 public/sw.js 的 API_CACHE_PREFIX 一致）。共用工地裝置上，
    // 前一位使用者快取的 API 回應絕不可在下一位登入後的離線回退中被讀到。
    // best-effort：不支援 Cache API 的環境（舊瀏覽器 / jsdom 測試）靜默略過。
    try {
      if (typeof caches !== 'undefined' && caches && typeof caches.keys === 'function') {
        caches
          .keys()
          .then((keys) =>
            Promise.all(
              keys.filter((k) => k.startsWith('cpm-field-api-')).map((k) => caches.delete(k)),
            ),
          )
          .catch(() => {})
      }
    } catch (e) {
      /* Cache API 不可用：略過 */
    }
    set({
      token: null,
      role: null,
      username: null,
      currentProject: null,
      projects: [],
      // 重置 scoped 載入/錯誤狀態
      loading: {},
      errors: {},
      loadingAny: false,
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
      // 重置 Batch 3 回收桶
      trash: [],
      // 重置 Pro Batch B：WBS + 多重命名基準線
      wbs: [],
      baselines: [],
      // 重置 Pro Batch A：匯入報告
      importReport: null,
      // 重置 Pro Batch C：任務照片快取（離線佇列待同步筆數為裝置層級，不隨登出清除）
      photosByTask: {},
      // 重置 Pro Batch D：成本負荷 + DCMA 健康度
      cost: null,
      health: null,
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
      // 切換租戶：清空殘留錯誤（loading 旗標由進行中請求自行收尾）
      errors: {},
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
      // 切換租戶：清空 Batch 3 回收桶（避免跨租戶殘留）
      trash: [],
      // 切換租戶：清空 Pro Batch B WBS + 多重命名基準線（避免跨租戶殘留）
      wbs: [],
      baselines: [],
      // 切換租戶：清空 Pro Batch A 匯入報告（避免跨租戶殘留）
      importReport: null,
      // 切換租戶：清空 Pro Batch C 任務照片快取（避免跨租戶殘留）
      photosByTask: {},
      // 切換租戶：清空 Pro Batch D 成本負荷 + DCMA 健康度（避免跨租戶殘留）
      cost: null,
      health: null,
    })
  },

  // 切換區域並持久化（驅動 i18n 與 X-Region 標頭）
  setRegion: (r) => {
    writeLS(LS_REGION_KEY, r)
    set({ region: r })
  },

  // 載入專案清單
  loadProjects: async () => {
    get()._start('projects')
    try {
      const projects = await api.listProjects()
      set({ projects })
      get()._ok('projects')
      return projects
    } catch (err) {
      get()._fail('projects', err)
      throw err
    }
  },

  // 載入單一專案（含 CPM 結果），設為當前專案
  loadProject: async (id) => {
    // 切換專案：重置 Phase 8/9 分析狀態（撫平/模擬/進度/EVM 結果不可跨專案沿用）
    set({
      resources: null,
      leveling: null,
      risk: [],
      simulation: null,
      progress: [],
      baseline: null,
      evm: null,
      dataDate: null,
      // Pro Batch B：切換/新建專案時重置 WBS + 多重命名基準線（不可跨專案沿用）
      wbs: [],
      baselines: [],
      // Pro Batch C：切換專案時重置任務照片快取（不可跨專案沿用）
      photosByTask: {},
      // Pro Batch D：切換專案時重置成本負荷 + DCMA 健康度（不可跨專案沿用）
      cost: null,
      health: null,
    })
    get()._start('project')
    try {
      const project = await api.getProject(id)
      set({ currentProject: project })
      get()._ok('project')
      return project
    } catch (err) {
      get()._fail('project', err)
      throw err
    }
  },

  // 建立專案，設為當前並刷新清單（建立屬寫入動作 -> scope 'mutation'，
  // 供 ProjectForm 以 mutation scope 顯示送出中/錯誤）
  createProject: async (payload) => {
    set({
      resources: null,
      leveling: null,
      risk: [],
      simulation: null,
      progress: [],
      baseline: null,
      evm: null,
      dataDate: null,
      // Pro Batch B：切換/新建專案時重置 WBS + 多重命名基準線（不可跨專案沿用）
      wbs: [],
      baselines: [],
      // Pro Batch C：新建專案時重置任務照片快取（不可跨專案沿用）
      photosByTask: {},
      // Pro Batch D：新建專案時重置成本負荷 + DCMA 健康度（不可跨專案沿用）
      cost: null,
      health: null,
    })
    get()._start('mutation')
    try {
      const project = await api.createProject(payload)
      set({ currentProject: project })
      get()._ok('mutation')
      // 背景刷新清單（不阻塞）
      get()
        .loadProjects()
        .catch(() => {})
      return project
    } catch (err) {
      get()._fail('mutation', err)
      throw err
    }
  },

  // 拖曳/輸入改工期 -> 後端整案重算 CPM -> 以回傳 ProjectOut 更新當前專案
  // Batch 4：樂觀更新 (optimistic) — 先快照當前專案，立即把新工期套用到
  // currentProject.tasks（甘特圖即時反映、不被 spinner 阻塞），再呼叫 API：
  //   - 成功：以伺服器回傳 ProjectOut 取代（含重算後 es/ef/float/critical）
  //   - 失敗：還原快照 + 設定 errors.mutation
  //   - 409 版本衝突：還原快照後重載專案 + errors.mutation = conflictReloaded
  changeTaskDuration: async (taskId, duration) => {
    const cur = get().currentProject
    if (!cur) return null
    const snapshot = cur
    const dur = Number(duration)
    // 樂觀套用新工期（僅該任務的 duration；es/ef 等待伺服器重算）
    set({
      currentProject: {
        ...cur,
        tasks: (cur.tasks || []).map((tk) =>
          tk.task_id === taskId ? { ...tk, duration: dur } : tk,
        ),
      },
    })
    get()._start('mutation')
    try {
      const project = await api.updateTaskDuration(
        cur.project_id,
        taskId,
        dur,
        cur.version != null ? cur.version : undefined,
      )
      set({ currentProject: project })
      get()._ok('mutation')
      return project
    } catch (err) {
      // 失敗：還原快照（伺服器未接受此工期）
      set({ currentProject: snapshot })
      if (isVersionConflict(err)) {
        await reloadAfterConflict(get, set)
        return null
      }
      get()._fail('mutation', err)
      throw err
    }
  },

  // 新增任務 -> 後端重算 -> 更新當前專案（附帶 expected_version；409 重載+提示）
  addTask: async (task) => {
    const cur = get().currentProject
    if (!cur) return null
    get()._start('mutation')
    try {
      const body = cur.version != null ? { ...task, expected_version: cur.version } : task
      const project = await api.addTask(cur.project_id, body)
      set({ currentProject: project })
      get()._ok('mutation')
      return project
    } catch (err) {
      if (isVersionConflict(err)) {
        await reloadAfterConflict(get, set)
        return null
      }
      get()._fail('mutation', err)
      throw err
    }
  },

  // 刪除任務 -> 後端重算 -> 更新當前專案（附帶 expected_version；409 重載+提示）
  removeTask: async (taskId) => {
    const cur = get().currentProject
    if (!cur) return null
    get()._start('mutation')
    try {
      const project = await api.deleteTask(
        cur.project_id,
        taskId,
        cur.version != null ? cur.version : undefined,
      )
      set({ currentProject: project })
      get()._ok('mutation')
      return project
    } catch (err) {
      if (isVersionConflict(err)) {
        await reloadAfterConflict(get, set)
        return null
      }
      get()._fail('mutation', err)
      throw err
    }
  },

  // ---- Batch 3：依賴編輯（dep_type + lag）----

  // 更新某任務的前置依賴連結（links: [{predecessor_task_id, dep_type, lag_days}]），
  // 選用 extra（Pro Batch B：{constraint_type, constraint_day}）一併併入同一次 PATCH，
  // 避免兩次連續請求造成樂觀鎖版本競態。
  // 以 api.updateTask 送出 PATCH 形狀 {links, ...extra, expected_version}（後端重算 CPM）。
  // 409 版本衝突：重載專案 + 設定 conflictReloaded 錯誤訊息（提示已重載）。
  updateTaskLinks: async (taskId, links, extra = {}) => {
    const cur = get().currentProject
    if (!cur) return null
    get()._start('mutation')
    try {
      const patch = { links, ...extra }
      if (cur.version != null) patch.expected_version = cur.version
      const project = await api.updateTask(cur.project_id, taskId, patch)
      set({ currentProject: project })
      get()._ok('mutation')
      return project
    } catch (err) {
      if (isVersionConflict(err)) {
        await reloadAfterConflict(get, set)
        return null
      }
      get()._fail('mutation', err)
      throw err
    }
  },

  // ---- Batch 3：回收桶（軟刪除/還原/永久刪除，僅 admin）----

  // 載入回收桶清單（軟刪除的專案摘要）；存入 store.trash
  loadTrash: async () => {
    get()._start('trash')
    try {
      const trash = await api.getTrash()
      set({ trash: Array.isArray(trash) ? trash : [] })
      get()._ok('trash')
      return trash
    } catch (err) {
      get()._fail('trash', err)
      throw err
    }
  },

  // 還原軟刪除專案；成功後刷新回收桶與專案清單
  restoreProject: async (id) => {
    get()._start('trash')
    try {
      const result = await api.restoreProject(id)
      get()._ok('trash')
      get()
        .loadTrash()
        .catch(() => {})
      get()
        .loadProjects()
        .catch(() => {})
      return result
    } catch (err) {
      get()._fail('trash', err)
      throw err
    }
  },

  // 永久刪除（硬刪除 cascade）；成功後刷新回收桶
  purgeProject: async (id) => {
    get()._start('trash')
    try {
      const result = await api.purgeProject(id)
      get()._ok('trash')
      get()
        .loadTrash()
        .catch(() => {})
      return result
    } catch (err) {
      get()._fail('trash', err)
      throw err
    }
  },

  // 拋轉當前專案至 ERP（排入同步事件）
  syncErp: async (syncType = 'SCHEDULE_PUSH') => {
    const cur = get().currentProject
    if (!cur) return null
    get()._start('mutation')
    try {
      const result = await api.syncErp(cur.project_id, syncType)
      get()._ok('mutation')
      return result
    } catch (err) {
      get()._fail('mutation', err)
      throw err
    }
  },

  // ---- Phase 8：資源撫平 (resource leveling) ----

  // 載入當前專案資源設定（資源上限 + 各任務需求）
  loadResources: async () => {
    const cur = get().currentProject
    if (!cur) return null
    get()._start('resources')
    try {
      const resources = await api.getResources(cur.project_id)
      set({ resources })
      get()._ok('resources')
      return resources
    } catch (err) {
      get()._fail('resources', err)
      throw err
    }
  },

  // 儲存資源設定（upsert 上限 + 各任務 resource_demands）；回傳並更新 store.resources
  saveResources: async (cfg) => {
    const cur = get().currentProject
    if (!cur) return null
    get()._start('resources')
    try {
      const resources = await api.setResources(cur.project_id, cfg)
      set({ resources })
      get()._ok('resources')
      return resources
    } catch (err) {
      get()._fail('resources', err)
      throw err
    }
  },

  // 執行資源撫平；結果（含 over_capacity_days）存入 store.leveling 供甘特圖警示帶
  runLeveling: async () => {
    const cur = get().currentProject
    if (!cur) return null
    get()._start('leveling')
    try {
      const leveling = await api.levelResources(cur.project_id)
      set({ leveling })
      get()._ok('leveling')
      return leveling
    } catch (err) {
      get()._fail('leveling', err)
      throw err
    }
  },

  // ---- Phase 8：蒙地卡羅風險分析 (Monte Carlo) ----

  // 載入三點估計參數（list[RiskParam]）
  loadRisk: async () => {
    const cur = get().currentProject
    if (!cur) return []
    get()._start('risk')
    try {
      const risk = await api.getRisk(cur.project_id)
      set({ risk: Array.isArray(risk) ? risk : [] })
      get()._ok('risk')
      return risk
    } catch (err) {
      get()._fail('risk', err)
      throw err
    }
  },

  // 儲存三點估計參數（upsert）；回傳並更新 store.risk
  saveRisk: async (list) => {
    const cur = get().currentProject
    if (!cur) return []
    get()._start('risk')
    try {
      const risk = await api.setRisk(cur.project_id, list)
      set({ risk: Array.isArray(risk) ? risk : [] })
      get()._ok('risk')
      return risk
    } catch (err) {
      get()._fail('risk', err)
      throw err
    }
  },

  // 執行蒙地卡羅模擬（req SimulationRequest）；結果存入 store.simulation
  runSimulation: async (req = {}) => {
    const cur = get().currentProject
    if (!cur) return null
    get()._start('simulation')
    try {
      const simulation = await api.simulate(cur.project_id, req)
      set({ simulation })
      get()._ok('simulation')
      return simulation
    } catch (err) {
      get()._fail('simulation', err)
      throw err
    }
  },

  // ---- Phase 9：進度追蹤 + EVM（實獲值管理）----

  // 載入當前專案每任務進度（list[ProgressEntry]）；存入 store.progress
  loadProgress: async () => {
    const cur = get().currentProject
    if (!cur) return []
    get()._start('progress')
    try {
      const progress = await api.getProgress(cur.project_id)
      set({ progress: Array.isArray(progress) ? progress : [] })
      get()._ok('progress')
      return progress
    } catch (err) {
      get()._fail('progress', err)
      throw err
    }
  },

  // 儲存每任務進度（upsert）；回傳並更新 store.progress
  saveProgress: async (list) => {
    const cur = get().currentProject
    if (!cur) return []
    get()._start('progress')
    try {
      const progress = await api.saveProgress(cur.project_id, list)
      set({ progress: Array.isArray(progress) ? progress : [] })
      get()._ok('progress')
      return progress
    } catch (err) {
      get()._fail('progress', err)
      throw err
    }
  },

  // 建立基準線（以目前 CPM + 進度預算為快照）；存入 store.baseline 並設為使用中。
  // Pro Batch B：後端會將此新基準線設為 is_active=true 並清除其餘旗標；
  // 背景刷新 store.baselines（picker 清單），失敗不影響本次建立結果。
  createBaseline: async (name) => {
    const cur = get().currentProject
    if (!cur) return null
    get()._start('progress')
    try {
      const baseline = await api.createBaseline(cur.project_id, name)
      set({ baseline })
      get()._ok('progress')
      get()
        .loadBaselines()
        .catch(() => {})
      return baseline
    } catch (err) {
      get()._fail('progress', err)
      throw err
    }
  },

  // 載入最新基準線；存入 store.baseline。無基準線（404）視為 null（不視為錯誤）。
  loadBaseline: async () => {
    const cur = get().currentProject
    if (!cur) return null
    get()._start('progress')
    try {
      const baseline = await api.getBaseline(cur.project_id)
      set({ baseline })
      get()._ok('progress')
      return baseline
    } catch (err) {
      // 尚無基準線（404）為正常狀態：清空 baseline、不顯示錯誤
      if (err && err.response && err.response.status === 404) {
        set({ baseline: null })
        get()._ok('progress')
        return null
      }
      get()._fail('progress', err)
      throw err
    }
  },

  // ---- Pro Batch B：多重命名基準線（清單 / 設為使用中 / 刪除） ----

  // 載入本專案所有基準線（含 is_active 旗標 + 各自 project_duration）；存入 store.baselines
  loadBaselines: async () => {
    const cur = get().currentProject
    if (!cur) return []
    get()._start('baselines')
    try {
      const baselines = await api.listBaselines(cur.project_id)
      set({ baselines: Array.isArray(baselines) ? baselines : [] })
      get()._ok('baselines')
      return baselines
    } catch (err) {
      get()._fail('baselines', err)
      throw err
    }
  },

  // 設為使用中基準線（後端原子性清除其餘 is_active）；
  // 成功後刷新 store.baselines 清單 + 重載 store.baseline（供甘特圖計畫條/EVM 使用）。
  activateBaseline: async (baselineId) => {
    const cur = get().currentProject
    if (!cur) return null
    get()._start('baselines')
    try {
      const result = await api.activateBaseline(cur.project_id, baselineId)
      get()._ok('baselines')
      await Promise.all([
        get()
          .loadBaselines()
          .catch(() => {}),
        get()
          .loadBaseline()
          .catch(() => {}),
      ])
      return result
    } catch (err) {
      get()._fail('baselines', err)
      throw err
    }
  },

  // 刪除基準線（若刪除的是使用中基準線，後端會自動將最新剩餘者設為使用中，
  // 若無剩餘則 store.baseline 重載後為 null）；成功後刷新清單 + 使用中基準線。
  deleteBaseline: async (baselineId) => {
    const cur = get().currentProject
    if (!cur) return null
    get()._start('baselines')
    try {
      const result = await api.deleteBaseline(cur.project_id, baselineId)
      get()._ok('baselines')
      await Promise.all([
        get()
          .loadBaselines()
          .catch(() => {}),
        get()
          .loadBaseline()
          .catch(() => {}),
      ])
      return result
    } catch (err) {
      get()._fail('baselines', err)
      throw err
    }
  },

  // ---- Pro Batch B：WBS 階層 (work breakdown structure) ----

  // 載入當前專案 WBS 節點（扁平清單）；存入 store.wbs
  loadWbs: async () => {
    const cur = get().currentProject
    if (!cur) return []
    get()._start('wbs')
    try {
      const wbs = await api.getWbs(cur.project_id)
      set({ wbs: Array.isArray(wbs) ? wbs : [] })
      get()._ok('wbs')
      return wbs
    } catch (err) {
      get()._fail('wbs', err)
      throw err
    }
  },

  // 儲存 WBS 節點（整批取代 upsert）；成功後以後端回傳的正規化清單更新 store.wbs
  saveWbs: async (list) => {
    const cur = get().currentProject
    if (!cur) return []
    get()._start('wbs')
    try {
      const wbs = await api.saveWbs(cur.project_id, list)
      set({ wbs: Array.isArray(wbs) ? wbs : [] })
      get()._ok('wbs')
      return wbs
    } catch (err) {
      get()._fail('wbs', err)
      throw err
    }
  },

  // ---- Pro Batch D：資源成本負荷 (D1) ----

  // 載入當前專案成本負荷（依 CPM 持久化欄位 + 資源費率/類別彙總）；存入 store.cost
  loadCost: async () => {
    const cur = get().currentProject
    if (!cur) return null
    get()._start('cost')
    try {
      const cost = await api.getCost(cur.project_id)
      set({ cost })
      get()._ok('cost')
      return cost
    } catch (err) {
      get()._fail('cost', err)
      throw err
    }
  },

  // ---- Pro Batch D：DCMA 14 點排程健康度評估 (D2) ----

  // 載入當前專案 DCMA 14 點排程健康度評估（dataDate 選用，預設專案總工期）；存入 store.health
  loadHealth: async (dataDate) => {
    const cur = get().currentProject
    if (!cur) return null
    get()._start('health')
    try {
      const health = await api.getHealth(cur.project_id, dataDate)
      set({ health })
      get()._ok('health')
      return health
    } catch (err) {
      get()._fail('health', err)
      throw err
    }
  },

  // ---- Pro Batch A：P6 XER + MS Project MSPDI XML 匯入 ----

  // 匯入專案檔案（P6 .xer 或 MS Project MSPDI .xml）：後端解析建檔（新專案 + WBS +
  // 任務 + 依賴 + 限制條件）後回傳 {project, report}。成功後：
  //   - 重置 Phase 8/9 分析狀態與 WBS/基準線（同 createProject，新專案不可沿用舊狀態）
  //   - 設為當前專案（project）
  //   - 將 report（counts + warnings）存入 store.importReport 供橫幅顯示
  //   - 背景刷新專案清單（不阻塞）
  // file  : File（<input type="file"> 選取）
  // opts  : { format？: 'auto'|'xer'|'mspdi', hoursPerDay？: number, projectId？: string }
  importProject: async (file, opts = {}) => {
    set({
      resources: null,
      leveling: null,
      risk: [],
      simulation: null,
      progress: [],
      baseline: null,
      evm: null,
      dataDate: null,
      wbs: [],
      baselines: [],
      photosByTask: {},
      cost: null,
      health: null,
    })
    get()._start('import')
    try {
      const result = await api.importProject(file, opts)
      set({
        currentProject: result.project,
        importReport: result.report || null,
      })
      get()._ok('import')
      get()
        .loadProjects()
        .catch(() => {})
      return result
    } catch (err) {
      get()._fail('import', err)
      throw err
    }
  },

  // 關閉匯入報告橫幅（僅清除顯示用的 report，不影響 currentProject）
  clearImportReport: () => set({ importReport: null }),

  // 計算 EVM（dataDate 選用，預設基準線總工期）；結果存入 store.evm，並記錄使用的 dataDate
  runEvm: async (dataDate) => {
    const cur = get().currentProject
    if (!cur) return null
    get()._start('evm')
    try {
      const evm = await api.getEvm(cur.project_id, dataDate)
      set({
        evm,
        // 以後端回傳的 data_date 為準（後端可能套用預設）；否則沿用傳入值
        dataDate: evm && evm.data_date != null ? evm.data_date : (dataDate ?? get().dataDate),
      })
      get()._ok('evm')
      return evm
    } catch (err) {
      get()._fail('evm', err)
      throw err
    }
  },

  // 拋轉 EVM 風險預警（若 risk_flagged 後端才會排入同步事件）-> {dispatched, ...}
  dispatchEvmAlert: async (dataDate) => {
    const cur = get().currentProject
    if (!cur) return null
    get()._start('evm')
    try {
      const result = await api.dispatchEvmAlert(cur.project_id, dataDate)
      get()._ok('evm')
      return result
    } catch (err) {
      get()._fail('evm', err)
      throw err
    }
  },

  // ---- Phase 10：儀表板（投資組合 KPI）----

  // 載入租戶層級儀表板彙總；存入 store.dashboard
  loadDashboard: async () => {
    get()._start('dashboard')
    try {
      const dashboard = await api.getDashboard()
      set({ dashboard })
      get()._ok('dashboard')
      return dashboard
    } catch (err) {
      get()._fail('dashboard', err)
      throw err
    }
  },

  // ---- Phase 10：使用者管理（僅 admin）----

  // 載入本租戶使用者清單；存入 store.users
  loadUsers: async () => {
    get()._start('users')
    try {
      const users = await api.listUsers()
      set({ users: Array.isArray(users) ? users : [] })
      get()._ok('users')
      return users
    } catch (err) {
      get()._fail('users', err)
      throw err
    }
  },

  // 建立使用者（body {username,password,role,region?}）；成功後刷新清單
  createUser: async (body) => {
    get()._start('users')
    try {
      const user = await api.createUser(body)
      get()._ok('users')
      get()
        .loadUsers()
        .catch(() => {})
      return user
    } catch (err) {
      get()._fail('users', err)
      throw err
    }
  },

  // 更新使用者（body {role?,is_active?,password?}）；成功後刷新清單
  updateUser: async (id, body) => {
    get()._start('users')
    try {
      const user = await api.updateUser(id, body)
      get()._ok('users')
      get()
        .loadUsers()
        .catch(() => {})
      return user
    } catch (err) {
      get()._fail('users', err)
      throw err
    }
  },

  // 刪除使用者；成功後刷新清單
  deleteUser: async (id) => {
    get()._start('users')
    try {
      const result = await api.deleteUser(id)
      get()._ok('users')
      get()
        .loadUsers()
        .catch(() => {})
      return result
    } catch (err) {
      get()._fail('users', err)
      throw err
    }
  },

  // ---- Pro Batch C：行動裝置現場回報（任務照片 + QR 深連結 + 離線佇列同步）----

  // 載入單一任務照片清單（lazy；board 徽章展開 / FieldMode 任務卡片皆呼叫此 action）；
  // 存入 store.photosByTask[taskId]。
  loadTaskPhotos: async (taskId) => {
    const cur = get().currentProject
    if (!cur) return []
    get()._start('photos')
    try {
      const list = await api.listTaskPhotos(cur.project_id, taskId)
      set((state) => ({
        photosByTask: { ...state.photosByTask, [taskId]: Array.isArray(list) ? list : [] },
      }))
      get()._ok('photos')
      return list
    } catch (err) {
      get()._fail('photos', err)
      throw err
    }
  },

  // 上傳任務照片（FormData file + note 選填）；成功後附加至 store.photosByTask[taskId]。
  uploadTaskPhoto: async (taskId, file, note) => {
    const cur = get().currentProject
    if (!cur) return null
    get()._start('photos')
    try {
      const photo = await api.uploadTaskPhoto(cur.project_id, taskId, file, note)
      set((state) => ({
        photosByTask: {
          ...state.photosByTask,
          [taskId]: [...(state.photosByTask[taskId] || []), photo],
        },
      }))
      get()._ok('photos')
      return photo
    } catch (err) {
      get()._fail('photos', err)
      throw err
    }
  },

  // 刪除任務照片（editor+）；成功後自 store.photosByTask[taskId] 移除該筆。
  deleteTaskPhoto: async (taskId, photoId) => {
    get()._start('photos')
    try {
      const result = await api.deleteTaskPhoto(photoId)
      set((state) => ({
        photosByTask: {
          ...state.photosByTask,
          [taskId]: (state.photosByTask[taskId] || []).filter((p) => p.id !== photoId),
        },
      }))
      get()._ok('photos')
      return result
    } catch (err) {
      get()._fail('photos', err)
      throw err
    }
  },

  // 任務 QR 深連結圖片 URL（純字串組裝，不呼叫 API；當前專案不存在時回空字串）。
  qrUrl: (taskId) => {
    const cur = get().currentProject
    if (!cur) return ''
    return api.qrUrl(cur.project_id, taskId)
  },

  // ---- 離線佇列（frontend/src/offline/fieldQueue.js）----
  // 以動態匯入載入離線佇列模組：該模組屬另一併行工作項的產出，且本 store 被
  // 既有測試套件廣泛匯入 (scheduleStore.test.js / client.test.js / wbsBaselines.test.js
  // 等)；若在檔案頂層靜態匯入，一旦該模組尚未落地會使上述既有測試全數因模組
  // 解析失敗而炸掉。動態匯入 + try/catch 讓佇列模組缺席時本 action 靜默降級為
  // no-op（fieldQueueCount 維持 0 / 既有值），既有測試與其餘 store 功能不受影響。

  // 刷新待同步筆數（FieldMode 掛載 / 佇列異動後呼叫）。
  refreshFieldQueueCount: async () => {
    try {
      const mod = await import('../offline/fieldQueue.js')
      const pending = await mod.listPending()
      const count = Array.isArray(pending) ? pending.length : 0
      set({ fieldQueueCount: count })
      return pending
    } catch (e) {
      /* 離線佇列模組尚未就緒或瀏覽器不支援：靜默略過 */
      return []
    }
  },

  // 同步離線佇列：FIFO 重播每筆（progress -> 併入現有進度清單後 saveProgress；
  // photo -> uploadTaskPhoto）；任一筆失敗即停止並保留該筆（維持順序，待下次
  // 連線/呼叫再繼續，見 fieldQueue.replay 的「stop-and-keep」語意）。
  syncFieldQueue: async () => {
    // 併發防護：重疊觸發（掛載 + 誤發的 'online' 事件）直接 no-op，
    // 避免兩個並行 replay 讀到同一批待同步項目而重複上傳（見旗標宣告處註解）。
    if (fieldQueueSyncInFlight) return { ok: 0, failed: 0, skipped: true }
    fieldQueueSyncInFlight = true
    get()._start('fieldQueue')
    try {
      const mod = await import('../offline/fieldQueue.js')
      const result = await mod.replay({
        progress: async (item) => {
          const list = await api.getProgress(item.projectId).catch(() => [])
          const arr = Array.isArray(list) ? list : []
          // 未回報欄位（budget / 實際起訖日）以伺服器「目前」的同任務進度列
          // 補齊，item.payload（使用者實際編輯的欄位）最後展開覆蓋。舊版佇列
          // 項目可能攜帶完整欄位快照，展開順序保證其行為不變（向後相容）。
          const existing = arr.find((p) => p && p.task_id === item.taskId) || {}
          const merged = arr.filter((p) => p && p.task_id !== item.taskId)
          merged.push({
            task_id: item.taskId,
            budget: Number(existing.budget) || 0,
            actual_start_day: existing.actual_start_day ?? null,
            actual_finish_day: existing.actual_finish_day ?? null,
            ...item.payload,
          })
          await api.saveProgress(item.projectId, merged)
        },
        photo: async (item) => {
          const p = item.payload || {}
          await api.uploadTaskPhoto(item.projectId, item.taskId, p.blob, p.note)
        },
      })
      await get().refreshFieldQueueCount()
      get()._ok('fieldQueue')
      // 專案未變更時，同步後刷新目前進度（讓 FieldMode/ScheduleBoard 顯示最新完成度）
      if (get().currentProject) {
        get()
          .loadProgress()
          .catch(() => {})
      }
      return result
    } catch (e) {
      await get().refreshFieldQueueCount()
      get()._fail('fieldQueue', e)
      return { ok: false, failed: -1 }
    } finally {
      fieldQueueSyncInFlight = false
    }
  },
}))

// 將 store 暴露於全域，供 api/client.js 攔截器讀取 tenantId/region
// 與 401 回應攔截器執行 logout/設定 errors.auth，
// 避免 store 與 client 模組相互循環匯入。
if (typeof globalThis !== 'undefined') {
  globalThis.__cpmScheduleStore = useScheduleStore
}

// ---- Batch 4：selector 風格輔助（元件以 isLoading(state, scope) 讀取） ----

// 任一 scope 進行中？
function anyLoading(loadingMap) {
  return Object.values(loadingMap || {}).some(Boolean)
}

// isLoading(state, scope) -> bool：該 scope 是否進行中
export function isLoading(state, scope) {
  return Boolean(state && state.loading && state.loading[scope])
}

// getError(state, scope) -> string|null：該 scope 的錯誤訊息
export function getError(state, scope) {
  return (state && state.errors && state.errors[scope]) || null
}

// Batch 3：判斷是否為樂觀鎖版本衝突（HTTP 409）
function isVersionConflict(err) {
  return Boolean(err && err.response && err.response.status === 409)
}

// Batch 3：版本衝突後處理 — 重載當前專案（取得最新版本），
// 並設定 errors.mutation = conflictReloaded 提示使用者「已重新載入」。
async function reloadAfterConflict(get, set) {
  const cur = get().currentProject
  if (cur && cur.project_id) {
    try {
      await get().loadProject(cur.project_id)
    } catch (e) {
      /* 重載失敗：保留 loadProject 設定的錯誤之外，仍以衝突訊息覆蓋 */
    }
  }
  set((state) => {
    const loading = { ...state.loading, mutation: false }
    return {
      loading,
      errors: { ...state.errors, mutation: t(get().region, 'conflictReloaded') },
      loadingAny: anyLoading(loading),
    }
  })
}

// 從 axios 錯誤萃取可讀訊息。
// Batch 4：FastAPI 陣列形 detail（422 驗證錯誤）改為人類可讀格式
// "loc.path: msg; loc.path: msg"（不再 JSON.stringify）。
export function extractError(err) {
  if (err && err.response && err.response.data) {
    const d = err.response.data
    if (typeof d === 'string') return d
    if (d.detail != null) {
      if (typeof d.detail === 'string') return d.detail
      if (Array.isArray(d.detail)) {
        const parts = d.detail
          .map((entry) => {
            if (entry == null) return ''
            if (typeof entry === 'string') return entry
            const loc = Array.isArray(entry.loc)
              ? entry.loc.join('.')
              : entry.loc != null
                ? String(entry.loc)
                : ''
            const msg = entry.msg || entry.message || ''
            if (loc && msg) return `${loc}: ${msg}`
            return loc || msg
          })
          .filter(Boolean)
        if (parts.length > 0) return parts.join('; ')
        return 'Request failed'
      }
      try {
        return JSON.stringify(d.detail)
      } catch (e) {
        return 'Request failed'
      }
    }
  }
  return (err && err.message) || 'Request failed'
}

// ---- Batch 4：工作階段復原 (session restore) ----
// store 建立時若已有持久化權杖，非同步呼叫 GET /auth/me 復原
// username/tenantId/region/role（重新整理後標頭仍能顯示登入者）。
// 失敗（401 權杖過期）由 api/client.js 的回應攔截器統一處理登出。
function restoreSession(store) {
  let token = null
  try {
    token = store.getState().token
  } catch (e) {
    return
  }
  if (!token) return
  api
    .me()
    .then((info) => {
      if (!info) return
      const patch = {}
      if (info.username) patch.username = info.username
      if (info.tenant_id) {
        patch.tenantId = info.tenant_id
        writeLS(LS_TENANT_KEY, info.tenant_id)
      }
      if (info.region) {
        patch.region = info.region
        writeLS(LS_REGION_KEY, info.region)
      }
      if (info.role) {
        patch.role = info.role
        writeLS(LS_ROLE_KEY, info.role)
      }
      store.setState(patch)
    })
    .catch(() => {
      /* 401 由回應攔截器登出；其他錯誤靜默（保留現有狀態） */
    })
}

restoreSession(useScheduleStore)

export default useScheduleStore
