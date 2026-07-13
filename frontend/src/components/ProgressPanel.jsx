import React, { useEffect, useMemo, useState } from 'react';
import { useScheduleStore, isLoading, getError } from '../store/scheduleStore';
import { t } from '../i18n';
import EvmChart from './EvmChart';

/**
 * ProgressPanel 進度追蹤 + 實獲值管理 (EVM) 面板（Phase 9）
 *
 * 功能：
 *   - 每任務進度編輯表：budget / percent_complete(0-100) / actual_cost /
 *       actual_start_day / actual_finish_day -> store.saveProgress(list)
 *   - 「建立基準線」按鈕（prompt 命名）-> store.createBaseline(name?)（以目前 CPM + 進度預算
 *       為快照；後端將此新基準線設為使用中並清除其餘旗標）
 *   - Pro Batch B Feature 3：基準線選擇器（下拉清單：名稱 + 日期，★ 標示使用中）+
 *       「設為使用中」-> store.activateBaseline(id) / 「刪除」-> store.deleteBaseline(id)
 *       （甘特圖計畫條 / EVM 一律依循「使用中」基準線，見 store.baseline）
 *   - 資料日 (data_date) 輸入 + 滑桿（0..基準線/專案總工期）
 *   - 「計算實獲值」-> store.runEvm(dataDate)
 *       渲染：EVM KPI 卡（SPI/CPI/SV/CV/EAC/VAC，紅 <0.9 或負值 / 綠）
 *             BAC/PV/EV/AC、<EvmChart/>、以及拋轉風險預警按鈕
 *   - 「拋轉風險預警」-> store.dispatchEvmAlert(dataDate)（僅 risk_flagged 時啟用）
 *
 * 資料來源：
 *   store.progress（list[ProgressEntry]）、store.baseline（使用中基準線 BaselineOut）、
 *   store.baselines（list[{id,name,created_at,is_active,project_duration}] 選單）、
 *   store.evm（EvmResult）、store.dataDate
 */

// SPI/CPI 風險門檻（< 0.9 視為不利，紅色高亮；對齊後端 risk_flagged 規則）
const PERF_THRESHOLD = 0.9;

