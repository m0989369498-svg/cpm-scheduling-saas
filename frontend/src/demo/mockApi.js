// Pro Batch F1 — 獨立展示站 (GitHub Pages) 的瀏覽器端模擬後端。
//
// install()：在 frontend/src/api/client.js 匯出的 apiClient（axios 實例）上
// 安裝自訂 adapter，讓所有 axios 請求（登入、專案 CRUD、CPM 編輯、進度/EVM/
// 資源/風險/成本/健康度/資源池…）一律在記憶體中處理，不發出任何網路請求；
// 並注入頁面頂端的示範模式提示橫幅。
//
// 種子資料：安裝時深拷貝 34 份由真實後端擷取的 fixtures（見 ./fixtures/），
// 深拷貝避免變更外洩回靜態匯入物件（重新整理頁面 = 完全重置，因整個模組
// 連同其 import 一起被瀏覽器重新載入）。
//
// 即時重算：任務工期/新增/刪除/依賴/限制條件的變更會呼叫 ./cpmLite.js 重新
// 執行 CPM（前向/後向掃描），並在有開工日期時以 ./workcalLite.js 重建
// day_dates；資源撫平 / 蒙地卡羅模擬 / 資源分配 / 成本負荷 / DCMA 健康度
// 則一律回傳擷取當時的凍結快照（見頁首橫幅提示）。
//
// 下載端點（匯出 xlsx/pdf/xer/mspdi、報表、任務 QR）不經過 axios，而是由
// ScheduleBoard.jsx 的 downloadWithAuth / QR 檢視器以「原生 fetch」直接打
// VITE_API_BASE_URL 相對路徑；demo 建置將該路徑設為相對值（'api/v1'，無前導
// 斜線），使其在 Pages 上解析為 /cpm-scheduling-saas/demo/api/v1/...，
// 由 .github/workflows/pages.yml 於組裝 _site 時複製對應的靜態範例檔提供
// （見該檔），本模組完全不處理這些請求。

import { apiClient } from '../api/client.js'
import { calculateCpm } from './cpmLite.js'
import { dayDates } from './workcalLite.js'

// ---- Fixtures（靜態匯入；deep-clone 後才寫入可變的記憶體 state）----
import loginFixture from './fixtures/login.json'
import meFixtureUnused from './fixtures/me.json' // 保留匯入以便對照欄位形狀；實際 /auth/me 由 token 解出（見 parseToken）
import projectsFixture from './fixtures/projects.json'
import dashboardFixture from './fixtures/dashboard.json'
import usersFixture from './fixtures/users.json'
import poolFixture from './fixtures/pool.json'
import allocationFixture from './fixtures/allocation.json'
import trashFixture from './fixtures/trash.json'

import project001 from './fixtures/project__PRJ_2026_TW_001.json'
import wbs001 from './fixtures/wbs__PRJ_2026_TW_001.json'
import holidays001 from './fixtures/holidays__PRJ_2026_TW_001.json'
import baselines001 from './fixtures/baselines__PRJ_2026_TW_001.json'
import baseline001 from './fixtures/baseline__PRJ_2026_TW_001.json'
import progress001 from './fixtures/progress__PRJ_2026_TW_001.json'
import evm001 from './fixtures/evm__PRJ_2026_TW_001.json'
import resources001 from './fixtures/resources__PRJ_2026_TW_001.json'
import risk001 from './fixtures/risk__PRJ_2026_TW_001.json'
import cost001 from './fixtures/cost__PRJ_2026_TW_001.json'
import health001 from './fixtures/health__PRJ_2026_TW_001.json'
import level001 from './fixtures/level__PRJ_2026_TW_001.json'
import simulate001 from './fixtures/simulate__PRJ_2026_TW_001.json'

import projectPar from './fixtures/project__PRJ_2026_TW_PARALLEL.json'
import wbsPar from './fixtures/wbs__PRJ_2026_TW_PARALLEL.json'
import holidaysPar from './fixtures/holidays__PRJ_2026_TW_PARALLEL.json'
import baselinesPar from './fixtures/baselines__PRJ_2026_TW_PARALLEL.json'
import baselinePar from './fixtures/baseline__PRJ_2026_TW_PARALLEL.json'
import progressPar from './fixtures/progress__PRJ_2026_TW_PARALLEL.json'
import evmPar from './fixtures/evm__PRJ_2026_TW_PARALLEL.json'
import resourcesPar from './fixtures/resources__PRJ_2026_TW_PARALLEL.json'
import riskPar from './fixtures/risk__PRJ_2026_TW_PARALLEL.json'
import costPar from './fixtures/cost__PRJ_2026_TW_PARALLEL.json'
import healthPar from './fixtures/health__PRJ_2026_TW_PARALLEL.json'
import levelPar from './fixtures/level__PRJ_2026_TW_PARALLEL.json'
import simulatePar from './fixtures/simulate__PRJ_2026_TW_PARALLEL.json'

void meFixtureUnused

