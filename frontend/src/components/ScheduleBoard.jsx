import React, { useEffect, useMemo, useState } from 'react';
import { useScheduleStore } from '../store/scheduleStore';
import { t } from '../i18n';
import { reportUrl, exportXlsxUrl, exportPdfUrl } from '../api/client';
import GanttChart from './GanttChart';
import ProjectForm from './ProjectForm';
import ResourcePanel from './ResourcePanel';
import RiskPanel from './RiskPanel';
import ProgressPanel from './ProgressPanel';

/**
 * ScheduleBoard 工期排程主控板
 *
 * 功能：
 *   - 租戶 (tenant) / 區域 (region) 切換器 -> 驅動 store 並重新以 i18n 標籤
 *   - 專案下拉選單 (mount 時 loadProjects；選取時 loadProject)
 *   - 任務表格：task_id / task_name / status / 可編輯工期(duration) / predecessors
 *     每列「更新工期」按鈕 -> store.changeTaskDuration(taskId, value)
 *       -> 後端 PUT .../duration 重算 CPM -> 回傳 ProjectOut -> Gantt 重繪
 *   - 按鈕：重新計算 / 新增任務 / 拋轉 ERP / 下載報表
 *   - 顯示 project_duration 與 要徑(critical path) 摘要
 *   - loading / error 狀態
 */

const REGIONS = ['TW', 'CN'];
const STATUS_VALUES = ['PENDING', 'IN_PROGRESS', 'COMPLETED', 'DELAYED'];
// Batch 3：依賴類型（FS 完成-開始 / SS 開始-開始 / FF 完成-完成 / SF 開始-完成）
const DEP_TYPES = ['FS', 'SS', 'FF', 'SF'];

// Batch 3：依賴連結顯示標籤 — 'A'（FS+0）或 'A(SS+2)' / 'A(FS-1)'
function linkLabel(l) {
  const dep = String(l.dep_type || 'FS').toUpperCase();
  const lag = Number(l.lag_days) || 0;
  if (dep === 'FS' && lag === 0) return l.predecessor_task_id;
  return `${l.predecessor_task_id}(${dep}${lag ? (lag > 0 ? `+${lag}` : `${lag}`) : ''})`;
}

// 狀態翻譯輔助：相容 t() 是否支援 'statuses.X' 點路徑。
// 後端/前端 i18n 約定 statuses 為子表 (sub-map)。先試點路徑，
// 若 t() 原樣回傳 key 視為未支援，退而求其次直接回傳狀態碼。
function statusLabel(region, status) {
  if (!status) return '';
  const dotKey = `statuses.${status}`;
  const val = t(region, dotKey);
  if (val && val !== dotKey && val !== status) return val;
  // 退回：嘗試直接以狀態碼當 key (部分 i18n 實作可能扁平化)
  const flat = t(region, status);
  if (flat && flat !== status) return flat;
  return status;
}