export default function ProgressPanel({ region = 'TW' }) {
  const store = useScheduleStore();
  const {
    currentProject,
    progress,
    baseline,
    baselines,
    evm,
    dataDate,
    loadProgress,
    saveProgress,
    createBaseline,
    loadBaseline,
    loadBaselines,
    activateBaseline,
    deleteBaseline,
    runEvm,
    dispatchEvmAlert,
  } = store;

  // Batch 4：本面板讀取 progress / evm / baselines(Pro Batch B) scope 的載入與錯誤
  const busy = isLoading(store, 'progress') || isLoading(store, 'evm') || isLoading(store, 'baselines');
  const panelError =
    getError(store, 'progress') || getError(store, 'evm') || getError(store, 'baselines');

  // 本地草稿：{ [taskId]: {budget, percent_complete, actual_cost, actual_start_day, actual_finish_day} }
  const [drafts, setDrafts] = useState({});
  // 資料日輸入（字串以同步 input/slider）；空字串表示沿用後端預設（基準線總工期）
  const [ddInput, setDdInput] = useState('');
  // 拋轉預警結果提示
  const [alertMsg, setAlertMsg] = useState('');
  // Pro Batch B Feature 3：基準線選擇器目前選取的 id（字串，對應 <select> value）
  const [selectedBaselineId, setSelectedBaselineId] = useState('');

  const projectId = currentProject?.project_id;
  const tasks = currentProject?.tasks || [];

  // 滑桿上界：基準線總工期，其次專案總工期，至少 1
  const maxDay = useMemo(() => {
    const b = baseline && Number.isFinite(Number(baseline.project_duration))
      ? Number(baseline.project_duration)
      : null;
    const p = Number.isFinite(Number(currentProject?.project_duration))
      ? Number(currentProject.project_duration)
      : null;
    return Math.max(1, b ?? p ?? 1);
  }, [baseline, currentProject]);

  // 掛載 / 切換專案時載入進度、使用中基準線 + 基準線選單（Pro Batch B Feature 3）
  useEffect(() => {
    if (projectId) {
      loadProgress().catch(() => {});
      loadBaseline().catch(() => {});
      loadBaselines().catch(() => {});
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId]);

  // 基準線選單載入/變動後，預設選取使用中者（無旗標則第一筆，向後相容舊資料）
  useEffect(() => {
    if (Array.isArray(baselines) && baselines.length > 0) {
      const active = baselines.find((b) => b && b.is_active) || baselines[0];
      setSelectedBaselineId(String(active.id));
    } else {
      setSelectedBaselineId('');
    }
  }, [baselines]);

  // 後端進度回傳後同步至草稿
  useEffect(() => {
    const next = {};
    if (Array.isArray(progress)) {
      progress.forEach((p) => {
        if (p && p.task_id != null) {
          next[p.task_id] = {
            budget: p.budget,
            percent_complete: p.percent_complete,
            actual_cost: p.actual_cost,
            actual_start_day: p.actual_start_day,
            actual_finish_day: p.actual_finish_day,
          };
        }
      });
    }
    setDrafts(next);
  }, [progress]);

  // 同步 store.dataDate -> 本地輸入（首次/重算後）
  useEffect(() => {
    if (dataDate != null) setDdInput(String(dataDate));
  }, [dataDate]);

  // 進度查詢表（task_id -> percent_complete），供甘特圖填色（此處未用，但維持一致）
  // ---- 事件處理 ----

  const handleDraftChange = (taskId, field, value) => {
    setDrafts((prev) => ({
      ...prev,
      [taskId]: { ...(prev[taskId] || {}), [field]: value },
    }));
  };

  // 解析整數，超出範圍夾在 [lo, hi]；空字串/非數字回傳 fallback（預設 null）
  const parseIntClamp = (v, lo, hi, fallback = null) => {
    if (v === '' || v == null) return fallback;
    const n = Number.parseInt(v, 10);
    if (!Number.isFinite(n)) return fallback;
    let r = n;
    if (lo != null) r = Math.max(lo, r);
    if (hi != null) r = Math.min(hi, r);
    return r;
  };

  const parseFloatNonNeg = (v) => {
    if (v === '' || v == null) return 0;
    const n = Number.parseFloat(v);
    return Number.isFinite(n) ? Math.max(0, n) : 0;
  };

  // 組裝 list[ProgressEntry]
  const buildProgressList = () =>
    tasks.map((tk) => {
      const d = drafts[tk.task_id] || {};
      return {
        task_id: tk.task_id,
        budget: parseFloatNonNeg(d.budget),
        percent_complete: parseIntClamp(d.percent_complete, 0, 100, 0),
        actual_cost: parseFloatNonNeg(d.actual_cost),
        actual_start_day: parseIntClamp(d.actual_start_day, 0, null, null),
        actual_finish_day: parseIntClamp(d.actual_finish_day, 0, null, null),
      };
    });

  const handleSave = async () => {
    setAlertMsg('');
    try {
      await saveProgress(buildProgressList());
    } catch (e) {
      /* 錯誤已存於 errors.progress */
    }
  };

  // 建立基準線（Pro Batch B Feature 3：prompt 命名，可留白使用後端預設名稱）：
  // 先儲存進度（確保預算落地），再建立基準線快照（後端設為使用中）。
  const handleCreateBaseline = async () => {
    setAlertMsg('');
    // eslint-disable-next-line no-alert
    const name = window.prompt(t(region, 'baselineName'), '');
    if (name === null) return; // 使用者取消
    try {
      await saveProgress(buildProgressList());
      await createBaseline(name.trim() || undefined);
    } catch (e) {
      /* 錯誤已存於 errors.progress */
    }
  };

  // Pro Batch B Feature 3：將選取的基準線設為使用中（清除其餘旗標）
  const handleActivateBaseline = async () => {
    if (!selectedBaselineId) return;
    setAlertMsg('');
    try {
      await activateBaseline(Number(selectedBaselineId));
    } catch (e) {
      /* 錯誤已存於 errors.baselines */
    }
  };

  // Pro Batch B Feature 3：刪除選取的基準線（若為使用中，後端自動將最新剩餘者設為使用中）
  const handleDeleteBaseline = async () => {
    if (!selectedBaselineId) return;
    // eslint-disable-next-line no-alert
    if (!window.confirm(`${t(region, 'deleteBaseline')}?`)) return;
    setAlertMsg('');
    try {
      await deleteBaseline(Number(selectedBaselineId));
    } catch (e) {
      /* 錯誤已存於 errors.baselines */
    }
  };

  // 計算 EVM：先儲存進度，再以資料日計算
  const handleComputeEvm = async () => {
    setAlertMsg('');
    try {
      await saveProgress(buildProgressList());
      const dd = ddInput === '' ? null : Number.parseInt(ddInput, 10);
      await runEvm(Number.isFinite(dd) ? dd : null);
    } catch (e) {
      /* 錯誤已存於 errors.progress / errors.evm */
    }
  };

  const handleDispatchAlert = async () => {
    setAlertMsg('');
    try {
      const dd = ddInput === '' ? null : Number.parseInt(ddInput, 10);
      const res = await dispatchEvmAlert(Number.isFinite(dd) ? dd : null);
      if (res) {
        setAlertMsg(
          res.dispatched
            ? `${t(region, 'dispatchAlert')} ✓ ${t(region, 'riskProvision')}`
            : `${t(region, 'dispatchAlert')} — ${t(region, 'none')}`,
        );
      }
    } catch (e) {
      /* 錯誤已存於 errors.evm */
    }
  };

  if (!currentProject) {
    return (
      <div style={{ padding: '16px', color: '#999' }}>
        {t(region, 'project')} — {t(region, 'projectName')}
      </div>
    );
  }

  const riskFlagged = Boolean(evm && evm.risk_flagged);

  return (
    <div className="panel" style={{ background: '#fff' }}>
      <h3 style={{ marginTop: 0, color: '#2c3e50' }}>
        {t(region, 'progress')} · {t(region, 'evm')}
      </h3>

      {/* ===== 面板自身 scope 的載入/錯誤 ===== */}
      {busy && <div className="notice loading">{t(region, 'loading')}…</div>}
      {panelError && (
        <div className="notice error">
          {t(region, 'error')}: {String(panelError)}
        </div>
      )}

      {/* ===== 基準線狀態列 ===== */}
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: '12px', alignItems: 'center', marginBottom: '12px' }}>
        <span style={{ fontSize: '13px', color: '#555' }}>
          {t(region, 'activeBaseline')}:{' '}
          {baseline ? (
            <strong style={{ color: '#2c3e50' }}>
              {baseline.name || 'baseline'} · {baseline.project_duration} {t(region, 'days')}
            </strong>
          ) : (
            <span style={{ color: '#bbb' }}>{t(region, 'none')}</span>
          )}
        </span>
        <button onClick={handleCreateBaseline} disabled={busy} className="secondary">
          {t(region, 'createBaseline')}
        </button>

        {/* Pro Batch B Feature 3：基準線選擇器（清單 + 設為使用中 + 刪除；★ 標示使用中） */}
        {Array.isArray(baselines) && baselines.length > 0 && (
          <div style={{ display: 'flex', gap: '6px', alignItems: 'center' }}>
            <label style={{ fontSize: '11px', color: '#777' }}>{t(region, 'baselines')}</label>
            <select
              value={selectedBaselineId}
              onChange={(e) => setSelectedBaselineId(e.target.value)}
              style={{ minWidth: '200px' }}
            >
              {baselines.map((b) => (
                <option key={b.id} value={b.id}>
                  {b.is_active ? '★ ' : ''}
                  {b.name || 'baseline'} · {b.created_at ? String(b.created_at).slice(0, 10) : ''} ·{' '}
                  {b.project_duration} {t(region, 'days')}
                </option>
              ))}
            </select>
            <button onClick={handleActivateBaseline} disabled={busy} className="secondary">
              {t(region, 'setActive')}
            </button>
            <button onClick={handleDeleteBaseline} disabled={busy} className="danger">
              {t(region, 'deleteBaseline')}
            </button>
          </div>
        )}
      </div>

      {/* ===== 進度編輯表 ===== */}
      <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '13px', marginBottom: '12px' }}>
        <thead>
          <tr style={{ background: '#f7f9fc', textAlign: 'left' }}>
            <th style={cellHead}>{t(region, 'taskId')}</th>
            <th style={cellHead}>{t(region, 'taskName')}</th>
            <th style={cellHead}>{t(region, 'budget')}</th>
            <th style={cellHead}>{t(region, 'percentComplete')}</th>
            <th style={cellHead}>{t(region, 'actualCost')}</th>
            <th style={cellHead}>{t(region, 'actualStartDay')}</th>
            <th style={cellHead}>{t(region, 'actualFinishDay')}</th>
          </tr>
        </thead>
        <tbody>
          {tasks.length === 0 && (
            <tr>
              <td style={{ ...cell, textAlign: 'center', color: '#999' }} colSpan={7}>
                {t(region, 'addTask')}
              </td>
            </tr>
          )}
          {tasks.map((tk) => {
            const d = drafts[tk.task_id] || {};
            return (
              <tr key={tk.task_id} style={{ borderBottom: '1px solid #eee' }}>
                <td style={{ ...cell, fontWeight: 700 }}>{tk.task_id}</td>
                <td style={cell}>{tk.task_name}</td>
                <td style={cell}>
                  <input
                    type="number"
                    min="0"
                    step="any"
                    style={{ width: '100px' }}
                    value={d.budget ?? ''}
                    onChange={(e) => handleDraftChange(tk.task_id, 'budget', e.target.value)}
                  />
                </td>
                <td style={cell}>
                  <input
                    type="number"
                    min="0"
                    max="100"
                    style={{ width: '70px' }}
                    value={d.percent_complete ?? ''}
                    onChange={(e) => handleDraftChange(tk.task_id, 'percent_complete', e.target.value)}
                  />
                </td>
                <td style={cell}>
                  <input
                    type="number"
                    min="0"
                    step="any"
                    style={{ width: '100px' }}
                    value={d.actual_cost ?? ''}
                    onChange={(e) => handleDraftChange(tk.task_id, 'actual_cost', e.target.value)}
                  />
                </td>
                <td style={cell}>
                  <input
                    type="number"
                    min="0"
                    style={{ width: '70px' }}
                    value={d.actual_start_day ?? ''}
                    onChange={(e) => handleDraftChange(tk.task_id, 'actual_start_day', e.target.value)}
                  />
                </td>
                <td style={cell}>
                  <input
                    type="number"
                    min="0"
                    style={{ width: '70px' }}
                    value={d.actual_finish_day ?? ''}
                    onChange={(e) => handleDraftChange(tk.task_id, 'actual_finish_day', e.target.value)}
                  />
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>

      {/* ===== 動作列：資料日 + 滑桿 + 儲存 / 計算 ===== */}
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: '14px', alignItems: 'flex-end', marginBottom: '12px' }}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: '3px' }}>
          <label style={{ fontSize: '11px', color: '#777' }}>{t(region, 'dataDate')}</label>
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
            <input
              type="number"
              min="0"
              max={maxDay}
              style={{ width: '90px' }}
              value={ddInput}
              onChange={(e) => setDdInput(e.target.value)}
              placeholder={String(maxDay)}
            />
            <input
              type="range"
              min="0"
              max={maxDay}
              step="1"
              style={{ width: '180px' }}
              value={ddInput === '' ? maxDay : Math.min(maxDay, Math.max(0, Number(ddInput) || 0))}
              onChange={(e) => setDdInput(e.target.value)}
            />
            <span style={{ fontSize: '12px', color: '#999' }}>
              0–{maxDay} {t(region, 'days')}
            </span>
          </div>
        </div>
        <button onClick={handleSave} disabled={busy} className="secondary">
          {t(region, 'save')}
        </button>
        <button
          onClick={handleComputeEvm}
          disabled={busy}
          style={{ background: '#16a085', borderColor: '#16a085' }}
        >
          {t(region, 'computeEvm')}
        </button>
      </div>

      {/* ===== EVM 結果 ===== */}
      {evm && (
        <div>
          {/* 風險旗標橫幅（進度落後 / 成本超支） */}
          {riskFlagged && (
            <div
              style={{
                padding: '8px 12px',
                marginBottom: '12px',
                background: '#fdecea',
                color: '#c0392b',
                border: '1px solid #f5c6cb',
                borderRadius: '4px',
                fontWeight: 700,
              }}
            >
              ⚠ {evm.spi != null && evm.spi < PERF_THRESHOLD ? t(region, 'behindSchedule') : ''}
              {evm.spi != null && evm.spi < PERF_THRESHOLD && evm.cpi != null && evm.cpi < PERF_THRESHOLD ? ' · ' : ''}
              {evm.cpi != null && evm.cpi < PERF_THRESHOLD ? t(region, 'overBudget') : ''}
            </div>
          )}

          {/* BAC / PV / EV / AC 摘要 */}
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: '20px', marginBottom: '12px' }}>
            <Metric label={t(region, 'bac')} value={fmt(evm.bac)} />
            <Metric label={t(region, 'pv')} value={fmt(evm.pv)} />
            <Metric label={t(region, 'ev')} value={fmt(evm.ev)} color="#27ae60" />
            <Metric label={t(region, 'ac')} value={fmt(evm.ac)} color="#e67e22" />
          </div>

          {/* KPI 卡：SPI / CPI / SV / CV / EAC / VAC（紅 <0.9 或負 / 綠） */}
          <div
            style={{
              display: 'grid',
              gridTemplateColumns: 'repeat(auto-fill, minmax(150px, 1fr))',
              gap: '10px',
              marginBottom: '16px',
            }}
          >
            <KpiCard label={t(region, 'spi')} value={fmtRatio(evm.spi)} bad={isBadRatio(evm.spi)} />
            <KpiCard label={t(region, 'cpi')} value={fmtRatio(evm.cpi)} bad={isBadRatio(evm.cpi)} />
            <KpiCard label={t(region, 'scheduleVariance')} value={fmt(evm.sv)} bad={isBadVariance(evm.sv)} />
            <KpiCard label={t(region, 'costVariance')} value={fmt(evm.cv)} bad={isBadVariance(evm.cv)} />
            <KpiCard label={t(region, 'eac')} value={fmt(evm.eac)} bad={evm.eac != null && evm.bac != null && evm.eac > evm.bac} />
            <KpiCard label={t(region, 'vac')} value={fmt(evm.vac)} bad={isBadVariance(evm.vac)} />
            <KpiCard label={t(region, 'etc')} value={fmt(evm.etc)} />
            <KpiCard label={t(region, 'tcpi')} value={fmtRatio(evm.tcpi)} bad={evm.tcpi != null && evm.tcpi > 1} />
          </div>

          {/* EVM 圖（PV 曲線 + 資料日 + EV/AC） */}
          <div style={{ marginBottom: '12px' }}>
            <div style={{ fontWeight: 700, fontSize: '13px', marginBottom: '6px', color: '#2c3e50' }}>
              {t(region, 'plannedVsActual')}
            </div>
            <EvmChart
              pvCurve={evm.pv_curve}
              ev={evm.ev}
              ac={evm.ac}
              dataDate={evm.data_date}
              projectDuration={baseline ? baseline.project_duration : currentProject.project_duration}
              region={region}
            />
          </div>

          {/* 拋轉風險預警 */}
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: '12px', alignItems: 'center' }}>
            <button
              onClick={handleDispatchAlert}
              disabled={busy || !riskFlagged}
              className="danger"
              title={riskFlagged ? t(region, 'dispatchAlert') : t(region, 'none')}
            >
              {t(region, 'dispatchAlert')}
            </button>
            {alertMsg && <span style={{ fontSize: '13px', color: '#2c3e50' }}>{alertMsg}</span>}
          </div>

          {/* 每任務 PV/EV/AC 明細 */}
          {Array.isArray(evm.per_task) && evm.per_task.length > 0 && (
            <div style={{ marginTop: '16px' }}>
              <div style={{ fontWeight: 700, fontSize: '13px', marginBottom: '6px', color: '#2c3e50' }}>
                {t(region, 'task')} · {t(region, 'evm')}
              </div>
              <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '13px' }}>
                <thead>
                  <tr style={{ background: '#f7f9fc', textAlign: 'left' }}>
                    <th style={cellHead}>{t(region, 'taskId')}</th>
                    <th style={cellHead}>{t(region, 'budget')}</th>
                    <th style={cellHead}>{t(region, 'plannedPct')}</th>
                    <th style={cellHead}>{t(region, 'percentComplete')}</th>
                    <th style={cellHead}>PV</th>
                    <th style={cellHead}>EV</th>
                    <th style={cellHead}>AC</th>
                  </tr>
                </thead>
                <tbody>
                  {evm.per_task.map((pt) => {
                    const behind = Number(pt.percent_complete) < Number(pt.planned_pct);
                    return (
                      <tr key={pt.task_id} style={{ borderBottom: '1px solid #eee' }}>
                        <td style={{ ...cell, fontWeight: 700 }}>{pt.task_id}</td>
                        <td style={cell}>{fmt(pt.budget)}</td>
                        <td style={cell}>{pt.planned_pct}%</td>
                        <td style={{ ...cell, color: behind ? '#c0392b' : '#27ae60', fontWeight: behind ? 700 : 400 }}>
                          {pt.percent_complete}%
                        </td>
                        <td style={cell}>{fmt(pt.pv)}</td>
                        <td style={cell}>{fmt(pt.ev)}</td>
                        <td style={cell}>{fmt(pt.ac)}</td>
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

// 金額格式化（千分位）；null/undefined -> '—'
function fmt(v) {
  if (v == null || !Number.isFinite(Number(v))) return '—';
  return Number(v).toLocaleString(undefined, { maximumFractionDigits: 0 });
}

// 比值格式化（SPI/CPI/TCPI）；null -> '—'
function fmtRatio(v) {
  if (v == null || !Number.isFinite(Number(v))) return '—';
  return Number(v).toFixed(3);
}

// SPI/CPI 不利判定：< 0.9
function isBadRatio(v) {
  return v != null && Number.isFinite(Number(v)) && Number(v) < PERF_THRESHOLD;
}

// 差異 (SV/CV/VAC) 不利判定：負值
function isBadVariance(v) {
  return v != null && Number.isFinite(Number(v)) && Number(v) < 0;
}

function Metric({ label, value, color }) {
  return (
    <div>
      <div style={{ fontSize: '12px', color: '#777' }}>{label}</div>
      <div style={{ fontSize: '16px', fontWeight: 700, color: color || '#2c3e50' }}>{value}</div>
    </div>
  );
}

// EVM KPI 卡：bad 為真時紅底紅字，否則綠字
function KpiCard({ label, value, bad }) {
  return (
    <div
      style={{
        border: `1px solid ${bad ? '#f5c6cb' : '#e0e3e8'}`,
        background: bad ? '#fdecea' : '#fff',
        borderRadius: '6px',
        padding: '10px 12px',
      }}
    >
      <div style={{ fontSize: '11px', color: '#777', marginBottom: '4px' }}>{label}</div>
      <div style={{ fontSize: '18px', fontWeight: 700, color: bad ? '#c0392b' : '#27ae60' }}>
        {value}
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