const PROJECT_FIXTURES = {
  'PRJ-2026-TW-001': {
    project: project001,
    wbs: wbs001,
    holidays: holidays001,
    baselines: baselines001,
    baseline: baseline001,
    progress: progress001,
    evm: evm001,
    resources: resources001,
    risk: risk001,
    cost: cost001,
    health: health001,
    level: level001,
    simulate: simulate001,
  },
  'PRJ-2026-TW-PARALLEL': {
    project: projectPar,
    wbs: wbsPar,
    holidays: holidaysPar,
    baselines: baselinesPar,
    baseline: baselinePar,
    progress: progressPar,
    evm: evmPar,
    resources: resourcesPar,
    risk: riskPar,
    cost: costPar,
    health: healthPar,
    level: levelPar,
    simulate: simulatePar,
  },
}

const DEMO_PASSWORD = 'demo1234'
const ROLE_BY_USERNAME = { 'admin@tw': 'admin', 'editor@tw': 'editor', 'viewer@tw': 'viewer' }
const TENANT_ID = 'TENT-9981'
const REGION = 'TW'

function clone(value) {
  return value == null ? value : JSON.parse(JSON.stringify(value))
}

// ---- 記憶體 state（module-level；install() 時（重）建立）----
let state = null
const warnedPaths = new Set()

function projectSummary(pstate) {
  const p = pstate.project
  return {
    project_id: p.project_id,
    project_name: p.project_name,
    region: p.region,
    tenant_id: p.tenant_id,
    task_count: (p.tasks || []).length,
    project_duration: p.project_duration,
  }
}

function makeProjectState(fixtureSet) {
  const baseline = clone(fixtureSet.baseline)
  const baselineDetails = {}
  if (baseline) baselineDetails[baseline.id] = baseline
  const baselines = clone(fixtureSet.baselines) || []
  const maxBaselineId = baselines.reduce((m, b) => Math.max(m, Number(b.id) || 0), 0)
  return {
    project: clone(fixtureSet.project),
    wbs: clone(fixtureSet.wbs) || [],
    holidays: clone(fixtureSet.holidays) || [],
    baselines,
    baselineDetails,
    baseline,
    progress: clone(fixtureSet.progress) || [],
    evm: clone(fixtureSet.evm),
    resources: clone(fixtureSet.resources),
    risk: clone(fixtureSet.risk) || [],
    cost: clone(fixtureSet.cost),
    health: clone(fixtureSet.health),
    level: clone(fixtureSet.level),
    simulate: clone(fixtureSet.simulate),
    photos: {},
    nextBaselineId: maxBaselineId + 1,
    nextPhotoId: 1,
  }
}

function resetState() {
  const projects = {}
  for (const pid of Object.keys(PROJECT_FIXTURES)) {
    projects[pid] = makeProjectState(PROJECT_FIXTURES[pid])
  }
  const nextUserId = clone(usersFixture).reduce((m, u) => Math.max(m, Number(u.id) || 0), 0) + 1
  state = {
    users: clone(usersFixture),
    nextUserId,
    dashboard: clone(dashboardFixture),
    pool: clone(poolFixture),
    allocation: clone(allocationFixture),
    trash: clone(trashFixture) || [],
    trashedIds: new Set(),
    projectsList: clone(projectsFixture) || [],
    projects,
    nextProjectSeq: 1,
  }
}

// ---- axios 錯誤/回應輔助 ----

function httpError(status, data) {
  const err = new Error(`demo mock http ${status}`)
  err.__mockHttp = true
  err.status = status
  err.data = data
  return err
}

function makeResponse(status, data, config) {
  return { data, status, statusText: String(status), headers: {}, config, request: {} }
}

function makeAxiosError(status, data, config) {
  const error = new Error(`Request failed with status code ${status}`)
  error.isAxiosError = true
  error.config = config
  error.response = { data, status, statusText: String(status), headers: {}, config }
  return error
}

function getAuthHeader(config) {
  const h = config && config.headers
  if (!h) return null
  if (typeof h.get === 'function') {
    return h.get('Authorization') || h.get('authorization') || null
  }
  return h.Authorization || h.authorization || null
}

function parseToken(authHeader) {
  if (!authHeader) return null
  const m = /^Bearer\s+(.+)$/i.exec(String(authHeader).trim())
  const token = m ? m[1] : String(authHeader)
  const parts = token.split('::')
  if (parts.length !== 3 || parts[0] !== 'demo') return null
  return { username: parts[1], role: parts[2] }
}

function getPstate(pid) {
  const p = state.projects[pid]
  if (!p) throw httpError(404, { detail: `專案不存在（demo）：${pid}` })
  return p
}

function checkVersion(pstate, body) {
  if (body && body.expected_version != null && body.expected_version !== pstate.project.version) {
    throw httpError(409, { detail: '版本衝突（version conflict）', current_version: pstate.project.version })
  }
}

function refreshDayDates(pstate) {
  const p = pstate.project
  if (p.start_date) {
    const holidayDates = (pstate.holidays || []).map((h) => h.holiday_date).filter(Boolean)
    p.day_dates = dayDates(p.start_date, p.project_duration, p.work_days || '1111111', holidayDates)
  } else {
    p.day_dates = null
  }
}

function syncProjectSummary(pstate) {
  const summary = projectSummary(pstate)
  const idx = state.projectsList.findIndex((s) => s.project_id === summary.project_id)
  if (idx >= 0) state.projectsList[idx] = summary
  const tIdx = state.trash.findIndex((s) => s.project_id === summary.project_id)
  if (tIdx >= 0) state.trash[tIdx] = summary
}

