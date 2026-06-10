// 前端 i18n 字典：keys 必須與後端 core/i18n.py 完全一致。
// 支援雙區域：TW（台灣，繁中）與 CN（大陸，簡中）。

export const I18N = {
  TW: {
    appTitle: 'CPM 工程排程與自動化平台',
    project: '專案',
    projectName: '專案名稱',
    task: '任務',
    taskId: '任務編號',
    taskName: '任務名稱',
    critical: '要徑',
    criticalPath: '要徑',
    floatTime: '寬裕時間',
    duration: '工期',
    status: '狀態',
    day: '天',
    days: '天',
    region: '區域',
    tenant: '租戶',
    recalc: '重新計算',
    addTask: '新增任務',
    updateDuration: '更新工期',
    syncErp: '拋轉ERP',
    downloadReport: '下載報表',
    reportTitle: '工期報表',
    projectDuration: '專案總工期',
    loading: '載入中…',
    error: '錯誤',
    // ---- 新增 UI 鍵（建立專案表單 / 刪除任務 / 甘特圖拖曳） ----
    newProject: '新增專案',
    save: '儲存',
    cancel: '取消',
    confirm: '確定',
    delete: '刪除',
    deleteTask: '刪除任務',
    confirmDeleteTask: '確定要刪除此任務嗎？',
    predecessors: '前置任務',
    addTaskRow: '新增任務列',
    removeRow: '移除',
    dragHint: '拖曳右側邊緣可調整工期',
    required: '必填',
    nameRequired: '請輸入專案名稱',
    atLeastOneTask: '至少需要一個任務',
    duplicateTaskId: '任務編號不可重複',
    invalidDuration: '工期須為大於或等於 0 的整數',
    taskIdRequired: '每個任務皆須填寫任務編號',
    none: '無',
    statuses: {
      PENDING: '待辦',
      IN_PROGRESS: '進行中',
      COMPLETED: '已完成',
      DELAYED: '延遲',
    },
  },
  CN: {
    appTitle: 'CPM 工程进度与自动化平台',
    project: '项目',
    projectName: '项目名称',
    task: '任务',
    taskId: '任务编号',
    taskName: '任务名称',
    critical: '关键路径',
    criticalPath: '关键路径',
    floatTime: '总时差',
    duration: '工期',
    status: '状态',
    day: '天',
    days: '天',
    region: '区域',
    tenant: '租户',
    recalc: '重新计算',
    addTask: '新增任务',
    updateDuration: '更新工期',
    syncErp: '拋轉ERP',
    downloadReport: '下载报表',
    reportTitle: '工期报表',
    projectDuration: '项目总工期',
    loading: '加载中…',
    error: '错误',
    // ---- 新增 UI 键（新建项目表单 / 删除任务 / 甘特图拖拽） ----
    newProject: '新建项目',
    save: '保存',
    cancel: '取消',
    confirm: '确定',
    delete: '删除',
    deleteTask: '删除任务',
    confirmDeleteTask: '确定要删除此任务吗？',
    predecessors: '前置任务',
    addTaskRow: '新增任务行',
    removeRow: '移除',
    dragHint: '拖拽右侧边缘可调整工期',
    required: '必填',
    nameRequired: '请输入项目名称',
    atLeastOneTask: '至少需要一个任务',
    duplicateTaskId: '任务编号不可重复',
    invalidDuration: '工期须为大于或等于 0 的整数',
    taskIdRequired: '每个任务都须填写任务编号',
    none: '无',
    statuses: {
      PENDING: '待办',
      IN_PROGRESS: '进行中',
      COMPLETED: '已完成',
      DELAYED: '延期',
    },
  },
}

// t(region, key) -> 翻譯字串
// 回退順序：指定區域 -> TW -> 回傳 key 本身。
// 支援 'statuses.PENDING' 形式的巢狀鍵。
export function t(region, key) {
  const dict = I18N[region] || I18N.TW
  const fallback = I18N.TW

  if (key && key.includes('.')) {
    const [group, sub] = key.split('.')
    const fromDict = dict[group] && dict[group][sub]
    if (fromDict != null) return fromDict
    const fromFallback = fallback[group] && fallback[group][sub]
    if (fromFallback != null) return fromFallback
    return sub
  }

  if (dict[key] != null) return dict[key]
  if (fallback[key] != null) return fallback[key]
  return key
}

// 翻譯狀態值的便捷函式
export function tStatus(region, status) {
  return t(region, `statuses.${status}`)
}

export default { I18N, t, tStatus }
