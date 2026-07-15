import React, { useEffect, useMemo, useRef, useState } from 'react';
import { useScheduleStore, isLoading, getError } from '../store/scheduleStore';
import { t, tStatus } from '../i18n';
import { photoUrl } from '../api/client';

/**
 * FieldMode 行動裝置現場回報（Pro Batch C · Feature 3）
 *
 * 行動優先（單欄、大觸控目標 ≥44px）的精簡介面，供工地人員以手機/平板：
 *   - 選擇專案 -> 瀏覽任務卡片（task_id / 名稱 / 狀態徽章 / 完成度長條）
 *   - 點選卡片開啟「回報單」：完成度滑桿(0-100/5) + 實際成本 + 備註 + 拍照上傳（多張）
 *   - 送出：
 *       線上 -> 併入現有進度後 PUT 進度 + 依序上傳照片
 *       離線（navigator.onLine=false 或請求失敗）-> 暫存至離線佇列（IndexedDB，
 *       見 offline/fieldQueue.js），畫面樂觀顯示「待同步」
 *   - 頂部列：專案選擇器 + 連線狀態徽章 + 待同步筆數（點擊「立即同步」重播佇列）
 *
 * 與離線佇列模組（frontend/src/offline/fieldQueue.js）之間一律以動態 import()
 * 銜接：該模組屬另一開發項的產出，動態載入 + try/catch 讓其尚未就緒時本元件
 * 仍可運作於「純線上」模式（enqueue 失敗僅記錄，不阻斷畫面）。
 */

const PERCENT_STEP = 5;

function clamp(n, lo, hi) {
  return Math.min(hi, Math.max(lo, n));
}

function statusLabel(region, status) {
  if (!status) return '';
  const val = tStatus(region, status);
  return val && val !== `statuses.${status}` ? val : status;
}

// 動態載入離線佇列模組（見上方模組註解）；失敗回傳 null。
async function loadFieldQueue() {
  try {
    return await import('../offline/fieldQueue.js');
  } catch (e) {
    return null;
  }
}

