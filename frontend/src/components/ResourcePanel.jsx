import React, { useEffect, useMemo, useState } from 'react';
import { useScheduleStore, isLoading, getError } from '../store/scheduleStore';
import { t } from '../i18n';

/**
 * ResourcePanel 資源撫平面板（Phase 8）
 *
 * 功能：
 *   - 編輯每種資源上限 (resource limit)：crane（吊車）/ manpower（人力）+ 任何既有資源型別
 *   - 編輯每個任務的資源需求 (resource demand)
 *   - 「儲存資源設定」-> store.saveResources(cfg)
 *   - 「執行資源撫平」-> store.runLeveling()
 *       顯示：撫平後工期 vs 原工期、工期展延警示（extended 時）、逐日資源載荷時間軸
 *
 * 資料來源：store.resources（ResourceConfig）、store.leveling（LevelingResult）
 *   ResourceConfig { limits:[{resource_type, max_capacity}], demands:{ [taskId]: {res: qty} } }
 *   LevelingResult { original_duration, leveled_duration, extended, tasks, timeline, over_capacity_days, unresolved }
 *     timeline: [{ day:int, loads:{res:int}, over:bool }]
 */

// 預設資源型別（與 seed/i18n 對齊）
const DEFAULT_RESOURCE_TYPES = ['crane', 'manpower'];