// 以 cpmLite 重算整案 CPM，回寫 tasks/project_duration/version/day_dates/wbs，
// 回傳更新後的 ProjectOut（與後端「拖曳改工期/新增/刪除/編輯依賴」端點一致）。
function recomputeProject(pstate) {
  let results
  try {
    results = calculateCpm(pstate.project.tasks)
  } catch (e) {
    throw httpError(422, { detail: e && e.message ? e.message : String(e) })
  }
  pstate.project.tasks = pstate.project.tasks.map((t) => results[t.task_id])
  const efValues = Object.values(results).map((r) => r.ef)
  pstate.project.project_duration = efValues.length ? Math.max(...efValues) : 0
  pstate.project.version = (pstate.project.version || 0) + 1
  pstate.project.wbs = pstate.wbs || []
  refreshDayDates(pstate)
  syncProjectSummary(pstate)
  return pstate.project
}

// ---- Auth ----

function handleLogin(body) {
  const username = body && body.username
  const password = body && body.password
  const role = ROLE_BY_USERNAME[username]
  if (!role || password !== DEMO_PASSWORD) {
    throw httpError(401, { detail: '帳號或密碼錯誤（demo 帳號：admin@tw / editor@tw / viewer@tw，密碼 demo1234）' })
  }
  const token = `demo::${username}::${role}`
  return {
    data: { ...clone(loginFixture), access_token: token, role },
  }
}

function handleMe(config) {
  const parsed = parseToken(getAuthHeader(config))
  if (!parsed) throw httpError(401, { detail: '未登入或權杖無效（demo）' })
  return { data: { username: parsed.username, tenant_id: TENANT_ID, region: REGION, role: parsed.role } }
}

// ---- Users ----

function handleCreateUser(body) {
  const user = {
    id: state.nextUserId,
    tenant_id: TENANT_ID,
    username: (body && body.username) || `user${state.nextUserId}@tw`,
    role: (body && body.role) || 'viewer',
    region: (body && body.region) || REGION,
    is_active: true,
    created_at: new Date().toISOString(),
  }
  state.nextUserId += 1
  state.users.push(user)
  return { data: user }
}

function handleUpdateUser(id, body) {
  const idx = state.users.findIndex((u) => String(u.id) === String(id))
  if (idx < 0) throw httpError(404, { detail: '使用者不存在（demo）' })
  const patch = {}
  if (body && body.role != null) patch.role = body.role
  if (body && body.is_active != null) patch.is_active = body.is_active
  // password 變更：demo 帳密固定為 demo1234，僅回應成功不實際變更登入密碼。
  state.users[idx] = { ...state.users[idx], ...patch }
  return { data: state.users[idx] }
}

function handleDeleteUser(id) {
  const idx = state.users.findIndex((u) => String(u.id) === String(id))
  if (idx < 0) throw httpError(404, { detail: '使用者不存在（demo）' })
  state.users.splice(idx, 1)
  return { data: { ok: true } }
}

// ---- 資源池 / 投資組合資源分配 ----

function handleSavePool(body) {
  const list = Array.isArray(body) ? body : []
  for (const item of list) {
    const idx = state.pool.findIndex((p) => p.resource_type === item.resource_type)
    if (idx >= 0) state.pool[idx] = { ...state.pool[idx], ...item }
    else state.pool.push(item)
  }
  return { data: state.pool }
}

// ---- 專案 CRUD ----

function nextGeneratedProjectId() {
  let pid
  do {
    pid = `PRJ-DEMO-${String(state.nextProjectSeq).padStart(3, '0')}`
    state.nextProjectSeq += 1
  } while (state.projects[pid])
  return pid
}

function handleCreateProject(body) {
  const pid = (body && body.project_id && String(body.project_id).trim()) || nextGeneratedProjectId()
  if (state.projects[pid] && !state.trashedIds.has(pid)) {
    throw httpError(409, { detail: `專案已存在（demo）：${pid}` })
  }
  const scheduleData = Array.isArray(body && body.schedule_data) ? body.schedule_data : []
  const tasks = scheduleData.map((t) => ({
    task_id: t.task_id,
    task_name: t.task_name || '',
    duration: Number(t.duration) || 0,
    predecessors: Array.isArray(t.predecessors) ? t.predecessors : [],
    status: t.status || 'PENDING',
    links: null,
    wbs_code: null,
    constraint_type: null,
    constraint_day: null,
    resource_demands: null,
  }))
  const project = {
    project_id: pid,
    project_name: (body && body.project_name) || pid,
    region: (body && body.region) || REGION,
    tenant_id: TENANT_ID,
    start_date: (body && body.start_date) || null,
    work_days: (body && body.work_days) || '1111111',
    version: 0,
    tasks,
    wbs: [],
    day_dates: null,
    project_duration: 0,
  }
  const pstate = {
    project,
    wbs: [],
    holidays: [],
    baselines: [],
    baselineDetails: {},
    baseline: null,
    progress: [],
    evm: null,
    resources: null,
    risk: [],
    cost: null,
    health: null,
    level: null,
    simulate: null,
    photos: {},
    nextBaselineId: 1,
    nextPhotoId: 1,
  }
  state.projects[pid] = pstate
  state.trashedIds.delete(pid)
  recomputeProject(pstate)
  state.projectsList.push(projectSummary(pstate))
  return { data: pstate.project }
}

