import React, { useEffect, useMemo, useState } from 'react';
import { useScheduleStore, isLoading, getError } from '../store/scheduleStore';
import { t } from '../i18n';
import SCurveChart from './SCurveChart';

/**
 * RiskPanel 蒙地卡羅風險分析面板（Phase 8）
 *
 * 功能：
 *   - 編輯每個任務的三點估計：optimistic(a) / most_likely(m) / pessimistic(b)
 *   - 合約工期 (contract deadline) 輸入
 *   - 「執行模擬」-> store.runSimulation({iterations, deadline})
 *       渲染：<SCurveChart/>（S 曲線 + p50/p90/deadline 標記）
 *             要徑指數 (criticality index) 表
 *             準時完工機率 (on-time probability)；< 70% 以紅色高亮
 *
 * 資料來源：store.risk（list[RiskParam]）、store.simulation（SimulationResult）
 *   RiskParam { task_id, optimistic_duration, most_likely_duration, pessimistic_duration, criticality_index }
 *   SimulationResult { iterations, mean, std, p10, p50, p90, s_curve, criticality, deadline, on_time_probability }
 *     criticality: [{ task_id, index }]
 */

const DEFAULT_ITERATIONS = 1000;
const ON_TIME_THRESHOLD = 0.7; // 準時機率警示門檻（< 70% 高亮）

