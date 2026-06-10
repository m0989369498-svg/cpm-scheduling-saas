import React from 'react'
import ScheduleBoard from './components/ScheduleBoard.jsx'
import Login from './components/Login.jsx'
import { useScheduleStore } from './store/scheduleStore'
import { t } from './i18n'

// 根元件：
//   - 未持有權杖 (store.token) -> 顯示 Login 登入頁
//   - 已登入 -> 顯示頂部使用者列（帳號 / 租戶 + 登出）與既有的 ScheduleBoard
export default function App() {
  const { token, username, tenantId, region, logout } = useScheduleStore()

  if (!token) {
    return <Login />
  }

  return (
    <div className="app-shell">
      <header className="app-header">
        <div className="app-header-user">
          <span className="app-header-label">{t(region, 'loggedInAs')}</span>
          <strong className="app-header-username">{username || '—'}</strong>
          <span className="app-header-tenant">
            {t(region, 'tenant')}: {tenantId}
          </span>
        </div>
        <button type="button" className="app-header-logout" onClick={logout}>
          {t(region, 'logout')}
        </button>
      </header>
      {/* 已登入：權杖租戶為單一真實來源，ScheduleBoard 仍可顯示但租戶由 token 決定。 */}
      <ScheduleBoard authMode />
    </div>
  )
}
