import React, { useEffect, useState } from 'react'
import { useScheduleStore } from '../store/scheduleStore'
import { t } from '../i18n'

/**
 * UserAdminPanel 使用者管理面板（Phase 10，僅 admin）
 *
 * 僅在 store.role === 'admin' 時渲染（呼叫端亦應條件渲染）。
 *   - 列出本租戶使用者（GET /users）
 *   - 新增使用者（username / password / role）
 *   - 變更角色 / 啟用狀態 / 重設密碼（PUT /users/{id}）
 *   - 刪除使用者（含確認；DELETE /users/{id}）
 * 全部操作後端皆 require_role("admin")，並依 ctx.tenant_id 範圍化。
 */
const ROLES = ['viewer', 'editor', 'admin']

export default function UserAdminPanel({ region }) {
  const { role, users, loading, error, region: storeRegion, loadUsers, createUser, updateUser, deleteUser } =
    useScheduleStore()

  // 新增使用者表單
  const [form, setForm] = useState({ username: '', password: '', role: 'viewer' })
  // 各列「重設密碼」暫存值（key = user id）
  const [pwDrafts, setPwDrafts] = useState({})

  useEffect(() => {
    if (role === 'admin') loadUsers().catch(() => {})
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [role])

  if (role !== 'admin') {
    return (
      <div style={{ padding: '40px', textAlign: 'center', color: '#999' }}>
        {t(region, 'noPermission')}
      </div>
    )
  }

  const handleCreate = async (e) => {
    e.preventDefault()
    if (!form.username.trim() || !form.password) return
    try {
      await createUser({
        username: form.username.trim(),
        password: form.password,
        role: form.role,
        // 預設沿用目前區域，使新使用者與管理者同區（後端 region 為選用）
        region: storeRegion || region,
      })
      setForm({ username: '', password: '', role: 'viewer' })
    } catch (err) {
      /* 錯誤已存於 store.error */
    }
  }

  const handleRoleChange = async (u, newRole) => {
    if (newRole === u.role) return
    await updateUser(u.id, { role: newRole }).catch(() => {})
  }

  const handleActiveToggle = async (u) => {
    await updateUser(u.id, { is_active: !u.is_active }).catch(() => {})
  }

  const handleResetPassword = async (u) => {
    const pw = pwDrafts[u.id]
    if (!pw) return
    await updateUser(u.id, { password: pw }).catch(() => {})
    setPwDrafts((prev) => ({ ...prev, [u.id]: '' }))
  }

  const handleDelete = async (u) => {
    // eslint-disable-next-line no-alert
    if (!window.confirm(t(region, 'confirmDeleteUser'))) return
    await deleteUser(u.id).catch(() => {})
  }

  return (
    <div>
      <h2 style={{ fontSize: '18px', color: '#2c3e50', margin: '0 0 12px' }}>
        {t(region, 'userManagement')}
      </h2>

      {loading && <div className="notice loading">{t(region, 'loading')}…</div>}
      {error && (
        <div className="notice error">
          {t(region, 'error')}: {String(error)}
        </div>
      )}

      {/* ===== 新增使用者 ===== */}
      <form
        onSubmit={handleCreate}
        style={{
          display: 'flex',
          flexWrap: 'wrap',
          gap: '10px',
          alignItems: 'flex-end',
          padding: '12px',
          border: '1px dashed #bbb',
          borderRadius: '6px',
          background: '#fcfcfc',
          marginBottom: '16px',
        }}
      >
        <div style={{ fontSize: '14px', fontWeight: 700, color: '#2c3e50', flexBasis: '100%' }}>
          {t(region, 'createUser')}
        </div>
        <div className="field">
          <label>{t(region, 'username')}</label>
          <input
            type="text"
            value={form.username}
            onChange={(e) => setForm({ ...form, username: e.target.value })}
            placeholder="user@tw"
          />
        </div>
        <div className="field">
          <label>{t(region, 'password')}</label>
          <input
            type="password"
            autoComplete="new-password"
            value={form.password}
            onChange={(e) => setForm({ ...form, password: e.target.value })}
          />
        </div>
        <div className="field">
          <label>{t(region, 'role')}</label>
          <select value={form.role} onChange={(e) => setForm({ ...form, role: e.target.value })}>
            {ROLES.map((r) => (
              <option key={r} value={r}>
                {t(region, r)}
              </option>
            ))}
          </select>
        </div>
        <button type="submit" style={{ background: '#27ae60', borderColor: '#27ae60' }}>
          {t(region, 'createUser')}
        </button>
      </form>

      {/* ===== 使用者清單 ===== */}
      <table>
        <thead>
          <tr>
            <th>{t(region, 'username')}</th>
            <th>{t(region, 'role')}</th>
            <th>{t(region, 'active')}</th>
            <th>{t(region, 'resetPassword')}</th>
            <th>{t(region, 'delete')}</th>
          </tr>
        </thead>
        <tbody>
          {users.length === 0 && (
            <tr>
              <td colSpan={5} style={{ textAlign: 'center', color: '#999' }}>
                {t(region, 'none')}
              </td>
            </tr>
          )}
          {users.map((u) => (
            <tr key={u.id}>
              <td style={{ fontWeight: 600 }}>{u.username}</td>
              <td>
                <select value={u.role} onChange={(e) => handleRoleChange(u, e.target.value)}>
                  {ROLES.map((r) => (
                    <option key={r} value={r}>
                      {t(region, r)}
                    </option>
                  ))}
                </select>
              </td>
              <td>
                <button
                  type="button"
                  className="small"
                  onClick={() => handleActiveToggle(u)}
                  style={
                    u.is_active
                      ? { background: '#27ae60', borderColor: '#27ae60' }
                      : { background: '#fff', color: '#e74c3c', borderColor: '#e74c3c' }
                  }
                >
                  {u.is_active ? '✓ ' + t(region, 'active') : t(region, 'active')}
                </button>
              </td>
              <td>
                <div style={{ display: 'flex', gap: '6px' }}>
                  <input
                    type="password"
                    autoComplete="new-password"
                    placeholder={t(region, 'newPassword')}
                    value={pwDrafts[u.id] || ''}
                    onChange={(e) => setPwDrafts((prev) => ({ ...prev, [u.id]: e.target.value }))}
                    style={{ width: '120px' }}
                  />
                  <button
                    type="button"
                    className="small secondary"
                    onClick={() => handleResetPassword(u)}
                  >
                    {t(region, 'resetPassword')}
                  </button>
                </div>
              </td>
              <td>
                <button
                  type="button"
                  className="small danger"
                  onClick={() => handleDelete(u)}
                >
                  {t(region, 'delete')}
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
