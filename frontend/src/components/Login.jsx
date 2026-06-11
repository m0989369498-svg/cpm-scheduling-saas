import React, { useState } from 'react'
import { useScheduleStore } from '../store/scheduleStore'
import { t } from '../i18n'

/**
 * Login 登入卡片
 *
 * 置中卡片，含帳號 / 密碼輸入、送出按鈕（呼叫 store.login）、錯誤顯示。
 * 僅在建置時設定 VITE_DEMO_LOGIN=1 時才預填示範帳密（admin@tw / demo1234）
 * 並顯示示範帳號提示；正式環境預設為空白欄位、不洩漏任何帳密。
 * 登入成功後由 App 依 store.token 切換到 ScheduleBoard。
 */
const DEMO = import.meta.env.VITE_DEMO_LOGIN === '1'

export default function Login() {
  const { region, loading, error, login } = useScheduleStore()

  // 僅 demo 模式預填示範帳密，方便評審/開發直接登入。
  const [username, setUsername] = useState(DEMO ? 'admin@tw' : '')
  const [password, setPassword] = useState(DEMO ? 'demo1234' : '')
  // 本地送出失敗旗標：避免顯示其他流程的 store.error。
  const [failed, setFailed] = useState(false)

  const handleSubmit = async (e) => {
    e.preventDefault()
    setFailed(false)
    try {
      await login(username.trim(), password)
    } catch (err) {
      setFailed(true)
    }
  }

  return (
    <div className="login-page">
      <form className="login-card" onSubmit={handleSubmit}>
        <h1 className="login-title">{t(region, 'appTitle')}</h1>
        <h2 className="login-subtitle">{t(region, 'login')}</h2>

        <div className="login-field">
          <label htmlFor="login-username">{t(region, 'username')}</label>
          <input
            id="login-username"
            type="text"
            autoComplete="username"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            placeholder={t(region, 'username')}
          />
        </div>

        <div className="login-field">
          <label htmlFor="login-password">{t(region, 'password')}</label>
          <input
            id="login-password"
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder={t(region, 'password')}
          />
        </div>

        {failed && (
          <div className="login-error">
            {error ? String(error) : t(region, 'loginFailed')}
          </div>
        )}

        <button type="submit" className="login-submit" disabled={loading}>
          {loading ? `${t(region, 'loading')}…` : t(region, 'signIn')}
        </button>

        {DEMO && (
          <div className="login-hint">
            <div className="login-hint-title">{t(region, 'demoAccounts')}</div>
            <div className="login-hint-line">admin@tw / demo1234</div>
            <div className="login-hint-line">admin@cn / demo1234</div>
          </div>
        )}
      </form>
    </div>
  )
}
