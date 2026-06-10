import React, { useEffect, useMemo, useState } from 'react';
import { useScheduleStore } from '../store/scheduleStore';
import { t } from '../i18n';
import { reportUrl } from '../api/client';
import GanttChart from './GanttChart';
import ProjectForm from './ProjectForm';

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
    projects,
    currentProject,
    loading,
    error,
    setTenant,
    setRegion,
    loadProjects,
    loadProject,
    changeTaskDuration,
    addTask,
    removeTask,
    createProject,
    syncErp,
  } = useScheduleStore();

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

  const tasks = currentProject?.tasks || [];

  // 要徑摘要：依 es 排序後串接要徑 task_id
  const criticalPathStr = useMemo(() => {
    const crit = tasks
      .filter((tk) => tk.is_critical || tk.float_time === 0)
      .sort((a, b) => (Number(a.es) || 0) - (Number(b.es) || 0))
      .map((tk) => tk.task_id);
    return crit.join(' → ');
  }, [tasks]);

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

  const handleDownloadReport = () => {
    if (currentProject?.project_id) {
      // reportUrl 回傳報表端點 URL；於新分頁開啟 (PDF 串流)
      window.open(reportUrl(currentProject.project_id), '_blank');
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

        {/* 新增專案 */}
        <div style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
          <label style={{ fontSize: '12px', color: '#666' }}>&nbsp;</label>
          <button
            style={{ ...btnStyle, background: '#27ae60' }}
            onClick={() => setShowProjectForm(true)}
          >
            + {t(region, 'newProject')}
          </button>
        </div>
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
            <button style={{ ...btnStyle, background: '#8e44ad' }} onClick={handleSyncErp}>
              {t(region, 'syncErp')}
            </button>
            <button style={{ ...btnStyle, background: '#d35400' }} onClick={handleDownloadReport}>
              {t(region, 'downloadReport')}
            </button>
          </div>

          {/* ===== 甘特圖 ===== */}
          <div style={{ marginBottom: '20px' }}>
            {tasks.length > 0 ? (
              <GanttChart
                tasks={tasks}
                region={region}
                onTaskDurationChange={(id, d) => changeTaskDuration(id, d)}
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
                <th style={thStyle}>{t(region, 'updateDuration')}</th>
                <th style={thStyle}>{t(region, 'delete')}</th>
              </tr>
            </thead>
            <tbody>
              {tasks.length === 0 && (
                <tr>
                  <td style={{ ...tdStyle, textAlign: 'center', color: '#999' }} colSpan={9}>
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
                        style={{ ...inputStyle, width: '70px' }}
                        value={durationDrafts[tk.task_id] ?? tk.duration}
                        onChange={(e) => handleDraftChange(tk.task_id, e.target.value)}
                      />
                    </td>
                    <td style={tdStyle}>{tk.float_time}</td>
                    <td style={tdStyle}>{critical ? '🔥' : ''}</td>
                    <td style={{ ...tdStyle, color: '#666' }}>
                      {(tk.predecessors || []).join(', ')}
                    </td>
                    <td style={tdStyle}>
                      <button
                        style={{ ...btnStyle, padding: '4px 10px', background: '#27ae60' }}
                        onClick={() => handleUpdateDuration(tk.task_id)}
                      >
                        {t(region, 'updateDuration')}
                      </button>
                    </td>
                    <td style={tdStyle}>
                      <button
                        style={{ ...btnStyle, padding: '4px 10px', background: '#e74c3c' }}
                        onClick={() => handleRemoveTask(tk.task_id)}
                        title={t(region, 'deleteTask')}
                      >
                        {t(region, 'delete')}
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>

          {/* ===== 新增任務表單 ===== */}
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
