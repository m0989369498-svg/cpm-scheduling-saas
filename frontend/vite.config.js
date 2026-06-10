import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Vite 設定：React 外掛 + 開發伺服器埠 5173
// 生產環境由 nginx 提供靜態檔；開發時走 VITE_API_BASE_URL 指向後端。
export default defineConfig({
  plugins: [react()],
  server: {
    host: true,
    port: 5173,
  },
  preview: {
    host: true,
    port: 5173,
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
  },
})