// ---- Pro Batch A 匯入（POST /projects/import；multipart）----
//
// demo 模式不在瀏覽器端實作 P6 XER / MSPDI XML 解析器（後端的解析器依賴
// server-side 程式庫），改為：依上傳檔名/表單參數判斷格式後，落地一個固定的
// 「示範匯入專案」（含 WBS、四型態相依連結、活動限制條件），經 cpmLite 重算後
// 以與後端相同的 {project, report} 形狀回傳；report.warnings 明確告知檔案內容
// 未被實際解析，避免誤導。

function makeImportDemoTasks() {
  return [
    { task_id: 'IM-010', task_name: '開工整備', duration: 3, predecessors: [], links: [], status: 'PENDING', wbs_code: 'IM', constraint_type: null, constraint_day: null, resource_demands: null },
    { task_id: 'IM-020', task_name: '基礎開挖', duration: 8, predecessors: [], links: [{ predecessor_task_id: 'IM-010', dep_type: 'FS', lag_days: 0 }], status: 'PENDING', wbs_code: 'IM.1', constraint_type: null, constraint_day: null, resource_demands: null },
    { task_id: 'IM-030', task_name: '結構工程', duration: 12, predecessors: [], links: [{ predecessor_task_id: 'IM-020', dep_type: 'FS', lag_days: 0 }], status: 'PENDING', wbs_code: 'IM.1', constraint_type: null, constraint_day: null, resource_demands: null },
    { task_id: 'IM-040', task_name: '機電配管', duration: 10, predecessors: [], links: [{ predecessor_task_id: 'IM-030', dep_type: 'SS', lag_days: 4 }], status: 'PENDING', wbs_code: 'IM.1', constraint_type: null, constraint_day: null, resource_demands: null },
    { task_id: 'IM-050', task_name: '內裝修飾', duration: 9, predecessors: [], links: [{ predecessor_task_id: 'IM-030', dep_type: 'FS', lag_days: 0 }, { predecessor_task_id: 'IM-040', dep_type: 'FF', lag_days: 2 }], status: 'PENDING', wbs_code: 'IM.1', constraint_type: null, constraint_day: null, resource_demands: null },
    { task_id: 'IM-060', task_name: '竣工驗收', duration: 2, predecessors: [], links: [{ predecessor_task_id: 'IM-050', dep_type: 'FS', lag_days: 0 }], status: 'PENDING', wbs_code: 'IM', constraint_type: 'SNET', constraint_day: 30, resource_demands: null },
  ]
}

function handleImportProject(body) {
  if (!body || typeof body.get !== 'function') {
    throw httpError(422, { detail: '需要 multipart/form-data（demo）' })
  }
  const file = body.get('file')
  if (!file) throw httpError(422, { detail: 'file 為必填（demo）' })
  const fileName = (file && file.name) || ''
  let format = String(body.get('format') || 'auto').toLowerCase()
  if (format !== 'xer' && format !== 'mspdi') {
    format = /\.xer$/i.test(fileName) ? 'xer' : 'mspdi'
  }
  const requestedPid = body.get('project_id')
  const pid = (requestedPid && String(requestedPid).trim()) || nextGeneratedProjectId()
  if (state.projects[pid] && !state.trashedIds.has(pid)) {
    throw httpError(409, { detail: `專案已存在（demo）：${pid}` })
  }
  const tasks = makeImportDemoTasks()
  const wbs = [
    { wbs_code: 'IM', name: '匯入示範', parent_code: null, sort_order: 0 },
    { wbs_code: 'IM.1', name: '主體工程', parent_code: 'IM', sort_order: 1 },
  ]
  const project = {
    project_id: pid,
    project_name: `匯入示範專案（${fileName || (format === 'xer' ? 'P6 XER' : 'MSPDI XML')}）`,
    region: REGION,
    tenant_id: TENANT_ID,
    start_date: '2026-08-03',
    work_days: '1111100',
    version: 0,
    tasks,
    wbs,
    day_dates: null,
    project_duration: 0,
  }
  const pstate = {
    project,
    wbs,
    holidays: [],
    baselines: [],
    baselineDetails: {},
    baseline: null,
    progress: [],
    evm: null,
    resources: null,
    risk: [],
    cost: null,
    health: null,
    level: null,
    simulate: null,
    photos: {},
    nextBaselineId: 1,
    nextPhotoId: 1,
  }
  state.projects[pid] = pstate
  state.trashedIds.delete(pid)
  recomputeProject(pstate)
  state.projectsList.push(projectSummary(pstate))
  const report = {
    format,
    tasks: tasks.length,
    wbs: wbs.length,
    links: tasks.reduce((n, t) => n + (Array.isArray(t.links) ? t.links.length : 0), 0),
    constraints: tasks.filter((t) => t.constraint_type).length,
    actuals: 0,
    warnings: [
      `示範模式：未實際解析「${fileName || '上傳檔案'}」的內容，以上為固定的示範匯入結果（重新整理即還原）。`,
    ],
  }
  return { data: { project: pstate.project, report } }
}

function handleGetProject(pid) {
  if (state.trashedIds.has(pid)) throw httpError(404, { detail: `專案不存在（demo）：${pid}` })
  return { data: getPstate(pid).project }
}

function handleDeleteProject(pid) {
  const pstate = getPstate(pid)
  state.trashedIds.add(pid)
  state.projectsList = state.projectsList.filter((s) => s.project_id !== pid)
  const summary = projectSummary(pstate)
  if (!state.trash.some((s) => s.project_id === pid)) state.trash.push(summary)
  return { data: { ok: true } }
}

