// QUAL-2 (Batch 4)：vitest 共用測試前置
// - 載入 @testing-library/jest-dom matchers（toBeInTheDocument 等）
// - 每個測試前清空 localStorage，避免權杖/租戶殘留造成測試相互污染
import '@testing-library/jest-dom/vitest'
import { beforeEach } from 'vitest'

beforeEach(() => {
  try {
    localStorage.clear()
  } catch (e) {
    /* jsdom 必有 localStorage；保險忽略 */
  }
})
