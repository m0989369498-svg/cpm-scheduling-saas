import React, { useEffect, useMemo, useState } from 'react';
import { useScheduleStore, isLoading, getError } from '../store/scheduleStore';
import { t } from '../i18n';

/**
 * ResourcePanel 資源撫平面板（Phase 8；Pro Batch D 擴充：成本費率/類別 + 每資源行事曆）
 *
 * 功能：
 *   - 編輯每種資源上限 (resource limit)：crane（吊車）/ manpower（人力）+ 任何既有資源型別
 *   - Pro Batch D Feature 1：每資源單位成本 (unit_cost) + 類別 (category：labor/equipment/material/subcontract)
 *   - Pro Batch D Feature 3：每資源行事曆（7 個工作日核取方塊，週一~週日；與專案行事曆並行套用於撫平）
 *   - 編輯每個任務的資源需求 (resource demand)
 *   - 「儲存資源設定」-> store.saveResources(cfg)
 *   - 「執行資源撫平」-> store.runLeveling()
 *       顯示：撫平後工期 vs 原工期、工期展延警示（extended 時）、逐日資源載荷時間軸
 *
 * 資料來源：store.resources（ResourceConfig）、store.leveling（LevelingResult）
 *   ResourceConfig { limits:[{resource_type, max_capacity, unit_cost, category}],
 *                     demands:{ [taskId]: {res: qty} }, calendars:[{resource_type, work_days}] }
 *   LevelingResult { original_duration, leveled_duration, extended, tasks, timeline, over_capacity_days, unresolved }
 *     timeline: [{ day:int, loads:{res:int}, over:bool }]
 */

// 預設資源型別（與 seed/i18n 對齊）
const DEFAULT_RESOURCE_TYPES = ['crane', 'manpower'];

// Pro Batch D Feature 1：資源類別（FROZEN，與後端 schema 一致）
const CATEGORIES = ['labor', 'equipment', 'material', 'subcontract'];
const CATEGORY_LABEL_KEYS = {
  labor: 'catLabor',
  equipment: 'catEquipment',
  material: 'catMaterial',
  subcontract: 'catSubcontract',
};