export default function ScheduleBoard() {
  const {
    tenantId,
    region,
    role,
    token,
    projects,
    currentProject,
    loading,
    error,
    leveling,
    baseline,
    progress,
    setTenant,
    setRegion,
    loadProjects,
    loadProject,
    changeTaskDuration,
    addTask,
    removeTask,
    createProject,
    syncErp,
    loadProgress,
    loadBaseline,
    updateTaskLinks,
  } = useScheduleStore();

  // 寫入權限：editor 以上可寫；viewer 僅讀（隱藏所有寫入動作）。
  // 角色缺失（舊權杖/標頭模式）視為 admin（與後端預設一致），維持向後相容。
  const canWrite = (role || 'admin') !== 'viewer';

  // 本地草稿：每列工期輸入框的暫存值 (key = task_id)
  const [durationDrafts, setDurationDrafts] = useState({});
  // 租戶輸入框暫存值
  const [tenantInput, setTenantInput] = useState(tenantId || '');
  // 新增任務表單暫存值
  const [newTask, setNewTask] = useState({
    task_id: '',
    task_name: '',
    duration: 1,
    predecessors: '',
    status: 'PENDING',
  });
  // 建立專案模態開關
  const [showProjectForm, setShowProjectForm] = useState(false);
  // Phase 8 分頁：'schedule'（排程/甘特圖）| 'resources'（資源撫平）| 'risk'（風險分析）
  const [activeTab, setActiveTab] = useState('schedule');
  // Batch 3：依賴編輯彈窗 — { taskId, draft: { [otherTaskId]: {checked, dep_type, lag_days} } } | null
  const [depEdit, setDepEdit] = useState(null);

  // 掛載時載入專案清單
  useEffect(() => {
    loadProjects();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 租戶/區域變更後重新載入專案清單
  useEffect(() => {
    setTenantInput(tenantId || '');
  }, [tenantId]);

  // currentProject 變更時，重置工期草稿為後端回傳值
  useEffect(() => {
    if (currentProject && Array.isArray(currentProject.tasks)) {
      const drafts = {};
      currentProject.tasks.forEach((tk) => {
        drafts[tk.task_id] = tk.duration;
      });
      setDurationDrafts(drafts);
    }
  }, [currentProject]);

  // Phase 9：切換專案後預載進度 + 最新基準線，使甘特圖能在「排程」分頁即顯示
  // 計畫 vs 實際疊圖與完成度填色（兩者皆為 best-effort；失敗時靜默，不影響排程）。
  useEffect(() => {
    if (currentProject?.project_id) {
      loadProgress().catch(() => {});
      loadBaseline().catch(() => {});
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [currentProject?.project_id]);

  const tasks = currentProject?.tasks || [];

  // 要徑摘要：依 es 排序後串接要徑 task_id
  const criticalPathStr = useMemo(() => {
    const crit = tasks
      .filter((tk) => tk.is_critical || tk.float_time === 0)
      .sort((a, b) => (Number(a.es) || 0) - (Number(b.es) || 0))
      .map((tk) => tk.task_id);
    return crit.join(' → ');
  }, [tasks]);

  // 資源撫平超載日（傳入甘特圖繪製紅色警示帶）；無撫平結果時為 undefined（甘特圖不繪製）
  const overCapacityDays = useMemo(() => {
    if (leveling && Array.isArray(leveling.over_capacity_days)) {
      return leveling.over_capacity_days;
    }
    return undefined;
  }, [leveling]);

  // Phase 9：完成度查詢表 task_id -> percent_complete（傳入甘特圖填色）；無進度時為 undefined
  const progressMap = useMemo(() => {
    if (Array.isArray(progress) && progress.length > 0) {
      const map = {};
      progress.forEach((p) => {
        if (p && p.task_id != null) map[p.task_id] = p.percent_complete;
      });
      return Object.keys(map).length > 0 ? map : undefined;
    }
    return undefined;
  }, [progress]);

  // ---- 事件處理 ----

  const handleRegionChange = (e) => {
    setRegion(e.target.value);
  };

  const handleTenantApply = () => {
    const v = (tenantInput || '').trim();
    if (v && v !== tenantId) {
      setTenant(v);
    }
    // 切換租戶後重新載入專案清單
    loadProjects();
  };

  const handleProjectSelect = (e) => {
    const pid = e.target.value;
    if (pid) {
      loadProject(pid);
    }
  };

  const handleDraftChange = (taskId, value) => {
    setDurationDrafts((prev) => ({ ...prev, [taskId]: value }));
  };

  const handleUpdateDuration = async (taskId) => {
    const raw = durationDrafts[taskId];
    const dur = Number.parseInt(raw, 10);
    if (Number.isNaN(dur) || dur < 0) return;
    await changeTaskDuration(taskId, dur);
  };

  const handleRecalc = () => {
    // 重新計算：重新載入目前專案 (後端載入時若缺結果會重算)
    if (currentProject?.project_id) {
      loadProject(currentProject.project_id);
    }
  };

  const handleAddTask = async () => {
    if (!newTask.task_id.trim()) return;
    const payload = {
      task_id: newTask.task_id.trim(),
      task_name: newTask.task_name.trim(),
      duration: Number.parseInt(newTask.duration, 10) || 0,
      predecessors: newTask.predecessors
        .split(',')
        .map((s) => s.trim())
        .filter(Boolean),
      status: newTask.status || 'PENDING',
    };
    await addTask(payload);
    // 清空表單
    setNewTask({ task_id: '', task_name: '', duration: 1, predecessors: '', status: 'PENDING' });
  };

  const handleSyncErp = () => {
    syncErp();
  };

  // 建立專案：呼叫 store.createProject(payload)，成功後關閉模態
  // （store 會將新專案設為 currentProject 並刷新清單）。失敗時拋出讓
  // ProjectForm 保持開啟，錯誤透過 store.error 顯示。
  const handleCreateProject = async (payload) => {
    await createProject(payload);
    setShowProjectForm(false);
  };

  // 刪除任務：確認後呼叫 store.removeTask(taskId)。
  const handleRemoveTask = async (taskId) => {
    // eslint-disable-next-line no-alert
    if (!window.confirm(t(region, 'confirmDeleteTask'))) return;
    await removeTask(taskId);
  };

  // ---- Batch 3：依賴編輯（dep_type FS/SS/FF/SF + lag）----

  // 開啟依賴編輯彈窗：以目標任務現有 links（無則由 predecessors 衍生 FS+0）
  // 初始化「其他每個任務」一列草稿 {checked, dep_type, lag_days}。
  const openDepEditor = (task) => {
    const links =
      Array.isArray(task.links) && task.links.length > 0
        ? task.links
        : (task.predecessors || []).map((p) => ({
            predecessor_task_id: p,
            dep_type: 'FS',
            lag_days: 0,
          }));
    const byPred = {};
    links.forEach((l) => {
      if (l && l.predecessor_task_id != null) byPred[l.predecessor_task_id] = l;
    });
    const draft = {};
    tasks.forEach((other) => {
      if (other.task_id === task.task_id) return;
      const l = byPred[other.task_id];
      draft[other.task_id] = {
        checked: Boolean(l),
        dep_type: l ? String(l.dep_type || 'FS').toUpperCase() : 'FS',
        lag_days: l ? (l.lag_days ?? 0) : 0,
      };
    });
    setDepEdit({ taskId: task.task_id, draft });
  };

  // 更新依賴草稿某一列
  const setDepDraft = (otherId, patch) => {
    setDepEdit((prev) =>
      prev
        ? {
            ...prev,
            draft: { ...prev.draft, [otherId]: { ...prev.draft[otherId], ...patch } },
          }
        : prev,
    );
  };

  // 儲存依賴：勾選列 -> links，呼叫 store.updateTaskLinks（帶 expected_version 樂觀鎖；
  // 409 由 store 重載專案並以 conflictReloaded 提示）。
  const handleSaveDeps = async () => {
    if (!depEdit) return;
    const links = Object.entries(depEdit.draft)
      .filter(([, v]) => v && v.checked)
      .map(([pid, v]) => ({
        predecessor_task_id: pid,
        dep_type: v.dep_type || 'FS',
        lag_days: Number.parseInt(v.lag_days, 10) || 0,
      }));
    const { taskId } = depEdit;
    setDepEdit(null);
    await updateTaskLinks(taskId, links);
  };

  const handleDownloadReport = () => {
    if (currentProject?.project_id) {
      // reportUrl 回傳報表端點 URL；於新分頁開啟 (PDF 串流)
      window.open(reportUrl(currentProject.project_id), '_blank');
    }
  };

  // Phase 10：以 Authorization 標頭驗證的檔案下載（GET -> blob -> 觸發瀏覽器下載）。
  // window.open 無法帶 Bearer，故改以 fetch 取得 blob 後建立暫時連結下載。
  const downloadWithAuth = async (url, fallbackName) => {
    try {
      const headers = {
        'X-Tenant-Id': tenantId,
        'X-Region': region,
      };
      if (token) headers.Authorization = `Bearer ${token}`;
      const res = await fetch(url, { headers });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const blob = await res.blob();
      // 嘗試由 Content-Disposition 取得檔名，否則使用回退名稱
      let filename = fallbackName;
      const cd = res.headers.get('Content-Disposition');
      if (cd) {
        const m = /filename\*?=(?:UTF-8'')?["']?([^"';]+)/i.exec(cd);
        if (m && m[1]) filename = decodeURIComponent(m[1]);
      }
      const objUrl = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = objUrl;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(objUrl);
    } catch (e) {
      // eslint-disable-next-line no-alert
      window.alert(`${t(region, 'error')}: ${e && e.message ? e.message : e}`);
    }
  };

  const handleExportExcel = () => {
    if (currentProject?.project_id) {
      downloadWithAuth(
        exportXlsxUrl(currentProject.project_id),
        `${currentProject.project_id}.xlsx`,
      );
    }
  };

  const handleExportPdf = () => {
    if (currentProject?.project_id) {
      downloadWithAuth(
        exportPdfUrl(currentProject.project_id),
        `${currentProject.project_id}.pdf`,
      );
    }
  };

  // ---- 樣式 ----
  const btnStyle = {
    padding: '6px 14px',
    border: 'none',
    borderRadius: '4px',
    cursor: 'pointer',
    fontSize: '13px',
    color: '#fff',
    background: '#2c3e50',
  };

  const inputStyle = {
    padding: '4px 6px',
    border: '1px solid #ccc',
    borderRadius: '4px',
    fontSize: '13px',
  };

  return (
    <div style={{ maxWidth: '1200px', margin: '0 auto', padding: '16px', fontFamily: 'sans-serif' }}>
      {/* ===== 標題 ===== */}
      <h1 style={{ fontSize: '22px', color: '#2c3e50', marginBottom: '12px' }}>
        {t(region, 'appTitle')}
      </h1>

      {/* ===== 切換器列：租戶 / 區域 / 專案 ===== */}
      <div
        style={{
          display: 'flex',
          flexWrap: 'wrap',
          gap: '16px',
          alignItems: 'flex-end',
          padding: '12px',
          background: '#f7f9fc',
          border: '1px solid #e0e0e0',
          borderRadius: '6px',
          marginBottom: '16px',
        }}
      >
        {/* 租戶切換 */}
        <div style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
          <label style={{ fontSize: '12px', color: '#666' }}>{t(region, 'tenant')}</label>
          <div style={{ display: 'flex', gap: '6px' }}>
            <input
              style={{ ...inputStyle, width: '140px' }}
              value={tenantInput}
              onChange={(e) => setTenantInput(e.target.value)}
              placeholder="TENT-9981"
            />
            <button style={{ ...btnStyle, background: '#16a085' }} onClick={handleTenantApply}>
              OK
            </button>
          </div>
        </div>

        {/* 區域切換 */}
        <div style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
          <label style={{ fontSize: '12px', color: '#666' }}>{t(region, 'region')}</label>
          <select style={{ ...inputStyle, width: '100px' }} value={region} onChange={handleRegionChange}>
            {REGIONS.map((r) => (
              <option key={r} value={r}>
                {r}
              </option>
            ))}
          </select>
        </div>

        {/* 專案下拉 */}
        <div style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
          <label style={{ fontSize: '12px', color: '#666' }}>{t(region, 'project')}</label>
          <select
            style={{ ...inputStyle, width: '260px' }}
            value={currentProject?.project_id || ''}
            onChange={handleProjectSelect}
          >
            <option value="">— {t(region, 'project')} —</option>
            {projects.map((p) => (
              <option key={p.project_id} value={p.project_id}>
                {p.project_id} · {p.project_name}
              </option>
            ))}
          </select>
        </div>

        {/* 新增專案（viewer 隱藏） */}
        {canWrite && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
            <label style={{ fontSize: '12px', color: '#666' }}>&nbsp;</label>
            <button
              style={{ ...btnStyle, background: '#27ae60' }}
              onClick={() => setShowProjectForm(true)}
            >
              + {t(region, 'newProject')}
            </button>
          </div>
        )}
      </div>

      {/* ===== 建立專案模態 ===== */}
      {showProjectForm && (
        <ProjectForm
          region={region}
          defaultRegion={region}
          onSubmit={handleCreateProject}
          onCancel={() => setShowProjectForm(false)}
          serverError={error}
          submitting={loading}
        />
      )}

      {/* ===== Batch 3：依賴編輯彈窗（dep_type + lag） ===== */}
      {depEdit && (
        <div
          className="cpm-modal-overlay"
          role="dialog"
          aria-modal="true"
          onMouseDown={() => setDepEdit(null)}
        >
          <div className="cpm-modal dep-popover" onMouseDown={(e) => e.stopPropagation()}>
            <div className="cpm-modal-header">
              <h2 className="cpm-modal-title">
                {t(region, 'editDependencies')} — {depEdit.taskId}
              </h2>
              <button
                type="button"
                className="cpm-modal-close"
                aria-label={t(region, 'cancel')}
                onClick={() => setDepEdit(null)}
              >
                ×
              </button>
            </div>
            <div className="cpm-modal-body">
              {/* 標頭：前置任務 / 依賴類型 / 延滯天數 */}
              <div className="dep-popover-head">
                <span style={{ flex: '1 1 auto' }}>{t(region, 'predecessors')}</span>
                <span style={{ width: '70px' }}>{t(region, 'dependencyType')}</span>
                <span style={{ width: '70px' }}>{t(region, 'lagDays')}</span>
              </div>
              {tasks
                .filter((o) => o.task_id !== depEdit.taskId)
                .map((o) => {
                  const row =
                    depEdit.draft[o.task_id] || { checked: false, dep_type: 'FS', lag_days: 0 };
                  return (
                    <div key={o.task_id} className="dep-popover-row">
                      <label>
                        <input
                          type="checkbox"
                          checked={row.checked}
                          onChange={(e) => setDepDraft(o.task_id, { checked: e.target.checked })}
                        />
                        <span style={{ fontWeight: 700 }}>{o.task_id}</span>
                        <span className="cpm-muted">{o.task_name}</span>
                      </label>
                      {row.checked && (
                        <>
                          <select
                            value={row.dep_type}
                            onChange={(e) => setDepDraft(o.task_id, { dep_type: e.target.value })}
                            title={t(region, 'dependencyType')}
                          >
                            {DEP_TYPES.map((dt) => (
                              <option key={dt} value={dt}>
                                {dt}
                              </option>
                            ))}
                          </select>
                          <input
                            type="number"
                            step="1"
                            value={row.lag_days}
                            onChange={(e) => setDepDraft(o.task_id, { lag_days: e.target.value })}
                            title={t(region, 'lagDays')}
                            style={{ ...inputStyle, width: '70px' }}
                          />
                        </>
                      )}
                    </div>
                  );
                })}
              {tasks.length <= 1 && <div className="cpm-muted">{t(region, 'none')}</div>}
            </div>
            <div className="cpm-modal-footer">
              <button type="button" className="secondary" onClick={() => setDepEdit(null)}>
                {t(region, 'cancel')}
              </button>
              <button type="button" onClick={handleSaveDeps}>
                {t(region, 'save')}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* ===== 狀態列 ===== */}
      {loading && (
        <div style={{ padding: '8px', color: '#2980b9' }}>{t(region, 'loading')}…</div>
      )}
      {error && (
        <div
          style={{
            padding: '8px 12px',
            color: '#c0392b',
            background: '#fdecea',
            border: '1px solid #f5c6cb',
            borderRadius: '4px',
            marginBottom: '12px',
          }}
        >
          {t(region, 'error')}: {String(error)}
        </div>
      )}

      {currentProject && (
        <>
          {/* ===== 摘要：專案工期 + 要徑 ===== */}
          <div
            style={{
              display: 'flex',
              flexWrap: 'wrap',
              gap: '24px',
              padding: '12px 16px',
              background: '#2c3e50',
              color: '#fff',
              borderRadius: '6px',
              marginBottom: '16px',
            }}
          >
            <div>
              <div style={{ fontSize: '12px', opacity: 0.8 }}>{t(region, 'projectName')}</div>
              <div style={{ fontSize: '16px', fontWeight: 700 }}>{currentProject.project_name}</div>
            </div>
            <div>
              <div style={{ fontSize: '12px', opacity: 0.8 }}>{t(region, 'projectDuration')}</div>
              <div style={{ fontSize: '16px', fontWeight: 700 }}>
                {currentProject.project_duration} {t(region, 'days')}
              </div>
            </div>
            {/* Batch 3：開工日期（有設定時顯示於工期旁） */}
            {currentProject.start_date && (
              <div>
                <div style={{ fontSize: '12px', opacity: 0.8 }}>{t(region, 'startDate')}</div>
                <div style={{ fontSize: '16px', fontWeight: 700 }}>
                  {currentProject.start_date}
                </div>
              </div>
            )}
            <div style={{ flex: 1, minWidth: '200px' }}>
              <div style={{ fontSize: '12px', opacity: 0.8 }}>{t(region, 'criticalPath')}</div>
              <div style={{ fontSize: '15px', fontWeight: 700, color: '#ff7675' }}>
                🔥 {criticalPathStr || '—'}
              </div>
            </div>
          </div>

          {/* ===== 操作按鈕列 ===== */}
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: '8px', marginBottom: '16px' }}>
            <button style={{ ...btnStyle, background: '#2980b9' }} onClick={handleRecalc}>
              {t(region, 'recalc')}
            </button>
            {/* 拋轉 ERP 為寫入動作：viewer 隱藏 */}
            {canWrite && (
              <button style={{ ...btnStyle, background: '#8e44ad' }} onClick={handleSyncErp}>
                {t(region, 'syncErp')}
              </button>
            )}
            <button style={{ ...btnStyle, background: '#d35400' }} onClick={handleDownloadReport}>
              {t(region, 'downloadReport')}
            </button>
            {/* 匯出（唯讀，viewer 亦可）：Excel + PDF，皆以 Bearer 驗證下載 */}
            <button style={{ ...btnStyle, background: '#1e8449' }} onClick={handleExportExcel}>
              {t(region, 'exportExcel')}
            </button>
            <button style={{ ...btnStyle, background: '#c0392b' }} onClick={handleExportPdf}>
              {t(region, 'exportPdf')}
            </button>
          </div>

          {/* ===== Phase 8 分頁切換：排程 / 資源撫平 / 風險分析 ===== */}
          <div
            style={{
              display: 'flex',
              gap: '4px',
              borderBottom: '2px solid #e0e0e0',
              marginBottom: '16px',
            }}
          >
            {[
              { key: 'schedule', label: `${t(region, 'task')} / ${t(region, 'criticalPath')}` },
              { key: 'resources', label: t(region, 'resourceLeveling') },
              { key: 'risk', label: t(region, 'riskAnalysis') },
              { key: 'progress', label: `${t(region, 'progress')} / ${t(region, 'evm')}` },
            ].map((tab) => {
              const active = activeTab === tab.key;
              return (
                <button
                  key={tab.key}
                  onClick={() => setActiveTab(tab.key)}
                  style={{
                    ...btnStyle,
                    background: active ? '#2c3e50' : '#fff',
                    color: active ? '#fff' : '#2c3e50',
                    border: '1px solid #e0e0e0',
                    borderBottom: active ? '2px solid #2c3e50' : '2px solid transparent',
                    borderTopLeftRadius: '4px',
                    borderTopRightRadius: '4px',
                    borderBottomLeftRadius: 0,
                    borderBottomRightRadius: 0,
                    marginBottom: '-2px',
                  }}
                >
                  {tab.label}
                </button>
              );
            })}
          </div>

          {/* ===== 資源撫平分頁 ===== */}
          {activeTab === 'resources' && <ResourcePanel region={region} />}

          {/* ===== 風險分析分頁 ===== */}
          {activeTab === 'risk' && <RiskPanel region={region} />}

          {/* ===== 進度 / EVM 分頁（Phase 9） ===== */}
          {activeTab === 'progress' && <ProgressPanel region={region} />}

          {/* ===== 排程分頁（甘特圖 + 任務表格 + 新增任務） ===== */}
          {activeTab === 'schedule' && (
          <>
          {/* ===== 甘特圖 ===== */}
          <div style={{ marginBottom: '20px' }}>
            {tasks.length > 0 ? (
              <GanttChart
                tasks={tasks}
                region={region}
                onTaskDurationChange={canWrite ? (id, d) => changeTaskDuration(id, d) : undefined}
                overCapacityDays={overCapacityDays}
                baseline={baseline}
                progress={progressMap}
                dayDates={currentProject.day_dates || undefined}
              />
            ) : (
              <div style={{ padding: '24px', textAlign: 'center', color: '#999', border: '1px dashed #ddd', borderRadius: '6px' }}>
                {t(region, 'task')} — {t(region, 'addTask')}
              </div>
            )}
          </div>

          {/* ===== 任務表格 ===== */}
          <table
            style={{
              width: '100%',
              borderCollapse: 'collapse',
              fontSize: '13px',
              marginBottom: '24px',
            }}
          >
            <thead>
              <tr style={{ background: '#f7f9fc', textAlign: 'left' }}>
                <th style={thStyle}>{t(region, 'taskId')}</th>
                <th style={thStyle}>{t(region, 'taskName')}</th>
                <th style={thStyle}>{t(region, 'status')}</th>
                <th style={thStyle}>{t(region, 'duration')}</th>
                <th style={thStyle}>{t(region, 'floatTime')}</th>
                <th style={thStyle}>{t(region, 'critical')}</th>
                <th style={thStyle}>Pred.</th>
                {canWrite && <th style={thStyle}>{t(region, 'updateDuration')}</th>}
                {canWrite && <th style={thStyle}>{t(region, 'delete')}</th>}
              </tr>
            </thead>
            <tbody>
              {tasks.length === 0 && (
                <tr>
                  <td style={{ ...tdStyle, textAlign: 'center', color: '#999' }} colSpan={canWrite ? 9 : 7}>
                    {t(region, 'addTask')}
                  </td>
                </tr>
              )}
              {tasks.map((tk) => {
                const critical = tk.is_critical || tk.float_time === 0;
                return (
                  <tr key={tk.task_id} style={{ borderBottom: '1px solid #eee' }}>
                    <td style={{ ...tdStyle, fontWeight: 700, color: critical ? '#e74c3c' : '#2c3e50' }}>
                      {tk.task_id}
                    </td>
                    <td style={tdStyle}>{tk.task_name}</td>
                    <td style={tdStyle}>{statusLabel(region, tk.status)}</td>
                    <td style={tdStyle}>
                      <input
                        type="number"
                        min="0"
                        readOnly={!canWrite}
                        style={{ ...inputStyle, width: '70px', ...(canWrite ? {} : { background: '#f1f1f1' }) }}
                        value={durationDrafts[tk.task_id] ?? tk.duration}
                        onChange={(e) => handleDraftChange(tk.task_id, e.target.value)}
                      />
                    </td>
                    <td style={tdStyle}>{tk.float_time}</td>
                    <td style={tdStyle}>{critical ? '🔥' : ''}</td>
                    <td style={{ ...tdStyle, color: '#666' }}>
                      {/* Batch 3：有 links 時以 PRED(類型±lag) 顯示；否則沿用 predecessors */}
                      {Array.isArray(tk.links) && tk.links.length > 0
                        ? tk.links.map((l) => linkLabel(l)).join(', ')
                        : (tk.predecessors || []).join(', ')}
                      {canWrite && (
                        <button
                          type="button"
                          style={{
                            ...btnStyle,
                            padding: '2px 8px',
                            fontSize: '11px',
                            background: '#7f8c8d',
                            marginLeft: '6px',
                          }}
                          onClick={() => openDepEditor(tk)}
                          title={t(region, 'editDependencies')}
                        >
                          {t(region, 'editDependencies')}
                        </button>
                      )}
                    </td>
                    {canWrite && (
                      <td style={tdStyle}>
                        <button
                          style={{ ...btnStyle, padding: '4px 10px', background: '#27ae60' }}
                          onClick={() => handleUpdateDuration(tk.task_id)}
                        >
                          {t(region, 'updateDuration')}
                        </button>
                      </td>
                    )}
                    {canWrite && (
                      <td style={tdStyle}>
                        <button
                          style={{ ...btnStyle, padding: '4px 10px', background: '#e74c3c' }}
                          onClick={() => handleRemoveTask(tk.task_id)}
                          title={t(region, 'deleteTask')}
                        >
                          {t(region, 'delete')}
                        </button>
                      </td>
                    )}
                  </tr>
                );
              })}
            </tbody>
          </table>

          {/* ===== 新增任務表單（viewer 隱藏整個寫入表單） ===== */}
          {canWrite && (
          <div
            style={{
              padding: '12px',
              border: '1px dashed #bbb',
              borderRadius: '6px',
              background: '#fcfcfc',
            }}
          >
            <div style={{ fontSize: '14px', fontWeight: 700, marginBottom: '8px', color: '#2c3e50' }}>
              {t(region, 'addTask')}
            </div>
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: '8px', alignItems: 'flex-end' }}>
              <Field label={t(region, 'taskId')}>
                <input
                  style={{ ...inputStyle, width: '110px' }}
                  value={newTask.task_id}
                  onChange={(e) => setNewTask({ ...newTask, task_id: e.target.value })}
                  placeholder="T-04"
                />
              </Field>
              <Field label={t(region, 'taskName')}>
                <input
                  style={{ ...inputStyle, width: '160px' }}
                  value={newTask.task_name}
                  onChange={(e) => setNewTask({ ...newTask, task_name: e.target.value })}
                />
              </Field>
              <Field label={t(region, 'duration')}>
                <input
                  type="number"
                  min="0"
                  style={{ ...inputStyle, width: '80px' }}
                  value={newTask.duration}
                  onChange={(e) => setNewTask({ ...newTask, duration: e.target.value })}
                />
              </Field>
              <Field label="Pred. (a,b)">
                <input
                  style={{ ...inputStyle, width: '140px' }}
                  value={newTask.predecessors}
                  onChange={(e) => setNewTask({ ...newTask, predecessors: e.target.value })}
                  placeholder="T-01,T-02"
                />
              </Field>
              <Field label={t(region, 'status')}>
                <select
                  style={{ ...inputStyle, width: '130px' }}
                  value={newTask.status}
                  onChange={(e) => setNewTask({ ...newTask, status: e.target.value })}
                >
                  {STATUS_VALUES.map((s) => (
                    <option key={s} value={s}>
                      {statusLabel(region, s)}
                    </option>
                  ))}
                </select>
              </Field>
              <button style={{ ...btnStyle, background: '#27ae60' }} onClick={handleAddTask}>
                {t(region, 'addTask')}
              </button>
            </div>
          </div>
          )}
          </>
          )}
        </>
      )}

      {!currentProject && !loading && (
        <div style={{ padding: '40px', textAlign: 'center', color: '#999' }}>
          {t(region, 'project')} — {t(region, 'projectName')}
        </div>
      )}
    </div>
  );
}

// 表頭/儲存格樣式
const thStyle = {
  padding: '8px 10px',
  borderBottom: '2px solid #ddd',
  fontSize: '12px',
  color: '#555',
};
const tdStyle = {
  padding: '6px 10px',
  verticalAlign: 'middle',
};

// 表單欄位小元件
function Field({ label, children }) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '3px' }}>
      <label style={{ fontSize: '11px', color: '#777' }}>{label}</label>
      {children}
    </div>
  );
}