function handleRestore(pid) {
  if (!state.trashedIds.has(pid)) throw httpError(404, { detail: '不在回收桶中（demo）' })
  const pstate = getPstate(pid)
  state.trashedIds.delete(pid)
  state.trash = state.trash.filter((s) => s.project_id !== pid)
  if (!state.projectsList.some((s) => s.project_id === pid)) {
    state.projectsList.push(projectSummary(pstate))
  }
  return { data: { ok: true } }
}

function handlePurge(pid) {
  state.trashedIds.delete(pid)
  state.trash = state.trash.filter((s) => s.project_id !== pid)
  delete state.projects[pid]
  return { data: { ok: true } }
}

// ---- 任務 CRUD（recompute 觸發即時 CPM 重算）----

function handleTaskDuration(pid, tid, body) {
  const pstate = getPstate(pid)
  checkVersion(pstate, body)
  const task = pstate.project.tasks.find((t) => t.task_id === tid)
  if (!task) throw httpError(404, { detail: `任務不存在（demo）：${tid}` })
  task.duration = Number(body && body.duration)
  return { data: recomputeProject(pstate) }
}

function handleAddTask(pid, body) {
  const pstate = getPstate(pid)
  checkVersion(pstate, body)
  const taskId = body && body.task_id
  if (!taskId) throw httpError(422, { detail: 'task_id 為必填（demo）' })
  if (pstate.project.tasks.some((t) => t.task_id === taskId)) {
    throw httpError(422, { detail: `重複的 task_id（demo）：${taskId}` })
  }
  const newTask = {
    task_id: taskId,
    task_name: (body && body.task_name) || '',
    duration: Number(body && body.duration) || 0,
    predecessors: Array.isArray(body && body.predecessors) ? body.predecessors : [],
    status: (body && body.status) || 'PENDING',
    links: (body && body.links) || null,
    wbs_code: (body && body.wbs_code) || null,
    constraint_type: (body && body.constraint_type) || null,
    constraint_day: body && body.constraint_day != null ? body.constraint_day : null,
    resource_demands: (body && body.resource_demands) || null,
  }
  pstate.project.tasks.push(newTask)
  return { data: recomputeProject(pstate) }
}

function handleUpdateTask(pid, tid, body) {
  const pstate = getPstate(pid)
  checkVersion(pstate, body)
  const idx = pstate.project.tasks.findIndex((t) => t.task_id === tid)
  if (idx < 0) throw httpError(404, { detail: `任務不存在（demo）：${tid}` })
  const patch = { ...(body || {}) }
  delete patch.expected_version
  pstate.project.tasks[idx] = { ...pstate.project.tasks[idx], ...patch }
  return { data: recomputeProject(pstate) }
}

function handleDeleteTask(pid, tid, body) {
  const pstate = getPstate(pid)
  checkVersion(pstate, body)
  const idx = pstate.project.tasks.findIndex((t) => t.task_id === tid)
  if (idx < 0) throw httpError(404, { detail: `任務不存在（demo）：${tid}` })
  pstate.project.tasks.splice(idx, 1)
  // 級聯：移除其餘任務中指向被刪任務的相依（predecessors / links），
  // 避免 cpmLite 因「未知的前置任務」而拋錯。
  for (const t of pstate.project.tasks) {
    if (Array.isArray(t.predecessors)) t.predecessors = t.predecessors.filter((p) => p !== tid)
    if (Array.isArray(t.links)) t.links = t.links.filter((l) => l.predecessor_task_id !== tid)
  }
  return { data: recomputeProject(pstate) }
}

// ---- 假日 / 回收桶以外的 upsert 類端點 ----

function handleSaveHolidays(pid, body) {
  const pstate = getPstate(pid)
  pstate.holidays = Array.isArray(body) ? body : []
  refreshDayDates(pstate)
  return { data: pstate.holidays }
}

function handleSaveResources(pid, body) {
  const pstate = getPstate(pid)
  pstate.resources = body || { limits: [], demands: {}, calendars: [] }
  return { data: pstate.resources }
}

function handleSaveRisk(pid, body) {
  const pstate = getPstate(pid)
  pstate.risk = Array.isArray(body) ? body : []
  return { data: pstate.risk }
}

function handleSaveProgress(pid, body) {
  const pstate = getPstate(pid)
  pstate.progress = Array.isArray(body) ? body : []
  return { data: pstate.progress }
}

function handleSaveWbs(pid, body) {
  const pstate = getPstate(pid)
  pstate.wbs = Array.isArray(body) ? body : []
  pstate.project.wbs = pstate.wbs
  return { data: pstate.wbs }
}

// ---- 基準線 ----

