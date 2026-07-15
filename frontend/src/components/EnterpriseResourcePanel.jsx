import React, { useEffect, useMemo, useState } from 'react';
import { useScheduleStore, isLoading, getError } from '../store/scheduleStore';
import { t } from '../i18n';

/**
 * EnterpriseResourcePanel — 企業（租戶層級）資源池 + 投資組合資源分配面板（Pro Batch E · Feature 1）
 *
 * 兩個區塊：
 *   (a) 資源池編輯器：tenant_resources 整批取代 upsert（resource_type / name / category /
 *       capacity / unit_cost / work_days 7 碼 0/1 核取方塊）。editor+ 可編輯，viewer 唯讀。
 *   (b) 資源分配熱區圖：橫列 = allocation.resources（每資源），縱欄 = allocation.weeks（ISO 週）,
 *       儲存格 = by_week[week]（該資源該週的尖峰逐日需求；缺值留空），超過 capacity（over_weeks）
 *       者紅底標示；另有 peak（該資源全期尖峰）與 capacity 欄。下方列出 unscheduled_projects
 *       （無 start_date 但仍有資源需求的專案）與 warnings。
 *
 * 資料來源：store.pool（list[TenantResource]）、store.allocation（ResourceAllocationResult）
 *   TenantResource { resource_type, name, category, capacity, unit_cost, work_days }
 *   ResourceAllocationResult { weeks:[string], resources:[{resource_type,name,category,capacity,
 *     unit_cost,by_week:{week:int},peak:int,over_weeks:[string]}], unscheduled_projects:[string],
 *     warnings:[string] }
 *
 * 掛載時 -> loadPool() + loadAllocation()（租戶層級；均為唯讀 GET 之外，PUT /resources/pool 需 editor+）。
 */

// Pro Batch D 既有類別（FROZEN，與後端 schema 一致；此處重用同一集合）
const CATEGORIES = ['labor', 'equipment', 'material', 'subcontract'];
const CATEGORY_LABEL_KEYS = {
  labor: 'catLabor',
  equipment: 'catEquipment',
  material: 'catMaterial',
  subcontract: 'catSubcontract',
};

// 週一~週日字元標籤（work_days 為 7 碼 0/1 字串，索引 0=週一）
const WEEKDAY_CHARS = ['一', '二', '三', '四', '五', '六', '日'];
const DEFAULT_WORK_DAYS = '1111100';

let rowSeq = 0;
function nextKey() {
  rowSeq += 1;
  return `pool-row-${rowSeq}`;
}

function rowFromResource(r) {
  return {
    _key: nextKey(),
    resource_type: (r && r.resource_type) || '',
    name: (r && r.name) || '',
    category: (r && r.category) || 'labor',
    capacity: r && Number.isFinite(Number(r.capacity)) ? Number(r.capacity) : 0,
    unit_cost: r && Number.isFinite(Number(r.unit_cost)) ? Number(r.unit_cost) : 0,
    work_days: (r && r.work_days) || DEFAULT_WORK_DAYS,
  };
}

