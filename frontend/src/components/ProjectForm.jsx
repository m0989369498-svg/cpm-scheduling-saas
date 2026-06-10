import React, { useState } from 'react';
import { t } from '../i18n';

/**
 * ProjectForm 建立專案表單（模態）
 *
 * props:
 *   region      : 'TW' | 'CN'        (i18n 語系 + region 預設值)
 *   defaultRegion : 'TW' | 'CN'      (任務區域預設，取自 store.region)
 *   onSubmit    : (payload) => Promise  接收 ProjectCreate 形狀；成功後外層關閉
 *   onCancel    : () => void
 *   serverError : string | null      來自 store.error（後端錯誤回顯）
 *   submitting  : boolean            來自 store.loading
 *
 * payload (ProjectCreate)：
 *   {
 *     project_name: str (必填),
 *     region: 'TW' | 'CN',
 *     project_id?: str (留白則後端指派),
 *     schedule_data: [ { task_id, task_name, duration(int>=0), predecessors:[str], status } ]
 *   }
 *
 * 用戶端基本驗證：
 *   - 專案名稱必填
 *   - 至少一個任務
 *   - 每列須有 task_id；task_id 不可重複
 *   - duration 須為 >=0 整數
 * predecessors 限制為「已列在前面的 task_id」（多選框）。
 */

const REGIONS = ['TW', 'CN'];
const STATUS_VALUES = ['PENDING', 'IN_PROGRESS', 'COMPLETED', 'DELAYED'];

function statusLabel(region, status) {
  if (!status) return '';
  const dotKey = `statuses.${status}`;
  const val = t(region, dotKey);
  if (val && val !== dotKey && val !== status) return val;
  return status;
}

let rowSeq = 0;
function makeRow() {
  rowSeq += 1;
  return {
    _key: `row-${rowSeq}`,
    task_id: '',
    task_name: '',
    duration: 1,
    status: 'PENDING',
    predecessors: [],
  };
}