// Pro Batch D Feature 3：週一~週日字元標籤（TW/CN 通用；work_days 為 7 碼 0/1 字串，索引 0=週一）
const WEEKDAY_CHARS = ['一', '二', '三', '四', '五', '六', '日'];
const DEFAULT_WORK_DAYS = '1111110';

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
  // Pro Batch D Feature 1：每資源成本草稿 {res: {unit_cost, category}}
  const [costDrafts, setCostDrafts] = useState({});
  // Pro Batch D Feature 3：每資源行事曆草稿 {res: work_days(7碼 0/1 字串)}
  const [calendarDrafts, setCalendarDrafts] = useState({});

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
    const costs = {};
    if (resources && Array.isArray(resources.limits)) {
      resources.limits.forEach((l) => {
        if (l && l.resource_type != null) {
          limits[l.resource_type] = l.max_capacity;
          costs[l.resource_type] = {
            unit_cost: l.unit_cost ?? 0,
            category: l.category || 'labor',
          };
        }
      });
    }
    const demands = {};
    if (resources && resources.demands && typeof resources.demands === 'object') {
      Object.entries(resources.demands).forEach(([tid, d]) => {
        demands[tid] = { ...(d || {}) };
      });
    }
    // Pro Batch D Feature 3：每資源行事曆
    const calendars = {};
    if (resources && Array.isArray(resources.calendars)) {
      resources.calendars.forEach((c) => {
        if (c && c.resource_type != null) calendars[c.resource_type] = c.work_days || DEFAULT_WORK_DAYS;
      });
    }
    setLimitDrafts(limits);
    setCostDrafts(costs);
    setDemandDrafts(demands);
    setCalendarDrafts(calendars);
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
    Object.keys(calendarDrafts).forEach(push);
    return set;
  }, [limitDrafts, demandDrafts, calendarDrafts]);

  // 資源型別 i18n 標籤（crane/manpower 有專屬鍵，其餘原樣顯示）
  const resourceLabel = (r) => {
    const label = t(region, r);
    return label && label !== r ? label : r;
  };

  // ---- 事件處理 ----

  const handleLimitChange = (res, value) => {
    setLimitDrafts((prev) => ({ ...prev, [res]: value }));
  };

  // Pro Batch D Feature 1：單位成本 / 類別草稿變更
  const handleCostChange = (res, field, value) => {
    setCostDrafts((prev) => ({
      ...prev,
      [res]: { unit_cost: 0, category: 'labor', ...(prev[res] || {}), [field]: value },
    }));
  };

  const handleDemandChange = (taskId, res, value) => {
    setDemandDrafts((prev) => ({
      ...prev,
      [taskId]: { ...(prev[taskId] || {}), [res]: value },
    }));
  };

  // Pro Batch D Feature 3：切換某資源第 dayIdx 天（0=週一...6=週日）的工作日旗標
  const handleCalendarToggle = (res, dayIdx) => {
    setCalendarDrafts((prev) => {
      const current = prev[res] || DEFAULT_WORK_DAYS;
      const chars = current.split('');
      chars[dayIdx] = chars[dayIdx] === '1' ? '0' : '1';
      return { ...prev, [res]: chars.join('') };
    });
  };

  // 組裝 ResourceConfig 並儲存（限量轉整數；需求為 0/空者略過；
  // Pro Batch D：limits 併入 unit_cost/category，calendars 依已載入/已編輯的資源送出）
  const buildConfig = () => {
    const limits = Object.entries(limitDrafts)
      .filter(([, v]) => v !== '' && v != null)
      .map(([resource_type, v]) => {
        const c = costDrafts[resource_type] || {};
        const unitCostN = Number.parseFloat(c.unit_cost);
        return {
          resource_type,
          max_capacity: Math.max(0, Number.parseInt(v, 10) || 0),
          unit_cost: Number.isFinite(unitCostN) ? Math.max(0, unitCostN) : 0,
          category: c.category || 'labor',
        };
      });
    const demands = {};
    Object.entries(demandDrafts).forEach(([tid, d]) => {
      const entry = {};
      Object.entries(d || {}).forEach(([res, v]) => {
        const n = Number.parseInt(v, 10);
        if (Number.isFinite(n) && n > 0) entry[res] = n;
      });
      if (Object.keys(entry).length > 0) demands[tid] = entry;
    });
    const calendars = Object.entries(calendarDrafts)
      .filter(([, wd]) => typeof wd === 'string' && /^[01]{7}$/.test(wd))
      .map(([resource_type, work_days]) => ({ resource_type, work_days }));
    return { limits, demands, calendars };
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

      {/* ===== 資源上限 + Pro Batch D：單位成本 / 類別 ===== */}
      <div style={{ marginBottom: '16px' }}>
        <div style={{ fontWeight: 700, fontSize: '13px', marginBottom: '6px', color: '#2c3e50' }}>
          {t(region, 'resourceLimit')}
        </div>
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: '16px' }}>
          {resourceTypes.map((res) => (
            <div
              key={`lim-${res}`}
              style={{
                display: 'flex',
                flexDirection: 'column',
                gap: '3px',
                padding: '8px 10px',
                border: '1px solid #eee',
                borderRadius: '4px',
              }}
            >
              <label style={{ fontSize: '12px', fontWeight: 700, color: '#2c3e50' }}>{resourceLabel(res)}</label>
              <div style={{ display: 'flex', flexDirection: 'column', gap: '2px' }}>
                <label style={{ fontSize: '11px', color: '#777' }}>{t(region, 'resourceLimit')}</label>
                <input
                  type="number"
                  min="0"
                  style={{ width: '110px' }}
                  value={limitDrafts[res] ?? ''}
                  onChange={(e) => handleLimitChange(res, e.target.value)}
                />
              </div>
              <div style={{ display: 'flex', flexDirection: 'column', gap: '2px' }}>
                <label style={{ fontSize: '11px', color: '#777' }}>{t(region, 'unitCost')}</label>
                <input
                  type="number"
                  min="0"
                  step="0.01"
                  style={{ width: '110px' }}
                  value={(costDrafts[res] && costDrafts[res].unit_cost) ?? 0}
                  onChange={(e) => handleCostChange(res, 'unit_cost', e.target.value)}
                />
              </div>
              <div style={{ display: 'flex', flexDirection: 'column', gap: '2px' }}>
                <label style={{ fontSize: '11px', color: '#777' }}>{t(region, 'category')}</label>
                <select
                  style={{ width: '110px' }}
                  value={(costDrafts[res] && costDrafts[res].category) || 'labor'}
                  onChange={(e) => handleCostChange(res, 'category', e.target.value)}
                >
                  {CATEGORIES.map((c) => (
                    <option key={c} value={c}>
                      {t(region, CATEGORY_LABEL_KEYS[c])}
                    </option>
                  ))}
                </select>
              </div>
            </div>
          ))}
        </div>
      </div>

      {/* ===== Pro Batch D Feature 3：每資源行事曆（週一~週日核取方塊） ===== */}
      <div style={{ marginBottom: '16px' }}>
        <div style={{ fontWeight: 700, fontSize: '13px', marginBottom: '6px', color: '#2c3e50' }}>
          {t(region, 'resourceCalendar')}
        </div>
        <table style={{ borderCollapse: 'collapse', fontSize: '12px' }}>
          <thead>
            <tr style={{ background: '#f7f9fc', textAlign: 'left' }}>
              <th style={cellHead}>{t(region, 'resourceWorkDays')}</th>
              {WEEKDAY_CHARS.map((w, i) => (
                <th key={`wd-h-${i}`} style={{ ...cellHead, textAlign: 'center' }}>
                  {w}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {resourceTypes.map((res) => {
              const wd = calendarDrafts[res] || DEFAULT_WORK_DAYS;
              return (
                <tr key={`cal-${res}`} style={{ borderBottom: '1px solid #eee' }}>
                  <td style={{ ...cell, fontWeight: 700 }}>{resourceLabel(res)}</td>
                  {WEEKDAY_CHARS.map((_, i) => (
                    <td key={`cal-${res}-${i}`} style={{ ...cell, textAlign: 'center' }}>
                      <input
                        type="checkbox"
                        checked={wd[i] === '1'}
                        onChange={() => handleCalendarToggle(res, i)}
                      />
                    </td>
                  ))}
                </tr>
              );
            })}
          </tbody>
        </table>
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