function handleCreateBaseline(pid, body) {
  const pstate = getPstate(pid)
  const name = (body && body.name) || 'baseline'
  const id = pstate.nextBaselineId
  pstate.nextBaselineId += 1
  const budgetByTask = {}
  for (const pr of pstate.progress || []) budgetByTask[pr.task_id] = pr.budget
  const tasksSnap = pstate.project.tasks.map((t) => ({
    task_id: t.task_id,
    es: t.es,
    ef: t.ef,
    duration: t.duration,
    budget: budgetByTask[t.task_id] != null ? budgetByTask[t.task_id] : 0,
  }))
  const now = new Date().toISOString()
  const full = {
    id,
    name,
    project_duration: pstate.project.project_duration,
    created_at: now,
    is_active: true,
    tasks: tasksSnap,
  }
  for (const k of Object.keys(pstate.baselineDetails)) pstate.baselineDetails[k].is_active = false
  pstate.baselineDetails[id] = full
  pstate.baselines = (pstate.baselines || []).map((b) => ({ ...b, is_active: false }))
  pstate.baselines.push({ id, name, created_at: now, is_active: true, project_duration: full.project_duration })
  pstate.baseline = full
  return { data: full }
}

function handleGetBaseline(pid) {
  const pstate = getPstate(pid)
  if (!pstate.baseline) throw httpError(404, { detail: '尚無基準線（demo）' })
  return { data: pstate.baseline }
}

function handleGetBaselineById(pid, id) {
  const pstate = getPstate(pid)
  const b = pstate.baselineDetails[id]
  if (!b) throw httpError(404, { detail: `基準線不存在（demo）：${id}` })
  return { data: b }
}

function handleActivateBaseline(pid, id) {
  const pstate = getPstate(pid)
  const details = pstate.baselineDetails
  const b = details[id]
  if (!b) throw httpError(404, { detail: `基準線不存在（demo）：${id}` })
  for (const k of Object.keys(details)) details[k].is_active = String(k) === String(id)
  pstate.baselines = (pstate.baselines || []).map((x) => ({ ...x, is_active: String(x.id) === String(id) }))
  pstate.baseline = b
  return { data: b }
}

function handleDeleteBaseline(pid, id) {
  const pstate = getPstate(pid)
  const details = pstate.baselineDetails
  if (!details[id]) throw httpError(404, { detail: `基準線不存在（demo）：${id}` })
  const wasActive = Boolean(details[id].is_active)
  delete details[id]
  pstate.baselines = (pstate.baselines || []).filter((x) => String(x.id) !== String(id))
  if (wasActive) {
    const remainingIds = Object.keys(details)
    if (remainingIds.length > 0) {
      const newestId = remainingIds.reduce((a, b) => (Number(b) > Number(a) ? b : a))
      details[newestId].is_active = true
      pstate.baselines = pstate.baselines.map((x) => ({ ...x, is_active: String(x.id) === String(newestId) }))
      pstate.baseline = details[newestId]
    } else {
      pstate.baseline = null
    }
  }
  return { data: { ok: true } }
}

// ---- EVM ----

function handleEvmAlert(pid) {
  const pstate = getPstate(pid)
  const flagged = Boolean(pstate.evm && pstate.evm.risk_flagged)
  return { data: { dispatched: flagged, message: flagged ? '已排入示範同步事件（demo）' : '未達風險門檻，未拋轉' } }
}

// ---- 照片（任務附件；multipart，data URI 就地保存，無真實上傳）----

function blobToDataUri(blob) {
  return new Promise((resolve, reject) => {
    try {
      const reader = new FileReader()
      reader.onload = () => resolve(reader.result)
      reader.onerror = () => reject(reader.error || new Error('FileReader 讀取失敗'))
      reader.readAsDataURL(blob)
    } catch (e) {
      reject(e)
    }
  })
}

async function handleUploadPhoto(pid, tid, formData, config) {
  const pstate = getPstate(pid)
  if (!formData || typeof formData.get !== 'function') {
    throw httpError(422, { detail: '需要 multipart/form-data（demo）' })
  }
  const file = formData.get('file')
  const note = formData.get('note') || null
  if (!file) throw httpError(422, { detail: 'file 為必填（demo）' })
  const dataUri = await blobToDataUri(file)
  const auth = parseToken(getAuthHeader(config))
  const photo = {
    id: pstate.nextPhotoId,
    task_id: tid,
    original_name: file.name || 'photo.jpg',
    content_type: file.type || 'application/octet-stream',
    size_bytes: file.size || 0,
    note,
    uploaded_by: auth ? auth.username : 'demo',
    created_at: new Date().toISOString(),
    url: dataUri,
  }
  pstate.nextPhotoId += 1
  if (!pstate.photos[tid]) pstate.photos[tid] = []
  pstate.photos[tid].push(photo)
  return { data: photo }
}

function handleListPhotos(pid, tid) {
  const pstate = getPstate(pid)
  return { data: pstate.photos[tid] || [] }
}

function handleDeletePhoto(photoId) {
  for (const pid of Object.keys(state.projects)) {
    const pstate = state.projects[pid]
    for (const tid of Object.keys(pstate.photos)) {
      const idx = pstate.photos[tid].findIndex((p) => String(p.id) === String(photoId))
      if (idx >= 0) {
        pstate.photos[tid].splice(idx, 1)
        return { data: { ok: true } }
      }
    }
  }
  throw httpError(404, { detail: `照片不存在（demo）：${photoId}` })
}

// ---- 路由表（regex path params）----