export default function ResourcePanel({ region = 'TW' }) {
  const store = useScheduleStore();
  const {
    currentProject,
    resources,
    leveling,
    loadResources,
    saveResources,
    runLeveling,
  } = store;

  // Batch 4：本面板僅讀取 resources / leveling scope 的載入與錯誤
  const busy = isLoading(store, 'resources') || isLoading(store, 'leveling');
  const panelError = getError(store, 'resources') || getError(store, 'leveling');

  // 本地草稿：limits {res: capacity}、demands {taskId: {res: qty}}
  const [limitDrafts, setLimitDrafts] = useState({});
  const [demandDrafts, setDemandDrafts] = useState({});

  const projectId = currentProject?.project_id;
  const tasks = currentProject?.tasks || [];

  // 掛載 / 切換專案時載入資源設定
  useEffect(() => {
    if (projectId) {
      loadResources().catch(() => {});
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId]);

  // 後端資源設定回傳後，同步至草稿
  useEffect(() => {
    const limits = {};
    if (resources && Array.isArray(resources.limits)) {
      resources.limits.forEach((l) => {
        if (l && l.resource_type != null) limits[l.resource_type] = l.max_capacity;
      });
    }
    const demands = {};
    if (resources && resources.demands && typeof resources.demands === 'object') {
      Object.entries(resources.demands).forEach(([tid, d]) => {
        demands[tid] = { ...(d || {}) };
      });
    }
    setLimitDrafts(limits);
    setDemandDrafts(demands);
  }, [resources]);

  // 顯示用的資源型別清單：預設 + 既有設定中出現的型別（去重，保序）
  const resourceTypes = useMemo(() => {
    const set = [];
    const push = (r) => {
      if (r != null && !set.includes(r)) set.push(r);
    };
    DEFAULT_RESOURCE_TYPES.forEach(push);
    Object.keys(limitDrafts).forEach(push);
    Object.values(demandDrafts).forEach((d) => Object.keys(d || {}).forEach(push));
    return set;
  }, [limitDrafts, demandDrafts]);

  // 資源型別 i18n 標籤（crane/manpower 有專屬鍵，其餘原樣顯示）
  const resourceLabel = (r) => {
    const label = t(region, r);
    return label && label !== r ? label : r;
  };

  // ---- 事件處理 ----

  const handleLimitChange = (res, value) => {
    setLimitDrafts((prev) => ({ ...prev, [res]: value }));
  };

  const handleDemandChange = (taskId, res, value) => {
    setDemandDrafts((prev) => ({
      ...prev,
      [taskId]: { ...(prev[taskId] || {}), [res]: value },
    }));
  };

  // 組裝 ResourceConfig 並儲存（限量轉整數；需求為 0/空者略過）
  const buildConfig = () => {
    const limits = Object.entries(limitDrafts)
      .filter(([, v]) => v !== '' && v != null)
      .map(([resource_type, v]) => ({
        resource_type,
        max_capacity: Math.max(0, Number.parseInt(v, 10) || 0),
      }));
    const demands = {};
    Object.entries(demandDrafts).forEach(([tid, d]) => {
      const entry = {};
      Object.entries(d || {}).forEach(([res, v]) => {
        const n = Number.parseInt(v, 10);
        if (Number.isFinite(n) && n > 0) entry[res] = n;
      });
      if (Object.keys(entry).length > 0) demands[tid] = entry;
    });
    return { limits, demands };
  };

  const handleSave = async () => {
    try {
      await saveResources(buildConfig());
    } catch (e) {
      /* 錯誤已存於 errors.resources */
    }
  };

  const handleRunLeveling = async () => {
    // 先儲存最新草稿，再執行撫平，確保引擎使用畫面上的設定
    try {
      await saveResources(buildConfig());
      await runLeveling();
    } catch (e) {
      /* 錯誤已存於 errors.resources / errors.leveling */
    }
  };

  if (!currentProject) {
    return (
      <div style={{ padding: '16px', color: '#999' }}>
        {t(region, 'project')} — {t(region, 'projectName')}
      </div>
    );
  }

  const extended = Boolean(leveling && leveling.extended);

  return (
    <div className="panel" style={{ background: '#fff' }}>
      <h3 style={{ marginTop: 0, color: '#2c3e50' }}>{t(region, 'resourceLeveling')}</h3>

      {/* ===== 面板自身 scope 的載入/錯誤 ===== */}
      {busy && <div className="notice loading">{t(region, 'loading')}…</div>}
      {panelError && (
        <div className="notice error">
          {t(region, 'error')}: {String(panelError)}
        </div>
      )}

      {/* ===== 資源上限 ===== */}
      <div style={{ marginBottom: '16px' }}>
        <div style={{ fontWeight: 700, fontSize: '13px', marginBottom: '6px', color: '#2c3e50' }}>
          {t(region, 'resourceLimit')}
        </div>
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: '12px' }}>
          {resourceTypes.map((res) => (
            <div key={`lim-${res}`} style={{ display: 'flex', flexDirection: 'column', gap: '3px' }}>
              <label style={{ fontSize: '11px', color: '#777' }}>{resourceLabel(res)}</label>
              <input
                type="number"
                min="0"
                style={{ width: '90px' }}
                value={limitDrafts[res] ?? ''}
                onChange={(e) => handleLimitChange(res, e.target.value)}
              />
            </div>
          ))}
        </div>
      </div>

      {/* ===== 各任務資源需求 ===== */}
      <div style={{ marginBottom: '16px' }}>
        <div style={{ fontWeight: 700, fontSize: '13px', marginBottom: '6px', color: '#2c3e50' }}>
          {t(region, 'resourceDemand')}
        </div>
        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '13px' }}>
          <thead>
            <tr style={{ background: '#f7f9fc', textAlign: 'left' }}>
              <th style={cellHead}>{t(region, 'taskId')}</th>
              <th style={cellHead}>{t(region, 'taskName')}</th>
              {resourceTypes.map((res) => (
                <th key={`h-${res}`} style={cellHead}>
                  {resourceLabel(res)}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {tasks.length === 0 && (
              <tr>
                <td style={{ ...cell, textAlign: 'center', color: '#999' }} colSpan={2 + resourceTypes.length}>
                  {t(region, 'addTask')}
                </td>
              </tr>
            )}
            {tasks.map((tk) => (
              <tr key={tk.task_id} style={{ borderBottom: '1px solid #eee' }}>
                <td style={{ ...cell, fontWeight: 700 }}>{tk.task_id}</td>
                <td style={cell}>{tk.task_name}</td>
                {resourceTypes.map((res) => (
                  <td key={`${tk.task_id}-${res}`} style={cell}>
                    <input
                      type="number"
                      min="0"
                      style={{ width: '70px' }}
                      value={(demandDrafts[tk.task_id] && demandDrafts[tk.task_id][res]) ?? ''}
                      onChange={(e) => handleDemandChange(tk.task_id, res, e.target.value)}
                    />
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* ===== 動作列 ===== */}
      <div style={{ display: 'flex', gap: '8px', marginBottom: '12px' }}>
        <button onClick={handleSave} disabled={busy} className="secondary">
          {t(region, 'save')}
        </button>
        <button onClick={handleRunLeveling} disabled={busy} style={{ background: '#16a085', borderColor: '#16a085' }}>
          {t(region, 'runLeveling')}
        </button>
      </div>

      {/* ===== 撫平結果 ===== */}
      {leveling && (
        <div style={{ marginTop: '8px' }}>
          {/* 工期摘要 */}
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: '24px', marginBottom: '12px' }}>
            <div>
              <div style={{ fontSize: '12px', color: '#777' }}>{t(region, 'projectDuration')}</div>
              <div style={{ fontSize: '16px', fontWeight: 700 }}>
                {leveling.original_duration} {t(region, 'days')}
              </div>
            </div>
            <div>
              <div style={{ fontSize: '12px', color: '#777' }}>{t(region, 'resourceLeveling')}</div>
              <div style={{ fontSize: '16px', fontWeight: 700, color: extended ? '#c0392b' : '#27ae60' }}>
                {leveling.leveled_duration} {t(region, 'days')}
              </div>
            </div>
          </div>

          {/* 工期展延警示 */}
          {extended && (
            <div
              style={{
                padding: '8px 12px',
                background: '#fdecea',
                border: '1px solid #f5c6cb',
                borderRadius: '4px',
                color: '#c0392b',
                fontWeight: 700,
                marginBottom: '12px',
              }}
            >
              ⚠ {t(region, 'scheduleExtended')}：{leveling.original_duration} →{' '}
              {leveling.leveled_duration} {t(region, 'days')}
              {Array.isArray(leveling.unresolved) && leveling.unresolved.length > 0 && (
                <span style={{ fontWeight: 400 }}>
                  {' '}
                  · {t(region, 'overCapacity')}: {leveling.unresolved.join(', ')}
                </span>
              )}
            </div>
          )}

          {/* 逐日資源載荷時間軸 */}
          {Array.isArray(leveling.timeline) && leveling.timeline.length > 0 && (
            <div>
              <div style={{ fontWeight: 700, fontSize: '13px', marginBottom: '6px', color: '#2c3e50' }}>
                {t(region, 'resourceDemand')} · {t(region, 'days')}
              </div>
              <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '12px' }}>
                <thead>
                  <tr style={{ background: '#f7f9fc', textAlign: 'left' }}>
                    <th style={cellHead}>{t(region, 'day')}</th>
                    {resourceTypes.map((res) => (
                      <th key={`tl-h-${res}`} style={cellHead}>
                        {resourceLabel(res)}
                      </th>
                    ))}
                    <th style={cellHead}>{t(region, 'overCapacity')}</th>
                  </tr>
                </thead>
                <tbody>
                  {leveling.timeline.map((row) => (
                    <tr
                      key={`tl-${row.day}`}
                      style={{
                        borderBottom: '1px solid #eee',
                        background: row.over ? 'rgba(231, 76, 60, 0.10)' : 'transparent',
                      }}
                    >
                      <td style={cell}>{row.day}</td>
                      {resourceTypes.map((res) => {
                        const v = row.loads ? row.loads[res] : undefined;
                        const lim = limitDrafts[res];
                        const over = v != null && lim !== '' && lim != null && Number(v) > Number(lim);
                        return (
                          <td
                            key={`tl-${row.day}-${res}`}
                            style={{ ...cell, color: over ? '#c0392b' : '#2c3e50', fontWeight: over ? 700 : 400 }}
                          >
                            {v ?? 0}
                          </td>
                        );
                      })}
                      <td style={cell}>{row.over ? '⚠' : ''}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

const cellHead = {
  padding: '6px 10px',
  borderBottom: '2px solid #ddd',
  fontSize: '12px',
  color: '#555',
};
const cell = {
  padding: '5px 10px',
  verticalAlign: 'middle',
};
