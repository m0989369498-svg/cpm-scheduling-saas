import React, { useEffect } from 'react';
import { useScheduleStore, isLoading, getError } from '../store/scheduleStore';
import { t } from '../i18n';

/**
 * CostPanel 資源成本負荷面板（Pro Batch D · Feature 1）
 *
 * 功能：
 *   - 掛載 / 切換專案時 -> store.loadCost()（唯讀，GET /projects/{pid}/cost）
 *   - 彙總卡片：總成本 + 依資源 / 依類別 / 依 WBS 分類小計
 *   - 每任務成本表格（task_id / task_name / duration / cost）
 *   - 成本曲線（逐日 cost + cumulative）簡易表格
 *
 * 資料來源：store.cost（CostResult）
 *   { total_cost, by_resource:{res:total}, by_category:{cat:total}, by_wbs:{code:total},
 *     per_task:[{task_id,task_name,duration,cost,per_resource}], cost_curve:[{day,cost,cumulative}] }
 */

const CATEGORY_KEYS = {
  labor: 'catLabor',
  equipment: 'catEquipment',
  material: 'catMaterial',
  subcontract: 'catSubcontract',
};

function categoryLabel(region, cat) {
  const key = CATEGORY_KEYS[cat];
  if (key) return t(region, key);
  return cat || t(region, 'uncategorized');
}

function formatNum(n) {
  const v = Number(n);
  if (!Number.isFinite(v)) return '0';
  return v.toLocaleString(undefined, { maximumFractionDigits: 2 });
}

function RollupCard({ title, entries, labelFn, uncategorizedLabel }) {
  const list = Object.entries(entries || {});
  return (
    <div style={{ minWidth: '200px' }}>
      <div style={{ fontSize: '12px', color: '#777', marginBottom: '4px' }}>{title}</div>
      {list.length === 0 && <div style={{ fontSize: '12px', color: '#999' }}>—</div>}
      {list.map(([k, v]) => (
        <div
          key={k || 'uncategorized'}
          style={{ fontSize: '13px', display: 'flex', justifyContent: 'space-between', gap: '10px' }}
        >
          <span>{k ? labelFn(k) : uncategorizedLabel}</span>
          <span style={{ fontWeight: 700 }}>{formatNum(v)}</span>
        </div>
      ))}
    </div>
  );
}

export default function CostPanel({ region = 'TW' }) {
  const store = useScheduleStore();
  const { currentProject, cost, loadCost } = store;

  // Batch 4 慣例：本面板僅讀取 cost scope 的載入與錯誤
  const busy = isLoading(store, 'cost');
  const panelError = getError(store, 'cost');

  const projectId = currentProject?.project_id;

  // 掛載 / 切換專案時載入成本負荷
  useEffect(() => {
    if (projectId) {
      loadCost().catch(() => {});
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId]);

  if (!currentProject) {
    return (
      <div style={{ padding: '16px', color: '#999' }}>
        {t(region, 'project')} — {t(region, 'projectName')}
      </div>
    );
  }

  const byResource = (cost && cost.by_resource) || {};
  const byCategory = (cost && cost.by_category) || {};
  const byWbs = (cost && cost.by_wbs) || {};
  const perTask = cost && Array.isArray(cost.per_task) ? cost.per_task : [];
  const curve = cost && Array.isArray(cost.cost_curve) ? cost.cost_curve : [];

  return (
    <div className="panel" style={{ background: '#fff' }}>
      <h3 style={{ marginTop: 0, color: '#2c3e50' }}>{t(region, 'costLoading')}</h3>

      {/* ===== 面板自身 scope 的載入/錯誤 ===== */}
      {busy && <div className="notice loading">{t(region, 'loading')}…</div>}
      {panelError && (
        <div className="notice error">
          {t(region, 'error')}: {String(panelError)}
        </div>
      )}

      {cost && (
        <>
          {/* ===== 總成本 ===== */}
          <div style={{ marginBottom: '16px' }}>
            <div style={{ fontSize: '12px', color: '#777' }}>{t(region, 'totalCost')}</div>
            <div style={{ fontSize: '20px', fontWeight: 700, color: '#2c3e50' }}>
              {formatNum(cost.total_cost)}
            </div>
          </div>

          {/* ===== 彙總卡片：依資源 / 依類別 / 依 WBS ===== */}
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: '24px', marginBottom: '16px' }}>
            <RollupCard
              title={t(region, 'costByResource')}
              entries={byResource}
              labelFn={(k) => k}
              uncategorizedLabel={t(region, 'uncategorized')}
            />
            <RollupCard
              title={t(region, 'costByCategory')}
              entries={byCategory}
              labelFn={(k) => categoryLabel(region, k)}
              uncategorizedLabel={t(region, 'uncategorized')}
            />
            <RollupCard
              title={t(region, 'costByWbs')}
              entries={byWbs}
              labelFn={(k) => k}
              uncategorizedLabel={t(region, 'uncategorized')}
            />
          </div>

          {/* ===== 每任務成本 ===== */}
          <div style={{ marginBottom: '16px' }}>
            <div style={{ fontWeight: 700, fontSize: '13px', marginBottom: '6px', color: '#2c3e50' }}>
              {t(region, 'taskCost')}
            </div>
            <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '13px' }}>
              <thead>
                <tr style={{ background: '#f7f9fc', textAlign: 'left' }}>
                  <th style={cellHead}>{t(region, 'taskId')}</th>
                  <th style={cellHead}>{t(region, 'taskName')}</th>
                  <th style={cellHead}>{t(region, 'duration')}</th>
                  <th style={cellHead}>{t(region, 'taskCost')}</th>
                </tr>
              </thead>
              <tbody>
                {perTask.length === 0 && (
                  <tr>
                    <td style={{ ...cell, textAlign: 'center', color: '#999' }} colSpan={4}>
                      {t(region, 'addTask')}
                    </td>
                  </tr>
                )}
                {perTask.map((row) => (
                  <tr key={row.task_id} style={{ borderBottom: '1px solid #eee' }}>
                    <td style={{ ...cell, fontWeight: 700 }}>{row.task_id}</td>
                    <td style={cell}>{row.task_name}</td>
                    <td style={cell}>{row.duration}</td>
                    <td style={cell}>{formatNum(row.cost)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {/* ===== 成本曲線（逐日 cost + cumulative） ===== */}
          {curve.length > 0 && (
            <div>
              <div style={{ fontWeight: 700, fontSize: '13px', marginBottom: '6px', color: '#2c3e50' }}>
                {t(region, 'costCurve')}
              </div>
              <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '12px' }}>
                <thead>
                  <tr style={{ background: '#f7f9fc', textAlign: 'left' }}>
                    <th style={cellHead}>{t(region, 'day')}</th>
                    <th style={cellHead}>{t(region, 'taskCost')}</th>
                    <th style={cellHead}>{t(region, 'totalCost')}</th>
                  </tr>
                </thead>
                <tbody>
                  {curve.map((pt) => (
                    <tr key={`cc-${pt.day}`} style={{ borderBottom: '1px solid #eee' }}>
                      <td style={cell}>{pt.day}</td>
                      <td style={cell}>{formatNum(pt.cost)}</td>
                      <td style={cell}>{formatNum(pt.cumulative)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
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
