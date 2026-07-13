// Pro Batch C — 行動裝置工地回報：手刻 Service Worker（不依賴 Workbox）
//
// 策略：
//   - install：預先快取 app shell（首頁、manifest、圖示），讓現場模式離線也能開啟。
//   - fetch：
//       /api/* -> network-first（優先拿最新資料；離線或請求失敗時退回快取，
//                 讓已經看過的專案/任務清單仍可瀏覽）。
//       其餘（shell/static 資源）-> cache-first（離線優先可用，背景仍會更新快取）。
//   - activate：清掉舊版快取，避免版本升級後裝置端殘留過期資源。
//
// 注意：非 GET 請求（進度回報 PUT、照片上傳 POST）一律不攔截，交給頁面內的
// 離線佇列 (src/offline/fieldQueue.js) 處理排隊與重播，Service Worker 不做寫入快取。

const CACHE_VERSION = 'v1'
const CACHE_NAME = `cpm-field-shell-${CACHE_VERSION}`
// /api/* 回應快取與 shell 分開存放，且「依租戶分桶」：Cache API 只以
// URL + method 作為 key，看不見 Authorization / X-Tenant-Id 標頭 —— 共用
// 工地裝置上若不分桶，租戶 A 快取的回應會在離線回退時回給租戶 B。
// 登出時由前端整批清除（scheduleStore.logout 依 API_CACHE_PREFIX 前綴刪除，
// 兩處字串須保持一致）。
const API_CACHE_PREFIX = 'cpm-field-api-'
const API_CACHE_BASE = `${API_CACHE_PREFIX}${CACHE_VERSION}`
const SHELL_ASSETS = ['/', '/index.html', '/manifest.webmanifest', '/icon.svg']

// 依請求的 X-Tenant-Id 標頭決定 API 快取桶名（無標頭時歸入 anon 桶）。
function apiCacheName(request) {
  const tenant = request.headers.get('X-Tenant-Id') || 'anon'
  return `${API_CACHE_BASE}-${tenant}`
}

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches
      .open(CACHE_NAME)
      .then((cache) => cache.addAll(SHELL_ASSETS))
      .then(() => self.skipWaiting())
  )
})

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(
          keys
            // 保留：目前版本的 shell 快取 + 目前版本的各租戶 API 快取桶；
            // 其餘（舊版 shell / 舊版 API 桶）一律清除。
            .filter((key) => key !== CACHE_NAME && !key.startsWith(`${API_CACHE_BASE}-`))
            .map((key) => caches.delete(key))
        )
      )
      .then(() => self.clients.claim())
  )
})

self.addEventListener('fetch', (event) => {
  const { request } = event

  // 只處理 GET；寫入類請求交給前端離線佇列處理，SW 不介入。
  if (request.method !== 'GET') return

  let url
  try {
    url = new URL(request.url)
  } catch (err) {
    return
  }

  // 只處理同源請求，避免干擾第三方資源。
  if (url.origin !== self.location.origin) return

  if (url.pathname.startsWith('/api/')) {
    event.respondWith(networkFirst(request))
    return
  }

  event.respondWith(cacheFirst(request))
})

async function networkFirst(request) {
  const cacheName = apiCacheName(request)
  try {
    const response = await fetch(request)
    if (response && response.ok) {
      // 僅快取非二進位圖片的 API 回應（專案/任務/進度等 JSON）：照片與 QR
      // 圖檔體積大且數量無上限，全部寫入 Cache Storage 會讓共用工地裝置的
      // 儲存空間無限膨脹（照片縮圖本就以驗證過的 fetch 於頁面層處理）。
      const contentType = response.headers.get('content-type') || ''
      if (!contentType.startsWith('image/')) {
        const cache = await caches.open(cacheName)
        cache.put(request, response.clone())
      }
    }
    return response
  } catch (err) {
    // 僅在「本租戶」的快取桶內查找。絕不可用全域 caches.match()：
    // 它會跨桶搜尋，等於把其他租戶的快取回應拿來回應本租戶。
    const cache = await caches.open(cacheName)
    const cached = await cache.match(request)
    if (cached) return cached
    throw err
  }
}

async function cacheFirst(request) {
  const cached = await caches.match(request)
  if (cached) return cached
  try {
    const response = await fetch(request)
    if (response && response.ok) {
      const cache = await caches.open(CACHE_NAME)
      cache.put(request, response.clone())
    }
    return response
  } catch (err) {
    // 離線且無快取可用：讓請求自然失敗（呼叫端可依此顯示離線狀態）。
    throw err
  }
}
