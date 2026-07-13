import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App.jsx'
import './styles.css'

// 應用程式進入點：掛載 React 至 #root
ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
)

// Pro Batch C：註冊 Service Worker（僅限正式建置，開發模式下 HMR 與 SW 快取會互相干擾）。
// 讓行動裝置工地現場模式離線也能開啟 app shell 並排隊回報，恢復連線後自動重播。
if (import.meta.env.PROD && 'serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js').catch(() => {
      // 註冊失敗（例如非 HTTPS/非 localhost 環境）不應影響應用程式正常運作。
    })
  })
}