export default function RiskPanel({ region = 'TW' }) {
  const store = useScheduleStore();
  const {
    currentProject,
    risk,
    simulation,
    loadRisk,
    saveRisk,
    runSimulation,
  } = store;

  // Batch 4：本面板僅讀取 risk / simulation scope 的載入與錯誤
  const busy = isLoading(store, 'risk') || isLoading(store, 'simulation');
  const panelError = getError(store, 'risk') || getError(store, 'simulation');

  // 本地草稿：{ [taskId]: {a, m, b} }
  const [drafts, setDrafts] = useState({});
  const [iterations, setIterations] = useState(DEFAULT_ITERATIONS);
  const [deadline, setDeadline] = useState('');

  const projectId = currentProject?.project_id;
  const tasks = currentProject?.tasks || [];

  // 掛載 / 切換專案時載入三點估計
  useEffect(() => {
    if (projectId) {
      loadRisk().catch(() => {});
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId]);

  // 後端三點估計回傳後同步至草稿
  useEffect(() => {
    const next = {};
    if (Array.isArray(risk)) {
      risk.forEach((r) => {
        if (r && r.task_id != null) {
          next[r.task_id] = {
            a: r.optimistic_duration,
            m: r.most_likely_duration,
            b: r.pessimistic_duration,
          };
        }
      });
    }
    setDrafts(next);
  }, [risk]);

  // 要徑指數查詢表：優先用模擬結果，其次後端 risk 的 criticality_index
  const criticalityMap = useMemo(() => {
    const map = {};
    if (simulation && Array.isArray(simulation.criticality)) {
      simulation.criticality.forEach((c) => {
        if (c && c.task_id != null) map[c.task_id] = Number(c.index);
      });
    } else if (Array.isArray(risk)) {
      risk.forEach((r) => {
        if (r && r.task_id != null) map[r.task_id] = Number(r.criticality_index);
      });
    }
    return map;
  }, [simulation, risk]);

  // ---- 事件處理 ----

  const handleDraftChange = (taskId, field, value) => {
    setDrafts((prev) => ({
      ...prev,
      [taskId]: { ...(prev[taskId] || {}), [field]: value },
    }));
  };

  // 組裝 list[RiskParam]（criticality_index 由後端計算，輸入時忽略）
  const buildRiskList = () =>
    tasks
      .map((tk) => {
        const d = drafts[tk.task_id] || {};
        const a = Number.parseInt(d.a, 10);
        const m = Number.parseInt(d.m, 10);
        const b = Number.parseInt(d.b, 10);
        if (!Number.isFinite(a) || !Number.isFinite(m) || !Number.isFinite(b)) return null;
        return {
          task_id: tk.task_id,
          optimistic_duration: Math.max(0, a),
          most_likely_duration: Math.max(0, m),
          pessimistic_duration: Math.max(0, b),
          criticality_index: 0.0,
        };
      })
      .filter(Boolean);

  const handleSave = async () => {
    try {
      await saveRisk(buildRiskList());
    } catch (e) {
      /* 錯誤已存於 errors.risk */
    }
  };

  const handleRunSimulation = async () => {
    // 先儲存三點估計，再執行模擬，確保引擎使用畫面上的設定
    try {
      await saveRisk(buildRiskList());
      const dl = Number.parseInt(deadline, 10);
      const req = {
        iterations: Math.max(1, Number.parseInt(iterations, 10) || DEFAULT_ITERATIONS),
        deadline: Number.isFinite(dl) ? dl : null,
      };
      await runSimulation(req);
    } catch (e) {
      /* 錯誤已存於 errors.risk / errors.simulation */
    }
  };

  if (!currentProject) {
    return (
      <div style={{ padding: '16px', color: '#999' }}>
        {t(region, 'project')} — {t(region, 'projectName')}
      </div>
    );
  }

  const onTime = simulation && simulation.on_time_probability != null ? Number(simulation.on_time_probability) : null;
  const onTimeLow = onTime != null && onTime < ON_TIME_THRESHOLD;

  return (
    <div className="panel" style={{ background: '#fff' }}>
      <h3 style={{ marginTop: 0, color: '#2c3e50' }}>
        {t(region, 'riskAnalysis')} · {t(region, 'monteCarlo')}
      </h3>

      {/* ===== 面板自身 scope 的載入/錯誤 ===== */}
      {busy && <div className="notice loading">{t(region, 'loading')}…</div>}
      {panelError && (
        <div className="notice error">
          {t(region, 'error')}: {String(panelError)}
        </div>
      )}

      {/* ===== 三點估計表 ===== */}
      <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '13px', marginBottom: '12px' }}>
        <thead>
          <tr style={{ background: '#f7f9fc', textAlign: 'left' }}>
            <th style={cellHead}>{t(region, 'taskId')}</th>
            <th style={cellHead}>{t(region, 'taskName')}</th>
            <th style={cellHead}>{t(region, 'optimistic')}</th>
            <th style={cellHead}>{t(region, 'mostLikely')}</th>
            <th style={cellHead}>{t(region, 'pessimistic')}</th>
            <th style={cellHead}>{t(region, 'criticalityIndex')}</th>
          </tr>
        </thead>
        <tbody>
          {tasks.length === 0 && (
            <tr>
              <td style={{ ...cell, textAlign: 'center', color: '#999' }} colSpan={6}>
                {t(region, 'addTask')}
              </td>
            </tr>
          )}
          {tasks.map((tk) => {
            const d = drafts[tk.task_id] || {};
            const ci = criticalityMap[tk.task_id];
            return (
              <tr key={tk.task_id} style={{ borderBottom: '1px solid #eee' }}>
                <td style={{ ...cell, fontWeight: 700 }}>{tk.task_id}</td>
                <td style={cell}>{tk.task_name}</td>
                <td style={cell}>
                  <input
                    type="number"
                    min="0"
                    style={{ width: '70px' }}
                    value={d.a ?? ''}
                    onChange={(e) => handleDraftChange(tk.task_id, 'a', e.target.value)}
                  />
                </td>
                <td style={cell}>
                  <input
                    type="number"
                    min="0"
                    style={{ width: '70px' }}
                    value={d.m ?? ''}
                    onChange={(e) => handleDraftChange(tk.task_id, 'm', e.target.value)}
                  />
                </td>
                <td style={cell}>
                  <input
                    type="number"
                    min="0"
                    style={{ width: '70px' }}
                    value={d.b ?? ''}
                    onChange={(e) => handleDraftChange(tk.task_id, 'b', e.target.value)}
                  />
                </td>
                <td style={cell}>
                  {ci != null && Number.isFinite(ci) ? (
                    <span
                      className="badge"
                      style={{
                        background: ci >= 0.999 ? 'var(--color-critical)' : '#ecf0f1',
                        color: ci >= 0.999 ? '#fff' : '#2c3e50',
                      }}
                    >
                      {(ci * 100).toFixed(0)}%
                    </span>
                  ) : (
                    <span style={{ color: '#bbb' }}>—</span>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>

      {/* ===== 動作列：合約工期 / 迭代次數 / 執行 ===== */}
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: '12px', alignItems: 'flex-end', marginBottom: '12px' }}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: '3px' }}>
          <label style={{ fontSize: '11px', color: '#777' }}>{t(region, 'contractDeadline')}</label>
          <input
            type="number"
            min="0"
            style={{ width: '110px' }}
            value={deadline}
            onChange={(e) => setDeadline(e.target.value)}
            placeholder={t(region, 'days')}
          />
        </div>
        <div style={{ display: 'flex', flexDirection: 'column', gap: '3px' }}>
          <label style={{ fontSize: '11px', color: '#777' }}>{t(region, 'monteCarlo')}</label>
          <input
            type="number"
            min="1"
            style={{ width: '110px' }}
            value={iterations}
            onChange={(e) => setIterations(e.target.value)}
          />
        </div>
        <button onClick={handleSave} disabled={busy} className="secondary">
          {t(region, 'save')}
        </button>
        <button onClick={handleRunSimulation} disabled={busy} style={{ background: '#8e44ad', borderColor: '#8e44ad' }}>
          {t(region, 'runSimulation')}
        </button>
      </div>

      {/* ===== 模擬結果 ===== */}
      {simulation && (
        <div>
          {/* 完工機率摘要 */}
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: '20px', marginBottom: '12px' }}>
            <Metric label="P10" value={`${simulation.p10} ${t(region, 'days')}`} />
            <Metric label="P50" value={`${simulation.p50} ${t(region, 'days')}`} />
            <Metric label="P90" value={`${simulation.p90} ${t(region, 'days')}`} />
            <Metric
              label={`${t(region, 'completionProbability')} (μ)`}
              value={`${Number(simulation.mean).toFixed(1)} ${t(region, 'days')}`}
            />
            {onTime != null && (
              <div>
                <div style={{ fontSize: '12px', color: '#777' }}>{t(region, 'onTimeProbability')}</div>
                <div
                  style={{
                    fontSize: '18px',
                    fontWeight: 700,
                    color: onTimeLow ? '#c0392b' : '#27ae60',
                    background: onTimeLow ? '#fdecea' : 'transparent',
                    padding: onTimeLow ? '2px 8px' : '2px 0',
                    borderRadius: '4px',
                  }}
                  title={onTimeLow ? `< ${Math.round(ON_TIME_THRESHOLD * 100)}% · ${t(region, 'riskProvision')}` : undefined}
                >
                  {(onTime * 100).toFixed(1)}%
                  {onTimeLow ? ` ⚠ ${t(region, 'riskProvision')}` : ''}
                </div>
              </div>
            )}
          </div>

          {/* S 曲線 */}
          <div style={{ marginBottom: '12px' }}>
            <div style={{ fontWeight: 700, fontSize: '13px', marginBottom: '6px', color: '#2c3e50' }}>
              {t(region, 'sCurve')}
            </div>
            <SCurveChart
              sCurve={simulation.s_curve}
              p50={simulation.p50}
              p90={simulation.p90}
              deadline={simulation.deadline}
              region={region}
            />
          </div>

          {/* 要徑指數表 */}
          {Array.isArray(simulation.criticality) && simulation.criticality.length > 0 && (
            <div>
              <div style={{ fontWeight: 700, fontSize: '13px', marginBottom: '6px', color: '#2c3e50' }}>
                {t(region, 'criticalityIndex')}
              </div>
              <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '13px' }}>
                <thead>
                  <tr style={{ background: '#f7f9fc', textAlign: 'left' }}>
                    <th style={cellHead}>{t(region, 'taskId')}</th>
                    <th style={cellHead}>{t(region, 'criticalityIndex')}</th>
                  </tr>
                </thead>
                <tbody>
                  {[...simulation.criticality]
                    .sort((a, b) => Number(b.index) - Number(a.index))
                    .map((c) => {
                      const idx = Number(c.index);
                      const high = idx >= 0.999;
                      return (
                        <tr key={c.task_id} style={{ borderBottom: '1px solid #eee' }}>
                          <td style={{ ...cell, fontWeight: 700, color: high ? '#e74c3c' : '#2c3e50' }}>
                            {c.task_id}
                          </td>
                          <td style={cell}>
                            <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                              <div
                                style={{
                                  flex: '0 0 120px',
                                  height: '8px',
                                  background: '#eef1f4',
                                  borderRadius: '4px',
                                  overflow: 'hidden',
                                }}
                              >
                                <div
                                  style={{
                                    width: `${Math.max(0, Math.min(1, idx)) * 100}%`,
                                    height: '100%',
                                    background: high ? '#e74c3c' : '#2980b9',
                                  }}
                                />
                              </div>
                              <span>{(idx * 100).toFixed(0)}%</span>
                            </div>
                          </td>
                        </tr>
                      );
                    })}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function Metric({ label, value }) {
  return (
    <div>
      <div style={{ fontSize: '12px', color: '#777' }}>{label}</div>
      <div style={{ fontSize: '16px', fontWeight: 700, color: '#2c3e50' }}>{value}</div>
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
