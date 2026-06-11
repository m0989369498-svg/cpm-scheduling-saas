import React, { useEffect } from 'react'
import { useScheduleStore, isLoading, getError } from '../store/scheduleStore'
import { t } from '../i18n'

/**
 * DashboardView 投資組合儀表板（Phase 10）
 *
 * 以 store.dashboard.projects 渲染本租戶各專案的 KPI 卡片：
 *   - 專案工期 / 要徑任務數
 *   - SPI / CPI（有基準線時；紅綠標示 <1 落後/超支）
 *   - 待處理風險事件徽章
 * 點擊卡片 -> loadProject(project_id) 並透過 onOpenProject 切回排程板。
 *
 * 唯讀（viewer 亦可檢視）；require_role 由後端 GET /dashboard 不設限。
 */
export default function DashboardView({ region, onOpenProject }) {
  const store = useScheduleStore()
  const { dashboard, loadDashboard, loadProject } = store

  // Batch 4：本視圖僅讀取 dashboard scope 的載入與錯誤
  const dashLoading = isLoading(store, 'dashboard')
  const dashError = getError(store, 'dashboard')

  // 掛載時載入儀表板彙總
  useEffect(() => {
    loadDashboard().catch(() => {})
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const projects = (dashboard && Array.isArray(dashboard.projects) && dashboard.projects) || []
  const totals = (dashboard && dashboard.totals) || null

  const handleOpen = async (pid) => {
    try {
      await loadProject(pid)
      if (typeof onOpenProject === 'function') onOpenProject(pid)
    } catch (e) {
      /* 錯誤已存於 errors.project（排程板的狀態列會顯示） */
    }
  }

  // SPI/CPI 顏色：>=1 綠、<1 紅、無資料灰
  const indexColor = (v) => {
    if (v == null) return '#95a5a6'
    return v >= 1 ? '#27ae60' : '#e74c3c'
  }
  const fmtIndex = (v) => (v == null ? '—' : Number(v).toFixed(2))

  return (
    <div>
      <h2 style={{ fontSize: '18px', color: '#2c3e50', margin: '0 0 12px' }}>
        {t(region, 'portfolio')}
      </h2>

      {dashLoading && <div className="notice loading">{t(region, 'loading')}…</div>}
      {dashError && (
        <div className="notice error">
          {t(region, 'error')}: {String(dashError)}
        </div>
      )}

      {/* ===== 租戶層級彙總 ===== */}
      {totals && (
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
          {Object.entries(totals).map(([k, v]) => (
            <div key={k}>
              <div style={{ fontSize: '12px', opacity: 0.8 }}>{t(region, k)}</div>
              <div style={{ fontSize: '18px', fontWeight: 700 }}>{String(v)}</div>
            </div>
          ))}
        </div>
      )}

      {/* ===== 專案卡片 ===== */}
      {projects.length === 0 && !dashLoading ? (
        <div style={{ padding: '40px', textAlign: 'center', color: '#999' }}>
          {t(region, 'project')} — {t(region, 'dashboard')}
        </div>
      ) : (
        <div
          style={{
            display: 'grid',
            gridTemplateColumns: 'repeat(auto-fill, minmax(300px, 1fr))',
            gap: '14px',
          }}
        >
          {projects.map((p) => (
            <button
              key={p.project_id}
              type="button"
              onClick={() => handleOpen(p.project_id)}
              style={{
                textAlign: 'left',
                background: '#fff',
                color: '#2c3e50',
                border: '1px solid #e0e3e8',
                borderRadius: '8px',
                padding: '14px',
                cursor: 'pointer',
                display: 'flex',
                flexDirection: 'column',
                gap: '10px',
              }}
            >
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', gap: '8px' }}>
                <strong style={{ fontSize: '15px' }}>{p.project_name}</strong>
                <span style={{ fontSize: '11px', color: '#95a5a6' }}>{p.region}</span>
              </div>
              <div style={{ fontSize: '11px', color: '#95a5a6' }}>{p.project_id}</div>

              {/* KPI 徽章列 */}
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: '6px' }}>
                <Chip label={t(region, 'projectDuration')} value={`${p.project_duration ?? '—'} ${t(region, 'days')}`} />
                <Chip label={t(region, 'criticalPath')} value={p.critical_count ?? '—'} accent="#e74c3c" />
                <Chip label={t(region, 'taskCount')} value={p.task_count ?? '—'} />
              </div>

              {/* EVM 指標（有基準線時） */}
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: '6px' }}>
                <Chip label={t(region, 'spi')} value={fmtIndex(p.spi)} accent={indexColor(p.spi)} />
                <Chip label={t(region, 'cpi')} value={fmtIndex(p.cpi)} accent={indexColor(p.cpi)} />
                {!p.has_baseline && (
                  <span style={{ fontSize: '11px', color: '#95a5a6', alignSelf: 'center' }}>
                    ({t(region, 'baseline')}: {t(region, 'none')})
                  </span>
                )}
              </div>

              {/* 待處理風險徽章 */}
              {p.pending_risk_events > 0 && (
                <div>
                  <span
                    style={{
                      display: 'inline-block',
                      background: '#e74c3c',
                      color: '#fff',
                      borderRadius: '10px',
                      padding: '2px 10px',
                      fontSize: '12px',
                      fontWeight: 600,
                    }}
                  >
                    ⚠ {t(region, 'pendingRisks')}: {p.pending_risk_events}
                  </span>
                </div>
              )}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

// KPI 小徽章
function Chip({ label, value, accent }) {
  return (
    <span
      style={{
        display: 'inline-flex',
        flexDirection: 'column',
        background: '#f7f9fc',
        border: '1px solid #e0e3e8',
        borderRadius: '6px',
        padding: '4px 8px',
        minWidth: '64px',
      }}
    >
      <span style={{ fontSize: '10px', color: '#7f8c8d' }}>{label}</span>
      <span style={{ fontSize: '14px', fontWeight: 700, color: accent || '#2c3e50' }}>{value}</span>
    </span>
  )
}
