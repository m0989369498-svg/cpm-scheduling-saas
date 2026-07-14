import React, { useEffect } from 'react';
import { useScheduleStore, isLoading, getError } from '../store/scheduleStore';
import { t } from '../i18n';

/**
 * HealthPanel DCMA 14 點排程健康度評估面板（Pro Batch D · Feature 2）
 *
 * 功能：
 *   - 掛載 / 切換專案時 -> store.loadHealth()（唯讀，GET /projects/{pid}/health）
 *   - 整體健康度分數（passed_count/applicable_count）
 *   - 14 列檢查表格：名稱（i18n dcma_<key>）/ 數值 / 門檻值 / 通過/未通過/不適用徽章
 *       綠 = 通過, 紅 = 未通過, 灰 = 不適用（value/passed 為 null，資訊性，不列入計分）
 *
 * 資料來源：store.health（DcmaReport）
 *   { data_date, checks:[{key,name,name_cn,value,threshold,comparison,count,total,passed,detail}],
 *     score, passed_count, applicable_count, total_count }
 */

function badgeStyle(passed) {
  if (passed === true) {
    return { background: '#eafaf1', color: '#1e8449', border: '1px solid #a9dfbf' };
  }
  if (passed === false) {
    return { background: '#fdecea', color: '#c0392b', border: '1px solid #f5c6cb' };
  }
  return { background: '#f4f4f4', color: '#777', border: '1px solid #ddd' };
}

function badgeLabel(region, passed) {
  if (passed === true) return t(region, 'checkPass');
  if (passed === false) return t(region, 'checkFail');
  return t(region, 'checkNa');
}

function formatVal(v) {
  if (v == null) return '—';
  const n = Number(v);
  if (!Number.isFinite(n)) return String(v);
  return Number.isInteger(n) ? String(n) : n.toFixed(3);
}

export default function HealthPanel({ region = 'TW' }) {
  const store = useScheduleStore();
  const { currentProject, health, loadHealth } = store;

  // Batch 4 慣例：本面板僅讀取 health scope 的載入與錯誤
  const busy = isLoading(store, 'health');
  const panelError = getError(store, 'health');

  const projectId = currentProject?.project_id;

  // 掛載 / 切換專案時載入 DCMA 健康度評估（使用後端預設 data_date = 專案總工期）
  useEffect(() => {
    if (projectId) {
      loadHealth().catch(() => {});
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

  const checks = health && Array.isArray(health.checks) ? health.checks : [];
  const scorePct = health ? Math.round((Number(health.score) || 0) * 100) : null;

  return (
    <div className="panel" style={{ background: '#fff' }}>
      <h3 style={{ marginTop: 0, color: '#2c3e50' }}>{t(region, 'dcmaHealth')}</h3>

      {/* ===== 面板自身 scope 的載入/錯誤 ===== */}
      {busy && <div className="notice loading">{t(region, 'loading')}…</div>}
      {panelError && (
        <div className="notice error">
          {t(region, 'error')}: {String(panelError)}
        </div>
      )}

      {health && (
        <>
          {/* ===== 整體健康度分數 ===== */}
          <div style={{ marginBottom: '16px' }}>
            <div style={{ fontSize: '12px', color: '#777' }}>{t(region, 'healthScore')}</div>
            <div style={{ fontSize: '22px', fontWeight: 700, color: '#2c3e50' }}>
              {scorePct}% ({health.passed_count}/{health.applicable_count})
            </div>
          </div>

          {/* ===== 14 點檢查表格 ===== */}
          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '13px' }}>
            <thead>
              <tr style={{ background: '#f7f9fc', textAlign: 'left' }}>
                <th style={cellHead} />
                <th style={cellHead}>{t(region, 'metricValue')}</th>
                <th style={cellHead}>{t(region, 'threshold')}</th>
                <th style={cellHead}>{t(region, 'status')}</th>
              </tr>
            </thead>
            <tbody>
              {checks.length === 0 && (
                <tr>
                  <td style={{ ...cell, textAlign: 'center', color: '#999' }} colSpan={4}>
                    {t(region, 'loading')}…
                  </td>
                </tr>
              )}
              {checks.map((c) => (
                <tr key={c.key} style={{ borderBottom: '1px solid #eee' }}>
                  <td style={{ ...cell, fontWeight: 700 }}>{t(region, `dcma_${c.key}`)}</td>
                  <td style={cell}>{formatVal(c.value)}</td>
                  <td style={cell}>{formatVal(c.threshold)}</td>
                  <td style={cell}>
                    <span
                      style={{
                        ...badgeStyle(c.passed),
                        padding: '2px 8px',
                        borderRadius: '4px',
                        fontSize: '12px',
                        fontWeight: 700,
                        display: 'inline-block',
                      }}
                    >
                      {badgeLabel(region, c.passed)}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
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