const RE_TASK_DURATION = /^\/projects\/([^/]+)\/tasks\/([^/]+)\/duration$/
const RE_TASK_PHOTOS = /^\/projects\/([^/]+)\/tasks\/([^/]+)\/photos$/
const RE_TASK = /^\/projects\/([^/]+)\/tasks\/([^/]+)$/
const RE_PROJECT_TASKS = /^\/projects\/([^/]+)\/tasks$/
const RE_PHOTO = /^\/photos\/([^/]+)$/
const RE_HOLIDAYS = /^\/projects\/([^/]+)\/holidays$/
const RE_RESTORE = /^\/projects\/([^/]+)\/restore$/
const RE_PURGE = /^\/projects\/([^/]+)\/purge$/
const RE_ERP_SYNC = /^\/projects\/([^/]+)\/erp\/sync$/
const RE_RESOURCES = /^\/projects\/([^/]+)\/resources$/
const RE_LEVEL = /^\/projects\/([^/]+)\/level$/
const RE_RISK = /^\/projects\/([^/]+)\/risk$/
const RE_SIMULATE = /^\/projects\/([^/]+)\/simulate$/
const RE_PROGRESS = /^\/projects\/([^/]+)\/progress$/
const RE_EVM_ALERT = /^\/projects\/([^/]+)\/evm\/alert$/
const RE_EVM = /^\/projects\/([^/]+)\/evm$/
const RE_BASELINE_ACTIVATE = /^\/projects\/([^/]+)\/baselines\/([^/]+)\/activate$/
const RE_BASELINE_ID = /^\/projects\/([^/]+)\/baselines\/([^/]+)$/
const RE_BASELINES = /^\/projects\/([^/]+)\/baselines$/
const RE_BASELINE = /^\/projects\/([^/]+)\/baseline$/
const RE_WBS = /^\/projects\/([^/]+)\/wbs$/
const RE_COST = /^\/projects\/([^/]+)\/cost$/
const RE_HEALTH = /^\/projects\/([^/]+)\/health$/
const RE_USER_ID = /^\/users\/([^/]+)$/
const RE_PROJECT = /^\/projects\/([^/]+)$/

async function route(method, url, body, config) {
  if (method === 'post' && url === '/auth/login') return handleLogin(body)
  if (method === 'get' && url === '/auth/me') return handleMe(config)

  if (method === 'get' && url === '/dashboard') return { data: state.dashboard }
  if (method === 'get' && url === '/users') return { data: state.users }
  if (method === 'post' && url === '/users') return handleCreateUser(body)

  if (method === 'get' && url === '/resources/pool') return { data: state.pool }
  if (method === 'put' && url === '/resources/pool') return handleSavePool(body)
  if (method === 'get' && url === '/resources/allocation') return { data: state.allocation }
  if (method === 'get' && url === '/projects/trash') return { data: state.trash }

  if (method === 'get' && url === '/projects') return { data: state.projectsList }
  if (method === 'post' && url === '/projects') return handleCreateProject(body)
  if (method === 'post' && url === '/projects/import') return handleImportProject(body)

  let m

  if ((m = RE_USER_ID.exec(url))) {
    if (method === 'put') return handleUpdateUser(m[1], body)
    if (method === 'delete') return handleDeleteUser(m[1])
  }

  if ((m = RE_TASK_DURATION.exec(url)) && method === 'put') return handleTaskDuration(m[1], m[2], body)

  if ((m = RE_TASK_PHOTOS.exec(url))) {
    if (method === 'get') return handleListPhotos(m[1], m[2])
    if (method === 'post') return handleUploadPhoto(m[1], m[2], body, config)
  }

  if ((m = RE_PROJECT_TASKS.exec(url)) && method === 'post') return handleAddTask(m[1], body)

  if ((m = RE_TASK.exec(url))) {
    if (method === 'put') return handleUpdateTask(m[1], m[2], body)
    if (method === 'delete') return handleDeleteTask(m[1], m[2], body)
  }

  if ((m = RE_PHOTO.exec(url)) && method === 'delete') return handleDeletePhoto(m[1])

  if ((m = RE_HOLIDAYS.exec(url))) {
    if (method === 'get') return { data: getPstate(m[1]).holidays }
    if (method === 'put') return handleSaveHolidays(m[1], body)
  }

  if ((m = RE_RESTORE.exec(url)) && method === 'post') return handleRestore(m[1])
  if ((m = RE_PURGE.exec(url)) && method === 'delete') return handlePurge(m[1])
  if ((m = RE_ERP_SYNC.exec(url)) && method === 'post') {
    return { data: { ok: true, sync_type: (body && body.sync_type) || 'SCHEDULE_PUSH', queued: true } }
  }

  if ((m = RE_RESOURCES.exec(url))) {
    if (method === 'get') return { data: getPstate(m[1]).resources }
    if (method === 'put') return handleSaveResources(m[1], body)
  }

  if ((m = RE_LEVEL.exec(url)) && method === 'post') return { data: getPstate(m[1]).level }

  if ((m = RE_RISK.exec(url))) {
    if (method === 'get') return { data: getPstate(m[1]).risk }
    if (method === 'put') return handleSaveRisk(m[1], body)
  }

  if ((m = RE_SIMULATE.exec(url)) && method === 'post') return { data: getPstate(m[1]).simulate }

  if ((m = RE_PROGRESS.exec(url))) {
    if (method === 'get') return { data: getPstate(m[1]).progress }
    if (method === 'put') return handleSaveProgress(m[1], body)
  }

  if ((m = RE_EVM_ALERT.exec(url)) && method === 'post') return handleEvmAlert(m[1])
  if ((m = RE_EVM.exec(url)) && method === 'get') return { data: getPstate(m[1]).evm }

  if ((m = RE_BASELINE_ACTIVATE.exec(url)) && method === 'post') return handleActivateBaseline(m[1], m[2])
  if ((m = RE_BASELINE_ID.exec(url))) {
    if (method === 'get') return handleGetBaselineById(m[1], m[2])
    if (method === 'delete') return handleDeleteBaseline(m[1], m[2])
  }
  if ((m = RE_BASELINES.exec(url)) && method === 'get') return { data: getPstate(m[1]).baselines }
  if ((m = RE_BASELINE.exec(url))) {
    if (method === 'get') return handleGetBaseline(m[1])
    if (method === 'post') return handleCreateBaseline(m[1], body)
  }

  if ((m = RE_WBS.exec(url))) {
    if (method === 'get') return { data: getPstate(m[1]).wbs }
    if (method === 'put') return handleSaveWbs(m[1], body)
  }

  if ((m = RE_COST.exec(url)) && method === 'get') return { data: getPstate(m[1]).cost }
  if ((m = RE_HEALTH.exec(url)) && method === 'get') return { data: getPstate(m[1]).health }

  if ((m = RE_PROJECT.exec(url))) {
    if (method === 'get') return handleGetProject(m[1])
    if (method === 'delete') return handleDeleteProject(m[1])
  }

  if (!warnedPaths.has(`${method} ${url}`)) {
    warnedPaths.add(`${method} ${url}`)
    // eslint-disable-next-line no-console
    console.warn(`[demo mockApi] 未實作的路由（unhandled route）：${method.toUpperCase()} ${url}`)
  }
  throw httpError(404, { detail: `Demo 模式尚未實作此端點：${method.toUpperCase()} ${url}` })
}