export default function EnterpriseResourcePanel({ region = 'TW' }) {
  const store = useScheduleStore();
  const { pool, allocation, role, loadPool, savePool, loadAllocation } = store;

  const poolBusy = isLoading(store, 'pool');
  const poolError = getError(store, 'pool');
  const allocBusy = isLoading(store, 'allocation');
  const allocError = getError(store, 'allocation');

  const canWrite = (role || 'admin') !== 'viewer';

  // 本地草稿列（含前端專用 _key，儲存時剝除）
  const [rows, setRows] = useState([]);

  // 掛載時載入資源池 + 投資組合分配
  useEffect(() => {
    loadPool().catch(() => {});
    loadAllocation().catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 後端資源池回傳後同步至草稿
  useEffect(() => {
    if (Array.isArray(pool)) {
      setRows(pool.map(rowFromResource));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pool]);

  const updateRow = (key, patch) => {
    setRows((prev) => prev.map((r) => (r._key === key ? { ...r, ...patch } : r)));
  };

  const addRow = () => setRows((prev) => [...prev, rowFromResource(null)]);

  const removeRow = (key) => {
    setRows((prev) => prev.filter((r) => r._key !== key));
  };

  const toggleWorkDay = (key, dayIdx) => {
    setRows((prev) =>
      prev.map((r) => {
        if (r._key !== key) return r;
        const chars = (r.work_days || DEFAULT_WORK_DAYS).split('');
        chars[dayIdx] = chars[dayIdx] === '1' ? '0' : '1';
        return { ...r, work_days: chars.join('') };
      }),
    );
  };

  // 組裝送出清單：僅濾除尚未填 resource_type 的空白列；其餘驗證交由後端
  const buildPayload = () =>
    rows
      .filter((r) => r.resource_type.trim())
      .map((r) => ({
        resource_type: r.resource_type.trim(),
        name: r.name.trim(),
        category: r.category || 'labor',
        capacity: Math.max(0, Number.parseInt(r.capacity, 10) || 0),
        unit_cost: Math.max(0, Number.parseFloat(r.unit_cost) || 0),
        work_days: /^[01]{7}$/.test(r.work_days || '') ? r.work_days : DEFAULT_WORK_DAYS,
      }));

  const handleSave = async () => {
    try {
      await savePool(buildPayload());
      loadAllocation().catch(() => {});
    } catch (e) {
      /* 錯誤已存於 errors.pool */
    }
  };

  const resourceLabel = (r) => {
    const label = t(region, r);
    return label && label !== r ? label : r;
  };

  const weeks = (allocation && Array.isArray(allocation.weeks) && allocation.weeks) || [];
  const allocResources =
    (allocation && Array.isArray(allocation.resources) && allocation.resources) || [];
  const unscheduled =
    (allocation && Array.isArray(allocation.unscheduled_projects) && allocation.unscheduled_projects) || [];
  const warnings = (allocation && Array.isArray(allocation.warnings) && allocation.warnings) || [];

  return (
    <div className="panel" style={{ background: '#fff' }}>
      <h3 style={{ marginTop: 0, color: '#2c3e50' }}>{t(region, 'enterpriseResources')}</h3>

      {/* ===== (a) 資源池編輯器 ===== */}
      <div style={{ marginBottom: '24px' }}>
        <div style={{ fontWeight: 700, fontSize: '13px', marginBottom: '6px', color: '#2c3e50' }}>
          {t(region, 'resourcePool')}
        </div>

        {poolBusy && <div className="notice loading">{t(region, 'loading')}…</div>}
        {poolError && (
          <div className="notice error">
            {t(region, 'error')}: {String(poolError)}
          </div>
        )}

        <div style={{ overflowX: 'auto' }}>
          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '13px', marginBottom: '12px' }}>
            <thead>
              <tr style={{ background: '#f7f9fc', textAlign: 'left' }}>
                <th style={cellHead}>{t(region, 'resources')}</th>
                <th style={cellHead}>{t(region, 'resourceName')}</th>
                <th style={cellHead}>{t(region, 'category')}</th>
                <th style={cellHead}>{t(region, 'capacity')}</th>
                <th style={cellHead}>{t(region, 'unitCost')}</th>
                {WEEKDAY_CHARS.map((w, i) => (
                  <th key={`wd-h-${i}`} style={{ ...cellHead, textAlign: 'center' }}>
                    {w}
                  </th>
                ))}
                {canWrite && <th style={cellHead} />}
              </tr>
            </thead>
            <tbody>
              {rows.length === 0 && (
                <tr>
                  <td style={{ ...cell, textAlign: 'center', color: '#999' }} colSpan={canWrite ? 13 : 12}>
                    {t(region, 'none')}
                  </td>
                </tr>
              )}
              {rows.map((r) => (
                <tr key={r._key} style={{ borderBottom: '1px solid #eee' }}>
                  <td style={cell}>
                    <input
                      type="text"
                      readOnly={!canWrite}
                      style={{ width: '110px', ...(canWrite ? {} : { background: '#f1f1f1' }) }}
                      value={r.resource_type}
                      onChange={(e) => updateRow(r._key, { resource_type: e.target.value })}
                      placeholder="crane"
                    />
                  </td>
                  <td style={cell}>
                    <input
                      type="text"
                      readOnly={!canWrite}
                      style={{ width: '140px', ...(canWrite ? {} : { background: '#f1f1f1' }) }}
                      value={r.name}
                      onChange={(e) => updateRow(r._key, { name: e.target.value })}
                    />
                  </td>
                  <td style={cell}>
                    <select
                      disabled={!canWrite}
                      value={r.category}
                      onChange={(e) => updateRow(r._key, { category: e.target.value })}
                    >
                      {CATEGORIES.map((c) => (
                        <option key={c} value={c}>
                          {t(region, CATEGORY_LABEL_KEYS[c])}
                        </option>
                      ))}
                    </select>
                  </td>
                  <td style={cell}>
                    <input
                      type="number"
                      min="0"
                      readOnly={!canWrite}
                      style={{ width: '70px', ...(canWrite ? {} : { background: '#f1f1f1' }) }}
                      value={r.capacity}
                      onChange={(e) => updateRow(r._key, { capacity: e.target.value })}
                    />
                  </td>
                  <td style={cell}>
                    <input
                      type="number"
                      min="0"
                      step="0.01"
                      readOnly={!canWrite}
                      style={{ width: '90px', ...(canWrite ? {} : { background: '#f1f1f1' }) }}
                      value={r.unit_cost}
                      onChange={(e) => updateRow(r._key, { unit_cost: e.target.value })}
                    />
                  </td>
                  {WEEKDAY_CHARS.map((_, i) => (
                    <td key={`${r._key}-wd-${i}`} style={{ ...cell, textAlign: 'center' }}>
                      <input
                        type="checkbox"
                        disabled={!canWrite}
                        checked={(r.work_days || DEFAULT_WORK_DAYS)[i] === '1'}
                        onChange={() => toggleWorkDay(r._key, i)}
                      />
                    </td>
                  ))}
                  {canWrite && (
                    <td style={cell}>
                      <button
                        type="button"
                        className="small danger"
                        onClick={() => removeRow(r._key)}
                        title={t(region, 'removeRow')}
                      >
                        {t(region, 'removeRow')}
                      </button>
                    </td>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        {canWrite && (
          <div style={{ display: 'flex', gap: '8px' }}>
            <button type="button" className="secondary" onClick={addRow}>
              + {t(region, 'addResource')}
            </button>
            <button type="button" onClick={handleSave} disabled={poolBusy}>
              {t(region, 'save')}
            </button>
          </div>
        )}
      </div>

      {/* ===== (b) 資源分配熱區圖 ===== */}
      <div>
        <div style={{ fontWeight: 700, fontSize: '13px', marginBottom: '6px', color: '#2c3e50' }}>
          {t(region, 'resourceAllocation')}
        </div>

        {allocBusy && <div className="notice loading">{t(region, 'loading')}…</div>}
        {allocError && (
          <div className="notice error">
            {t(region, 'error')}: {String(allocError)}
          </div>
        )}

        <div style={{ overflowX: 'auto' }}>
          <table style={{ borderCollapse: 'collapse', fontSize: '12px', marginBottom: '12px' }}>
            <thead>
              <tr style={{ background: '#f7f9fc', textAlign: 'left' }}>
                <th style={cellHead}>{t(region, 'resources')}</th>
                <th style={cellHead}>{t(region, 'capacity')}</th>
                <th style={cellHead}>{t(region, 'peakDemand')}</th>
                {weeks.map((w) => {
                  // ISO 年週標籤 "2026-W10" -> 「第 10 週」;完整標籤放 title 供查對。
                  const m = /^(\d{4})-W(\d+)$/.exec(w);
                  const label = m
                    ? `第 ${parseInt(m[2], 10)} ${t(region, 'week')}`
                    : w.replace(/^\d{4}-/, '');
                  return (
                    <th
                      key={`week-h-${w}`}
                      style={{ ...cellHead, textAlign: 'center', whiteSpace: 'nowrap' }}
                      title={w}
                    >
                      {label}
                    </th>
                  );
                })}
              </tr>
            </thead>
            <tbody>
              {allocResources.length === 0 && (
                <tr>
                  <td style={{ ...cell, textAlign: 'center', color: '#999' }} colSpan={3 + weeks.length}>
                    {t(region, 'none')}
                  </td>
                </tr>
              )}
              {allocResources.map((res) => {
                const overSet = new Set(Array.isArray(res.over_weeks) ? res.over_weeks : []);
                return (
                  <tr key={res.resource_type} style={{ borderBottom: '1px solid #eee' }}>
                    <td style={{ ...cell, fontWeight: 700 }}>
                      {res.name && res.name !== res.resource_type ? res.name : resourceLabel(res.resource_type)}
                    </td>
                    <td style={cell}>{res.capacity}</td>
                    <td style={cell}>{res.peak}</td>
                    {weeks.map((w) => {
                      const v = res.by_week ? res.by_week[w] : undefined;
                      const over = overSet.has(w);
                      return (
                        <td
                          key={`${res.resource_type}-${w}`}
                          style={{
                            ...cell,
                            textAlign: 'center',
                            background: over ? 'rgba(231, 76, 60, 0.15)' : 'transparent',
                            color: over ? '#c0392b' : '#2c3e50',
                            fontWeight: over ? 700 : 400,
                          }}
                          title={over ? t(region, 'overAllocated') : undefined}
                        >
                          {v == null ? '' : v}
                        </td>
                      );
                    })}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        {/* 週欄位說明(回應「W10/W11 是什麼」的現場疑問) */}
        {weeks.length > 0 && (
          <div style={{ fontSize: '11px', color: '#8a93a0', marginBottom: '12px' }}>
            ⓘ {t(region, 'weekLegend')}
          </div>
        )}

        {unscheduled.length > 0 && (
          <div style={{ fontSize: '12px', color: '#b9770e', marginBottom: '6px' }}>
            <strong>{t(region, 'unscheduledProjects')}:</strong> {unscheduled.join(', ')}
          </div>
        )}

        {warnings.length > 0 && (
          <div style={{ fontSize: '12px', color: '#b9770e' }}>
            <ul style={{ margin: '4px 0 0', paddingLeft: '18px' }}>
              {warnings.map((w, i) => (
                <li key={i}>{String(w)}</li>
              ))}
            </ul>
          </div>
        )}
      </div>
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