export default function FieldMode({ initialProjectId = '', initialTaskId = '' }) {
  const store = useScheduleStore();
  const {
    region,
    role,
    username,
    projects,
    currentProject,
    progress,
    photosByTask,
    fieldQueueCount,
    logout,
    loadProjects,
    loadProject,
    loadProgress,
    saveProgress,
    loadTaskPhotos,
    uploadTaskPhoto,
    deleteTaskPhoto,
    refreshFieldQueueCount,
    syncFieldQueue,
  } = store;

  const canWrite = (role || 'admin') !== 'viewer';
  const busy = isLoading(store, 'progress') || isLoading(store, 'photos');
  const syncing = isLoading(store, 'fieldQueue');
  const panelError = getError(store, 'progress') || getError(store, 'photos');

  const [selectedProjectId, setSelectedProjectId] = useState(initialProjectId || '');
  const [online, setOnline] = useState(typeof navigator === 'undefined' ? true : navigator.onLine);
  const [activeTask, setActiveTask] = useState(null); // 開啟中的回報單目標任務
  const [draft, setDraft] = useState({ percent_complete: 0, actual_cost: 0, note: '' });
  const [files, setFiles] = useState([]);
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState('');
  const [taskStatus, setTaskStatus] = useState({}); // { [taskId]: 'synced' | 'queued' }
  const fileInputRef = useRef(null);
  const statusTimers = useRef({});

  // 掛載：載入專案清單（若尚未載入）+ 刷新離線佇列筆數 + 嘗試同步（連線時 best-effort）。
  useEffect(() => {
    if (!Array.isArray(projects) || projects.length === 0) {
      loadProjects().catch(() => {});
    }
    refreshFieldQueueCount().catch(() => {});
    if (typeof navigator === 'undefined' || navigator.onLine) {
      syncFieldQueue().catch(() => {});
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 上線/離線事件：更新徽章 + 恢復連線時觸發同步。
  useEffect(() => {
    if (typeof window === 'undefined') return undefined;
    const handleOnline = () => {
      setOnline(true);
      syncFieldQueue().catch(() => {});
    };
    const handleOffline = () => setOnline(false);
    window.addEventListener('online', handleOnline);
    window.addEventListener('offline', handleOffline);
    return () => {
      window.removeEventListener('online', handleOnline);
      window.removeEventListener('offline', handleOffline);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 專案下拉未選取時，預設帶入目前專案（若有）。
  useEffect(() => {
    if (!selectedProjectId && currentProject?.project_id) {
      setSelectedProjectId(currentProject.project_id);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [currentProject?.project_id]);

  // 選取專案變更（含掛載時的 initialProjectId）-> 載入該專案 + 進度。
  // progressReadyId 記錄「進度已至少嘗試載入一次」的專案 id，供下方 QR 深連結
  // 自動開啟回報單的 effect 等待，避免在 progress 尚未回來前以預設值 0% 開啟
  // 回報單（使用者若未調整滑桿直接送出，會把既有完成度悄悄覆蓋為 0%）。
  const [progressReadyId, setProgressReadyId] = useState('');
  useEffect(() => {
    if (selectedProjectId && selectedProjectId !== currentProject?.project_id) {
      loadProject(selectedProjectId)
        .then(() => loadProgress().catch(() => {}))
        .catch(() => {})
        .finally(() => setProgressReadyId(selectedProjectId));
    } else if (selectedProjectId) {
      loadProgress()
        .catch(() => {})
        .finally(() => setProgressReadyId(selectedProjectId));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedProjectId]);

  // 卸載時清除暫存的「已同步/待同步」狀態計時器
  useEffect(
    () => () => {
      Object.values(statusTimers.current).forEach((tm) => clearTimeout(tm));
    },
    [],
  );

  const tasks = currentProject?.tasks || [];
  const progressByTask = useMemo(() => {
    const map = {};
    (progress || []).forEach((p) => {
      if (p && p.task_id != null) map[p.task_id] = p;
    });
    return map;
  }, [progress]);

  // 初始 task 深連結（QR 掃描帶入 task 參數）：專案 + 進度皆載入完成後自動開啟
  // 回報單一次。務必等待 progressReadyId 對上目前專案，才能保證 openSheet 讀到
  // 的 progressByTask 是該任務「目前」的完成度，而非預設的 0%（見上方 progressReadyId 註解）。
  const appliedInitialTask = useRef(false);
  useEffect(() => {
    if (
      !appliedInitialTask.current &&
      initialTaskId &&
      currentProject?.project_id === selectedProjectId &&
      progressReadyId === selectedProjectId &&
      tasks.length > 0
    ) {
      const found = tasks.find((tk) => tk.task_id === initialTaskId);
      if (found) openSheet(found);
      appliedInitialTask.current = true;
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tasks, currentProject?.project_id, selectedProjectId, progressReadyId]);

  const markStatus = (taskId, status) => {
    setTaskStatus((prev) => ({ ...prev, [taskId]: status }));
    if (statusTimers.current[taskId]) clearTimeout(statusTimers.current[taskId]);
    statusTimers.current[taskId] = setTimeout(() => {
      setTaskStatus((prev) => {
        const next = { ...prev };
        delete next[taskId];
        return next;
      });
    }, 6000);
  };

  const openSheet = (task) => {
    const p = progressByTask[task.task_id];
    setDraft({
      percent_complete: p ? Number(p.percent_complete) || 0 : 0,
      actual_cost: p ? Number(p.actual_cost) || 0 : 0,
      note: '',
    });
    setFiles([]);
    setSubmitError('');
    setActiveTask(task);
    loadTaskPhotos(task.task_id).catch(() => {});
  };

  const closeSheet = () => {
    setActiveTask(null);
    setFiles([]);
  };

  const handleFileChange = (e) => {
    const picked = Array.from(e.target.files || []);
    if (picked.length > 0) setFiles((prev) => [...prev, ...picked]);
    e.target.value = '';
  };

  const removeFile = (idx) => {
    setFiles((prev) => prev.filter((_, i) => i !== idx));
  };

  // 離線暫存：progress 一筆 + 每張照片各一筆，皆入列離線佇列（模組缺席時靜默略過）。
  const enqueueOffline = async (taskId, payload, note, pickedFiles) => {
    const mod = await loadFieldQueue();
    if (!mod || typeof mod.enqueue !== 'function') return false;
    try {
      await mod.enqueue({
        type: 'progress',
        projectId: currentProject.project_id,
        taskId,
        payload,
        queuedAt: Date.now(),
      });
      for (const file of pickedFiles) {
        // eslint-disable-next-line no-await-in-loop
        await mod.enqueue({
          type: 'photo',
          projectId: currentProject.project_id,
          taskId,
          payload: { blob: file, note },
          queuedAt: Date.now(),
        });
      }
      await refreshFieldQueueCount();
      return true;
    } catch (e) {
      return false;
    }
  };

  const handleSubmit = async () => {
    if (!activeTask || !currentProject || submitting) return;
    const taskId = activeTask.task_id;
    // 回報單只攜帶使用者實際編輯的欄位（完成度 + 實際成本）。budget /
    // actual_start_day / actual_finish_day 一律於「實際送出當下」以伺服器最新
    // 值補齊：線上路徑在 trySubmitOnline 重新抓取、離線重播在
    // store.syncFieldQueue 的 progress handler 重新抓取 —— 避免把選案當下的
    // 過期快照寫回伺服器（進度 PUT 是整份清單 upsert）。
    const report = {
      percent_complete: clamp(Number.parseInt(draft.percent_complete, 10) || 0, 0, 100),
      actual_cost: Math.max(0, Number(draft.actual_cost) || 0),
    };
    const note = draft.note || '';
    const pickedFiles = files;
    const isOnline = typeof navigator === 'undefined' ? true : navigator.onLine;

    setSubmitting(true);
    setSubmitError('');

    const trySubmitOnline = async () => {
      // PUT /progress 會覆寫 payload 內出現的「每一個」task_id：務必在送出前
      // 重新抓取目前清單，而非沿用選案當下的 progress 快照，否則其他使用者
      // 剛更新的任務進度會被舊快照悄悄蓋回（與 store.syncFieldQueue 的離線
      // 重播 handler 相同模式）。
      const freshRaw = await loadProgress();
      const fresh = Array.isArray(freshRaw) ? freshRaw : [];
      const freshExisting = fresh.find((p) => p && p.task_id === taskId) || {};
      const merged = fresh.filter((p) => p && p.task_id !== taskId);
      merged.push({
        task_id: taskId,
        budget: Number(freshExisting.budget) || 0,
        actual_start_day: freshExisting.actual_start_day ?? null,
        actual_finish_day: freshExisting.actual_finish_day ?? null,
        ...report,
      });
      await saveProgress(merged);
      // eslint-disable-next-line no-restricted-syntax
      for (const file of pickedFiles) {
        // 依序上傳（非並行）：避免行動網路上大量並行連線造成逾時。
        // eslint-disable-next-line no-await-in-loop
        await uploadTaskPhoto(taskId, file, note);
      }
    };

    if (isOnline) {
      try {
        await trySubmitOnline();
        markStatus(taskId, 'synced');
        closeSheet();
      } catch (e) {
        // 線上但請求失敗（弱訊號/伺服器錯誤）：退回離線暫存，不遺失使用者輸入。
        const queued = await enqueueOffline(taskId, report, note, pickedFiles);
        if (queued) {
          markStatus(taskId, 'queued');
          closeSheet();
        } else {
          setSubmitError(t(region, 'error'));
        }
      }
    } else {
      const queued = await enqueueOffline(taskId, report, note, pickedFiles);
      if (queued) {
        markStatus(taskId, 'queued');
        closeSheet();
      } else {
        setSubmitError(t(region, 'error'));
      }
    }
    setSubmitting(false);
  };

  const handleSyncNow = () => {
    syncFieldQueue().catch(() => {});
  };

  const handleDeletePhoto = async (taskId, photoId) => {
    // eslint-disable-next-line no-alert
    if (!window.confirm(t(region, 'confirmDeletePhoto'))) return;
    await deleteTaskPhoto(taskId, photoId).catch(() => {});
  };

  const handleExitFieldMode = () => {
    try {
      const url = new URL(window.location.href);
      url.searchParams.delete('field');
      url.searchParams.delete('project');
      url.searchParams.delete('task');
      window.location.href = url.toString();
    } catch (e) {
      window.location.href = '/';
    }
  };

  const activePhotos = activeTask ? photosByTask[activeTask.task_id] || [] : [];

  return (
    <div className="field-mode">
      <header className="field-header">
        <div className="field-header-top">
          <strong className="field-title">📱 {t(region, 'fieldMode')}</strong>
          <div className="field-header-actions">
            <span className={`field-badge ${online ? 'field-badge-online' : 'field-badge-offline'}`}>
              {online ? `🟢 ${t(region, 'online')}` : `🔴 ${t(region, 'offline')}`}
            </span>
            <button type="button" className="field-exit-btn" onClick={handleExitFieldMode}>
              {t(region, 'exitFieldMode')}
            </button>
          </div>
        </div>
        <div className="field-header-row">
          <select
            className="field-select"
            value={selectedProjectId}
            onChange={(e) => setSelectedProjectId(e.target.value)}
          >
            <option value="">— {t(region, 'project')} —</option>
            {(projects || []).map((p) => (
              <option key={p.project_id} value={p.project_id}>
                {p.project_id} · {p.project_name}
              </option>
            ))}
          </select>
          {fieldQueueCount > 0 && (
            <button type="button" className="field-sync-btn" onClick={handleSyncNow} disabled={syncing || !online}>
              🔄 {t(region, 'pendingSync')} ({fieldQueueCount}) · {t(region, 'syncNow')}
            </button>
          )}
        </div>
        <div className="field-header-user">
          {username ? `${username} · ` : ''}
          <button type="button" className="field-link-btn" onClick={logout}>
            {t(region, 'logout')}
          </button>
        </div>
      </header>

      {panelError && <div className="notice error field-notice">{t(region, 'error')}: {String(panelError)}</div>}

      <main className="field-body">
        {!currentProject && (
          <div className="field-empty">{busy ? `${t(region, 'loading')}…` : `${t(region, 'project')} — ${t(region, 'projectName')}`}</div>
        )}

        {currentProject && tasks.length === 0 && (
          <div className="field-empty">{t(region, 'addTask')}</div>
        )}

        {currentProject &&
          tasks.map((tk) => {
            const p = progressByTask[tk.task_id];
            const pct = p ? Number(p.percent_complete) || 0 : 0;
            const status = taskStatus[tk.task_id];
            const photoCount = (photosByTask[tk.task_id] || []).length;
            return (
              <button
                key={tk.task_id}
                type="button"
                className="field-task-card"
                onClick={() => openSheet(tk)}
              >
                <div className="field-task-card-head">
                  <span className="field-task-id">{tk.task_id}</span>
                  <span className={`badge status-${tk.status}`}>{statusLabel(region, tk.status)}</span>
                </div>
                <div className="field-task-name">{tk.task_name}</div>
                <div className="field-progress-track">
                  <div className="field-progress-fill" style={{ width: `${pct}%` }} />
                </div>
                <div className="field-task-card-foot">
                  <span>{pct}%</span>
                  {photoCount > 0 && <span className="field-photo-chip">📷 {photoCount}</span>}
                  {status === 'synced' && <span className="field-status-chip field-status-synced">✓ {t(region, 'synced')}</span>}
                  {status === 'queued' && <span className="field-status-chip field-status-queued">⏳ {t(region, 'pendingSync')}</span>}
                </div>
              </button>
            );
          })}
      </main>

      {activeTask && (
        <div className="field-sheet-overlay" role="dialog" aria-modal="true" onMouseDown={closeSheet}>
          <div className="field-sheet" onMouseDown={(e) => e.stopPropagation()}>
            <div className="field-sheet-header">
              <h2 className="field-sheet-title">
                {t(region, 'fieldReport')} — {activeTask.task_id}
              </h2>
              <button type="button" className="field-icon-btn" onClick={closeSheet} aria-label={t(region, 'dismiss')}>
                ×
              </button>
            </div>
            <div className="field-sheet-body">
              <div className="field-task-name">{activeTask.task_name}</div>

              <div className="field-field">
                <label>
                  {t(region, 'percentComplete')}: <strong>{draft.percent_complete}%</strong>
                </label>
                <input
                  type="range"
                  min="0"
                  max="100"
                  step={PERCENT_STEP}
                  disabled={!canWrite}
                  value={draft.percent_complete}
                  onChange={(e) => setDraft((d) => ({ ...d, percent_complete: Number(e.target.value) }))}
                />
              </div>

              <div className="field-field">
                <label>{t(region, 'actualCost')}</label>
                <input
                  type="number"
                  min="0"
                  step="any"
                  disabled={!canWrite}
                  value={draft.actual_cost}
                  onChange={(e) => setDraft((d) => ({ ...d, actual_cost: e.target.value }))}
                />
              </div>

              <div className="field-field">
                <label>{t(region, 'note')}</label>
                <textarea
                  rows={3}
                  disabled={!canWrite}
                  value={draft.note}
                  onChange={(e) => setDraft((d) => ({ ...d, note: e.target.value }))}
                  maxLength={500}
                />
              </div>

              {canWrite && (
                <div className="field-field">
                  <label>{t(region, 'photos')}</label>
                  <button type="button" className="field-camera-btn" onClick={() => fileInputRef.current?.click()}>
                    📷 {t(region, 'takePhoto')}
                  </button>
                  <input
                    ref={fileInputRef}
                    type="file"
                    accept="image/*"
                    capture="environment"
                    multiple
                    style={{ display: 'none' }}
                    onChange={handleFileChange}
                  />
                  {files.length > 0 && (
                    <div className="field-file-chips">
                      {files.map((f, i) => (
                        <span key={`${f.name}-${i}`} className="field-file-chip">
                          {f.name}
                          <button type="button" onClick={() => removeFile(i)} aria-label={t(region, 'delete')}>
                            ×
                          </button>
                        </span>
                      ))}
                    </div>
                  )}
                </div>
              )}

              {activePhotos.length > 0 && (
                <div className="field-field">
                  <label>
                    {t(region, 'photos')} ({activePhotos.length})
                  </label>
                  <div className="field-photo-grid">
                    {activePhotos.map((ph) => (
                      <FieldPhotoThumb
                        key={ph.id}
                        photo={ph}
                        canDelete={canWrite}
                        onDelete={() => handleDeletePhoto(activeTask.task_id, ph.id)}
                      />
                    ))}
                  </div>
                </div>
              )}

              {submitError && <div className="notice error field-notice">{submitError}</div>}
            </div>
            <div className="field-sheet-footer">
              <button type="button" className="secondary field-btn-lg" onClick={closeSheet}>
                {t(region, 'cancel')}
              </button>
              {canWrite && (
                <button type="button" className="field-btn-lg field-submit-btn" onClick={handleSubmit} disabled={submitting}>
                  {submitting ? `${t(region, 'loading')}…` : `✓ ${t(region, 'submitReport')}`}
                </button>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

// 單張既有照片縮圖：以驗證過的 fetch 取得 blob 再建立物件 URL（<img src> 無法帶 Bearer）。
// 具名匯出供 ScheduleBoard.jsx 的桌面版照片燈箱共用，避免重複實作驗證下載邏輯。
export function FieldPhotoThumb({ photo, canDelete, onDelete }) {
  const { token, tenantId, region } = useScheduleStore();
  const [objUrl, setObjUrl] = useState('');

  useEffect(() => {
    // Pro Batch F1（demo 模式）：mockApi.js 沒有真實的 /photos/{id} 位元組端點
    // （<img> 無法帶 Bearer，一律驗證下載後才顯示，demo 沒有後端可打），改以
    // data: URI 直接承載圖片內容於 photo.url。此為最小相容改動：photo.url 為
    // data: URI 時直接當 <img src> 使用；正式環境（photo.url 為一般端點路徑）
    // 行為不變，仍以驗證過的 fetch 取得 blob 再建立物件 URL。
    if (typeof photo.url === 'string' && photo.url.startsWith('data:')) {
      setObjUrl(photo.url);
      return undefined;
    }
    let revoked = false;
    let url = '';
    const headers = { 'X-Tenant-Id': tenantId, 'X-Region': region };
    if (token) headers.Authorization = `Bearer ${token}`;
    fetch(photoUrl(photo.id), { headers })
      .then((res) => (res.ok ? res.blob() : Promise.reject(new Error(`HTTP ${res.status}`))))
      .then((blob) => {
        if (revoked) return;
        url = URL.createObjectURL(blob);
        setObjUrl(url);
      })
      .catch(() => {});
    return () => {
      revoked = true;
      if (url) URL.revokeObjectURL(url);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [photo.id, photo.url]);

  return (
    <div className="field-photo-thumb">
      {objUrl ? <img src={objUrl} alt={photo.note || photo.original_name || ''} /> : <div className="field-photo-placeholder" />}
      {canDelete && (
        <button type="button" className="field-photo-delete" onClick={onDelete} aria-label="delete">
          ×
        </button>
      )}
    </div>
  );
}
