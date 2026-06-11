import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Vite 設定：React 外掛 + 開發伺服器埠 5173
// 生產環境由 nginx 提供靜態檔；開發時走 VITE_API_BASE_URL 指向後端。
export default defineConfig({
  plugins: [react()],
  server: {
    host: true,
    port: 5173,
    // 公開通道 (cloudflared) 會以隧道網域作為 Host header；允許任意 host 以免被擋。
    allowedHosts: true,
    // 同源代理：瀏覽器打 /api/* 由 dev server 轉發到後端，
    // 遠端使用者才不會去打「自己的 localhost:8000」。
    proxy: {
      '/api': { target: 'http://localhost:8000', changeOrigin: true },
    },
  },
  preview: {
    host: true,
    port: 5173,
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
  },
  // QUAL-2 (Batch 4)：vitest 單元測試設定 — jsdom 環境 + 全域 API (describe/it/expect)
  // setupFiles 載入 @testing-library/jest-dom matchers 與 localStorage 清理。
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test/setup.js'],
  },
})