// ---- axios 自訂 adapter ----

async function adapter(config) {
  const method = (config.method || 'get').toLowerCase()
  let url = config.url || ''
  if (config.baseURL && url.startsWith(config.baseURL)) {
    url = url.slice(config.baseURL.length)
  }
  url = url.split('?')[0]
  if (!url.startsWith('/')) url = `/${url}`

  let body = config.data
  if (typeof body === 'string' && body.length > 0) {
    try {
      body = JSON.parse(body)
    } catch (e) {
      // 非 JSON 字串（理論上不會發生，axios transformRequest 只對純物件序列化）：原樣保留
    }
  }

  try {
    const result = await route(method, url, body, config)
    return makeResponse((result && result.status) || 200, result ? result.data : undefined, config)
  } catch (err) {
    if (err && err.__mockHttp) {
      throw makeAxiosError(err.status, err.data, config)
    }
    // eslint-disable-next-line no-console
    console.error('[demo mockApi] 未預期的錯誤（unexpected error）', method, url, err)
    throw makeAxiosError(500, { detail: String((err && err.message) || err) }, config)
  }
}

// ---- 示範模式提示橫幅 ----

function injectBanner() {
  if (typeof document === 'undefined' || !document.body) return
  if (document.getElementById('cpm-demo-banner')) return

  const bar = document.createElement('div')
  bar.id = 'cpm-demo-banner'
  bar.setAttribute('role', 'status')
  bar.title =
    '「資源撫平」「蒙地卡羅模擬」「投資組合資源分配」「DCMA 健康度」「成本負荷」等分析頁籤顯示的是擷取當下的示範快照，不會隨您的編輯即時重算；任務照片以瀏覽器記憶體（data URI）暫存，不會真的上傳；資源池可編輯，但投資組合資源分配維持快照。'
  bar.style.cssText = [
    'position:fixed',
    'top:0',
    'left:0',
    'right:0',
    'z-index:99999',
    'display:flex',
    'align-items:center',
    'justify-content:center',
    'gap:8px',
    'background:#8e44ad',
    'color:#fff',
    'font-size:13px',
    'line-height:1.5',
    'padding:6px 40px',
    'text-align:center',
    'box-shadow:0 1px 4px rgba(0,0,0,.25)',
    'font-family:system-ui,-apple-system,"Segoe UI",sans-serif',
  ].join(';')

  const text = document.createElement('span')
  text.textContent = '🧪 體驗版 Demo — 所有資料僅存在瀏覽器中，重新整理即還原；分析結果為示範快照。'
  bar.appendChild(text)

  const closeBtn = document.createElement('button')
  closeBtn.type = 'button'
  closeBtn.textContent = '×'
  closeBtn.setAttribute('aria-label', '關閉提示')
  closeBtn.style.cssText =
    'position:absolute;right:8px;top:2px;background:transparent;border:none;color:#fff;font-size:18px;cursor:pointer;line-height:1;padding:2px 6px;'
  closeBtn.onclick = () => {
    bar.remove()
    document.body.style.marginTop = ''
  }
  bar.appendChild(closeBtn)

  document.body.appendChild(bar)
  document.body.style.marginTop = '34px'
}

// ---- 公開 API ----

// 安裝 demo 模擬後端：（重）建立記憶體 state、將自訂 adapter 掛上 apiClient、
// 注入提示橫幅。main.jsx 於 VITE_DEMO_STANDALONE==='1' 時，於渲染 React 前呼叫。
export function install() {
  resetState()
  apiClient.defaults.adapter = adapter
  injectBanner()
  return state
}

// 測試專用：讀取目前記憶體 state（不供正式程式碼使用）。
export function _getStateForTests() {
  return state
}
