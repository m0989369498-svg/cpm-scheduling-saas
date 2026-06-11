// QUAL-2 (Batch 4)：i18n 字典單元測試
// 驗證 t() 回退順序（指定區域 -> TW -> key 本身）、巢狀鍵（statuses.*）
// 與 TW/CN 鍵完整對等（cross-strait parity，深層比對）。
import { describe, it, expect } from 'vitest'
import { I18N, t, tStatus } from './index.js'

// 遞迴蒐集物件的深層鍵路徑（'statuses.PENDING' 形式），排序後便於比對
function deepKeys(obj, prefix = '') {
  const keys = []
  Object.keys(obj).forEach((k) => {
    const path = prefix ? `${prefix}.${k}` : k
    const v = obj[k]
    if (v && typeof v === 'object' && !Array.isArray(v)) {
      keys.push(...deepKeys(v, path))
    } else {
      keys.push(path)
    }
  })
  return keys.sort()
}

describe('i18n t()', () => {
  it('returns the region-specific translation', () => {
    expect(t('TW', 'save')).toBe('儲存')
    expect(t('CN', 'save')).toBe('保存')
  })

  it('falls back to TW for an unknown region', () => {
    expect(t('EN', 'save')).toBe(I18N.TW.save)
    expect(t(undefined, 'projectName')).toBe(I18N.TW.projectName)
  })

  it('returns the key itself for an unknown key', () => {
    expect(t('TW', 'noSuchKeyAnywhere')).toBe('noSuchKeyAnywhere')
  })

  it('resolves nested statuses.PENDING keys', () => {
    expect(t('TW', 'statuses.PENDING')).toBe('待辦')
    expect(t('CN', 'statuses.PENDING')).toBe('待办')
    expect(tStatus('TW', 'IN_PROGRESS')).toBe('進行中')
  })
})

describe('i18n TW/CN parity', () => {
  it('TW and CN dictionaries expose exactly the same deep key set', () => {
    expect(deepKeys(I18N.CN)).toEqual(deepKeys(I18N.TW))
  })

  it('contains the Batch 4 session/recalc keys in both regions', () => {
    expect(I18N.TW.sessionExpired).toBeTruthy()
    expect(I18N.CN.sessionExpired).toBeTruthy()
    expect(I18N.TW.recalculating).toBeTruthy()
    expect(I18N.CN.recalculating).toBeTruthy()
  })
})
