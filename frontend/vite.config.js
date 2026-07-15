import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Pro Batch F1：demo 模式專用 — 重寫 PWA manifest 的絕對路徑。
// Vite 的 base 重寫只作用於 index.html 內的 href/src 屬性，不會改寫由 public/
// 原樣複製的 manifest.webmanifest「內容」；該檔的 start_url:"/?field=1"、
// scope:"/"、icons[].src:"/icon.svg" 在正式部署（nginx 網域根目錄）下正確，
// 但在 GitHub Pages 的 /cpm-scheduling-saas/demo/ 子路徑下會指向 Pages 網域
// 根目錄（行銷首頁 / 404 圖示）。此外掛在建置完成（public/ 已複製）後，把
// 輸出目錄中 manifest 的根路徑改為以 base 為前綴，讓「加入主畫面」等讀取
// manifest 的瀏覽器行為指回 demo 子路徑。一般 build 不掛此外掛、完全不變。
function demoManifestBasePlugin(base, outDir) {
  const prefix = base.replace(/\/$/, '')
  const rebase = (p) => (typeof p === 'string' && p.startsWith('/') ? `${prefix}${p}` : p)
  return {
    name: 'cpm-demo-manifest-base',
    apply: 'build',
    closeBundle() {
      const file = path.resolve(
        path.dirname(fileURLToPath(import.meta.url)),
        outDir,
        'manifest.webmanifest',
      )
      if (!fs.existsSync(file)) return
      const manifest = JSON.parse(fs.readFileSync(file, 'utf8'))
      if (manifest.start_url) manifest.start_url = rebase(manifest.start_url)
      if (manifest.scope) manifest.scope = rebase(manifest.scope) || base
      if (Array.isArray(manifest.icons)) {
        for (const icon of manifest.icons) icon.src = rebase(icon.src)
      }
      fs.writeFileSync(file, `${JSON.stringify(manifest, null, 2)}\n`)
    },
  }
}

// Vite 設定：React 外掛 + 開發伺服器埠 5173
// 生產環境由 nginx 提供靜態檔；開發時走 VITE_API_BASE_URL 指向後端。
//
// Pro Batch F1：新增 'demo' 模式（`vite build --mode demo`，見 package.json 的
// build:demo script + frontend/.env.demo）——獨立展示站要部署在 GitHub Pages 的
// 子路徑 /cpm-scheduling-saas/demo/ 之下，需要不同的 base 與輸出目錄；一般
// build/dev/preview（mode 未指定或非 'demo'）維持原本設定完全不變。
export default defineConfig(({ mode }) => {
  const isDemo = mode === 'demo'
  const demoBase = '/cpm-scheduling-saas/demo/'
  return {
    plugins: isDemo ? [react(), demoManifestBasePlugin(demoBase, 'dist-demo')] : [react()],
    base: isDemo ? demoBase : '/',
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
      outDir: isDemo ? 'dist-demo' : 'dist',
      sourcemap: false,
    },
    // QUAL-2 (Batch 4)：vitest 單元測試設定 — jsdom 環境 + 全域 API (describe/it/expect)
    // setupFiles 載入 @testing-library/jest-dom matchers 與 localStorage 清理。
    test: {
      environment: 'jsdom',
      globals: true,
      setupFiles: ['./src/test/setup.js'],
    },
  }
})
