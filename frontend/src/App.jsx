import React, { useState } from 'react'
import ScheduleBoard from './components/ScheduleBoard.jsx'
import DashboardView from './components/DashboardView.jsx'
import UserAdminPanel from './components/UserAdminPanel.jsx'
import Login from './components/Login.jsx'
import { useScheduleStore } from './store/scheduleStore'
import { t } from './i18n'

// Batch 4：頂層視圖對應的錯誤 scope（切換視圖時清除該視圖的殘留錯誤）
const VIEW_ERROR_SCOPES = {
  board: ['project', 'projects'],
  dashboard: ['dashboard'],
  users: ['users', 'trash'],
}

// 根元件：
//   - 未持有權杖 (store.token) -> 顯示 Login 登入頁
//   - 已登入 -> 頂部使用者列（帳號 / 租戶 / 角色 + 登出；username 由
//     session restore 經 GET /auth/me 復原，重新整理後仍顯示）+ 導覽分頁
//     （排程板 / 儀表板 / 使用者管理[僅 admin]）
export default function App() {
  const { token, username, tenantId, region, role, logout, clearError } = useScheduleStore()
  // 頂層視圖：'board'（排程板）| 'dashboard'（投資組合儀表板）| 'users'（使用者管理）
  const [view, setView] = useState('board')

  // 切換頂層視圖：清除目標視圖 scope 的殘留錯誤後再切換
  const handleViewSwitch = (key) => {
    ;(VIEW_ERROR_SCOPES[key] || []).forEach((scope) => clearError(scope))
    setView(key)
  }

  if (!token) {
    return <Login />
  }

  // 導覽分頁定義（使用者管理僅 admin 可見）
  const navItems = [
    { key: 'board', label: t(region, 'board') },
    { key: 'dashboard', label: t(region, 'dashboard') },
  ]
  if (role === 'admin') {
    navItems.push({ key: 'users', label: t(region, 'users') })
  }

  // 角色徽章顏色
  const roleColor = role === 'admin' ? '#8e44ad' : role === 'editor' ? '#2980b9' : '#7f8c8d'

  return (
    <div className="app-shell">
      <header className="app-header">
        <div className="app-header-user">
          <span className="app-header-label">{t(region, 'loggedInAs')}</span>
          <strong className="app-header-username">{username || '—'}</strong>
          <span className="app-header-tenant">
            {t(region, 'tenant')}: {tenantId}
          </span>
          {role && (
            <span
              className="app-header-tenant"
              style={{ background: roleColor, color: '#fff', borderColor: roleColor }}
            >
              {t(region, 'role')}: {t(region, role)}
            </span>
          )}
        </div>
        <button type="button" className="app-header-logout" onClick={logout}>
          {t(region, 'logout')}
        </button>
      </header>

      {/* ===== 頂層導覽分頁 ===== */}
      <nav className="app-nav">
        {navItems.map((item) => (
          <button
            key={item.key}
            type="button"
            className={`app-nav-tab${view === item.key ? ' active' : ''}`}
            onClick={() => handleViewSwitch(item.key)}
          >
            {item.label}
          </button>
        ))}
      </nav>

      {/* ===== 視圖切換 =====
          已登入：權杖租戶為單一真實來源，ScheduleBoard 仍可顯示但租戶由 token 決定。
          DashboardView 點擊專案後切回排程板。UserAdminPanel 僅 admin 渲染（內部亦再次防護）。 */}
      <div className="app-view">
        {view === 'board' && <ScheduleBoard authMode />}
        {view === 'dashboard' && (
          <div className="board">
            <DashboardView region={region} onOpenProject={() => setView('board')} />
          </div>
        )}
        {view === 'users' && role === 'admin' && (
          <div className="board">
            <UserAdminPanel region={region} />
          </div>
        )}
      </div>
    </div>
  )
}
