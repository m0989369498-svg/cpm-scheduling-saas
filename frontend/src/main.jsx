import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App.jsx'
import './styles.css'

// 應用程式進入點：掛載 React 至 #root
function renderApp() {
  ReactDOM.createRoot(document.getElementById('root')).render(
    <React.StrictMode>
      <App />
    </React.StrictMode>,
  )
}

// Pro Batch F1：獨立展示站（GitHub Pages，無後端）— VITE_DEMO_STANDALONE==='1' 時，
// 先動態載入 demo/mockApi.js 並安裝其 axios 自訂 adapter（讓所有 API 請求皆在
// 瀏覽器記憶體中處理），再渲染 React；避免任何一次 axios 請求在 adapter 掛上之前
// 就先打到真實網路（Pages 上沒有後端可打，會直接失敗）。以 .then(renderApp) 串接
// （而非頂層 await），維持本檔案為一般同步模組。
const DEMO_STANDALONE = import.meta.env.VITE_DEMO_STANDALONE === '1'

if (DEMO_STANDALONE) {
  // demo 模式：Service Worker 的 /api/* network-first 快取策略會與瀏覽器端
  // 模擬後端互相干擾（快取到的回應可能來自別次 demo session），一律不註冊；
  // 並盡力反註冊同網域可能殘留的既有 SW（例如同一裝置先前開過正式版 PWA）。
  if ('serviceWorker' in navigator) {
    navigator.serviceWorker
      .getRegistrations()
      .then((regs) => Promise.all(regs.map((r) => r.unregister())))
      .catch(() => {
        // 不支援 / 權限問題：不影響 demo 主流程
      })
  }
  import('./demo/mockApi.js').then((mod) => {
    mod.install()
    renderApp()
  })
} else {
  renderApp()

  // Pro Batch C：註冊 Service Worker（僅限正式建置，開發模式下 HMR 與 SW 快取會互相干擾）。
  // 讓行動裝置工地現場模式離線也能開啟 app shell 並排隊回報，恢復連線後自動重播。
  if (import.meta.env.PROD && 'serviceWorker' in navigator) {
    window.addEventListener('load', () => {
      navigator.serviceWorker.register('/sw.js').catch(() => {
        // 註冊失敗（例如非 HTTPS/非 localhost 環境）不應影響應用程式正常運作。
      })
    })
  }
}