export default function ProjectForm({
  region = 'TW',
  defaultRegion = 'TW',
  onSubmit,
  onCancel,
  serverError = null,
  submitting = false,
}) {
  const [projectName, setProjectName] = useState('');
  const [projectId, setProjectId] = useState('');
  const [formRegion, setFormRegion] = useState(REGIONS.includes(defaultRegion) ? defaultRegion : 'TW');
  const [rows, setRows] = useState([makeRow()]);
  const [validationError, setValidationError] = useState('');

  const updateRow = (key, patch) => {
    setRows((prev) => prev.map((r) => (r._key === key ? { ...r, ...patch } : r)));
  };

  const addRow = () => setRows((prev) => [...prev, makeRow()]);

  const removeRow = (key) => {
    setRows((prev) => {
      const next = prev.filter((r) => r._key !== key);
      // 移除某列後，仍指向該列 task_id 的前置依賴需一併清掉
      const removed = prev.find((r) => r._key === key);
      const removedId = removed ? removed.task_id : '';
      return next.map((r) => ({
        ...r,
        predecessors: removedId ? r.predecessors.filter((p) => p !== removedId) : r.predecessors,
      }));
    });
  };

  // 某列的可選前置任務：在它之前（index 較小）且已填 task_id 的列
  const predecessorOptions = (rowIndex) =>
    rows
      .slice(0, rowIndex)
      .map((r) => r.task_id.trim())
      .filter(Boolean);

  const togglePredecessor = (key, predId, checked) => {
    setRows((prev) =>
      prev.map((r) => {
        if (r._key !== key) return r;
        const set = new Set(r.predecessors);
        if (checked) set.add(predId);
        else set.delete(predId);
        return { ...r, predecessors: Array.from(set) };
      }),
    );
  };

  const validate = () => {
    if (!projectName.trim()) return t(region, 'nameRequired');
    if (rows.length < 1) return t(region, 'atLeastOneTask');
    const ids = [];
    for (const r of rows) {
      const id = r.task_id.trim();
      if (!id) return t(region, 'taskIdRequired');
      const dur = Number(r.duration);
      if (!Number.isInteger(dur) || dur < 0) return t(region, 'invalidDuration');
      ids.push(id);
    }
    const unique = new Set(ids);
    if (unique.size !== ids.length) return t(region, 'duplicateTaskId');
    return '';
  };

  const buildPayload = () => {
    const validIds = new Set(rows.map((r) => r.task_id.trim()).filter(Boolean));
    const payload = {
      project_name: projectName.trim(),
      region: formRegion,
      schedule_data: rows.map((r) => ({
        task_id: r.task_id.trim(),
        task_name: r.task_name.trim(),
        duration: Number.parseInt(r.duration, 10) || 0,
        // 僅保留指向實際存在 task_id 的前置依賴
        predecessors: r.predecessors.filter((p) => validIds.has(p)),
        status: r.status || 'PENDING',
      })),
    };
    const pid = projectId.trim();
    if (pid) payload.project_id = pid;
    return payload;
  };

  const handleSubmit = async (e) => {
    if (e && e.preventDefault) e.preventDefault();
    const err = validate();
    if (err) {
      setValidationError(err);
      return;
    }
    setValidationError('');
    try {
      await onSubmit(buildPayload());
    } catch (_) {
      // 後端錯誤由 serverError 顯示；表單保持開啟讓用戶修正
    }
  };

  return (
    <div className="cpm-modal-overlay" role="dialog" aria-modal="true" onMouseDown={onCancel}>
      <div className="cpm-modal" onMouseDown={(e) => e.stopPropagation()}>
        <div className="cpm-modal-header">
          <h2 className="cpm-modal-title">{t(region, 'newProject')}</h2>
          <button
            type="button"
            className="cpm-modal-close"
            aria-label={t(region, 'cancel')}
            onClick={onCancel}
          >
            ×
          </button>
        </div>

        <form onSubmit={handleSubmit}>
          <div className="cpm-modal-body">
            {/* 專案基本欄位 */}
            <div className="cpm-form-grid">
              <div className="cpm-form-field">
                <label>
                  {t(region, 'projectName')} <span className="cpm-required">*</span>
                </label>
                <input
                  type="text"
                  value={projectName}
                  onChange={(e) => setProjectName(e.target.value)}
                  placeholder={t(region, 'projectName')}
                />
              </div>
              <div className="cpm-form-field">
                <label>{t(region, 'region')}</label>
                <select value={formRegion} onChange={(e) => setFormRegion(e.target.value)}>
                  {REGIONS.map((r) => (
                    <option key={r} value={r}>
                      {r}
                    </option>
                  ))}
                </select>
              </div>
              <div className="cpm-form-field">
                <label>
                  {t(region, 'project')} ID <span className="cpm-optional">({t(region, 'none')})</span>
                </label>
                <input
                  type="text"
                  value={projectId}
                  onChange={(e) => setProjectId(e.target.value)}
                  placeholder="(auto)"
                />
              </div>
            </div>

            {/* 動態任務列 */}
            <div className="cpm-task-rows">
              <div className="cpm-task-rows-head">
                <span>{t(region, 'task')}</span>
                <button type="button" className="small secondary" onClick={addRow}>
                  + {t(region, 'addTaskRow')}
                </button>
              </div>

              <table className="cpm-task-rows-table">
                <thead>
                  <tr>
                    <th>{t(region, 'taskId')} *</th>
                    <th>{t(region, 'taskName')}</th>
                    <th>{t(region, 'duration')}</th>
                    <th>{t(region, 'status')}</th>
                    <th>{t(region, 'predecessors')}</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {rows.map((r, idx) => {
                    const opts = predecessorOptions(idx);
                    return (
                      <tr key={r._key}>
                        <td>
                          <input
                            type="text"
                            style={{ width: '90px' }}
                            value={r.task_id}
                            onChange={(e) => updateRow(r._key, { task_id: e.target.value })}
                            placeholder="T-01"
                          />
                        </td>
                        <td>
                          <input
                            type="text"
                            style={{ width: '150px' }}
                            value={r.task_name}
                            onChange={(e) => updateRow(r._key, { task_name: e.target.value })}
                          />
                        </td>
                        <td>
                          <input
                            type="number"
                            min="0"
                            style={{ width: '70px' }}
                            value={r.duration}
                            onChange={(e) => updateRow(r._key, { duration: e.target.value })}
                          />
                        </td>
                        <td>
                          <select
                            value={r.status}
                            onChange={(e) => updateRow(r._key, { status: e.target.value })}
                          >
                            {STATUS_VALUES.map((s) => (
                              <option key={s} value={s}>
                                {statusLabel(region, s)}
                              </option>
                            ))}
                          </select>
                        </td>
                        <td>
                          {opts.length === 0 ? (
                            <span className="cpm-muted">{t(region, 'none')}</span>
                          ) : (
                            <div className="cpm-pred-options">
                              {opts.map((pid) => (
                                <label key={pid} className="cpm-pred-chip">
                                  <input
                                    type="checkbox"
                                    checked={r.predecessors.includes(pid)}
                                    onChange={(e) =>
                                      togglePredecessor(r._key, pid, e.target.checked)
                                    }
                                  />
                                  {pid}
                                </label>
                              ))}
                            </div>
                          )}
                        </td>
                        <td>
                          <button
                            type="button"
                            className="small danger"
                            onClick={() => removeRow(r._key)}
                            disabled={rows.length <= 1}
                            title={t(region, 'removeRow')}
                          >
                            {t(region, 'removeRow')}
                          </button>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>

            {/* 錯誤訊息 */}
            {validationError && (
              <div className="notice error" style={{ marginTop: '12px' }}>
                {validationError}
              </div>
            )}
            {serverError && (
              <div className="notice error" style={{ marginTop: '12px' }}>
                {t(region, 'error')}: {String(serverError)}
              </div>
            )}
          </div>

          <div className="cpm-modal-footer">
            <button type="button" className="secondary" onClick={onCancel} disabled={submitting}>
              {t(region, 'cancel')}
            </button>
            <button type="submit" disabled={submitting}>
              {submitting ? `${t(region, 'loading')}` : t(region, 'save')}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
