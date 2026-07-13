// Pro Batch C — 行動裝置工地回報：離線佇列 (offline-first queue)
//
// 純模組、可單元測試。優先使用 IndexedDB 持久化佇列項目（重新整理/關閉瀏覽器
// 後仍保留待同步資料）；當環境沒有 indexedDB（例如測試環境 jsdom、或極舊瀏覽器/
// 隱私模式封鎖）時，自動退回記憶體內陣列，讓呼叫端完全不需要關心底層實作。
//
// 佇列項目形狀：
//   {
//     id: number,               // 由儲存層配發（IndexedDB 自動遞增 key 或記憶體計數器）
//     type: 'progress' | 'photo',
//     projectId: string,
//     taskId: string,
//     payload: object,          // type==='progress' -> 進度欄位；type==='photo' -> { blob, note, ... }
//     queuedAt: number,         // Date.now()，用於 FIFO 排序
//   }
//
// 對外 API：enqueue(item)、listPending()、remove(id)、replay(handlers)。

const DB_NAME = 'cpm_field_queue'
const DB_VERSION = 1
const STORE_NAME = 'queue'

// ---- 記憶體內備援（indexedDB 不存在時使用） ----
let memoryItems = []
let memoryNextId = 1

function resetMemoryStoreForTests() {
  memoryItems = []
  memoryNextId = 1
}

function hasIndexedDb() {
  return typeof indexedDB !== 'undefined' && indexedDB !== null
}

let dbPromise = null

function openDb() {
  if (dbPromise) return dbPromise
  dbPromise = new Promise((resolve, reject) => {
    let request
    try {
      request = indexedDB.open(DB_NAME, DB_VERSION)
    } catch (err) {
      reject(err)
      return
    }
    request.onupgradeneeded = () => {
      const db = request.result
      if (!db.objectStoreNames.contains(STORE_NAME)) {
        db.createObjectStore(STORE_NAME, { keyPath: 'id', autoIncrement: true })
      }
    }
    request.onsuccess = () => resolve(request.result)
    request.onerror = () => reject(request.error)
  })
  // 開啟失敗時清掉快取，讓下一次呼叫可以重試（例如暫時性錯誤恢復後）。
  dbPromise.catch(() => {
    dbPromise = null
  })
  return dbPromise
}

function buildRecord(item) {
  return {
    type: item.type,
    projectId: item.projectId,
    taskId: item.taskId,
    payload: item.payload,
    queuedAt: item.queuedAt || Date.now(),
  }
}

/**
 * 將一筆待同步項目加入佇列，回傳含 id 的完整項目。
 */
export async function enqueue(item) {
  const record = buildRecord(item)

  if (!hasIndexedDb()) {
    const stored = { id: memoryNextId, ...record }
    memoryNextId += 1
    memoryItems.push(stored)
    return stored
  }

  const db = await openDb()
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE_NAME, 'readwrite')
    const store = tx.objectStore(STORE_NAME)
    const req = store.add(record)
    req.onsuccess = () => resolve({ id: req.result, ...record })
    req.onerror = () => reject(req.error)
  })
}

/**
 * 列出所有待同步項目，依加入順序 (FIFO) 排序。
 */
export async function listPending() {
  let items
  if (!hasIndexedDb()) {
    items = [...memoryItems]
  } else {
    const db = await openDb()
    items = await new Promise((resolve, reject) => {
      const tx = db.transaction(STORE_NAME, 'readonly')
      const store = tx.objectStore(STORE_NAME)
      const req = store.getAll()
      req.onsuccess = () => resolve(req.result || [])
      req.onerror = () => reject(req.error)
    })
  }
  return items.slice().sort((a, b) => a.queuedAt - b.queuedAt || a.id - b.id)
}

/**
 * 移除指定 id 的佇列項目（同步成功後呼叫）。
 */
export async function remove(id) {
  if (!hasIndexedDb()) {
    memoryItems = memoryItems.filter((it) => it.id !== id)
    return
  }
  const db = await openDb()
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE_NAME, 'readwrite')
    tx.objectStore(STORE_NAME).delete(id)
    tx.oncomplete = () => resolve()
    tx.onerror = () => reject(tx.error)
  })
}

/**
 * 依 FIFO 順序重播佇列：對每一項呼叫 handlers[item.type](item)。
 * 成功則從佇列移除並繼續下一項；一旦某項失敗（handler 拋出/回傳 rejected promise，
 * 或找不到對應 type 的 handler），立即停止並保留該項與其後所有項目在佇列中
 * （stop-and-keep 語意，避免同步順序錯亂或漏傳）。
 *
 * @param {Record<string, (item: object) => Promise<any>>} handlers
 * @returns {Promise<{ok: number, failed: number}>} ok = 成功並移除的筆數；
 *   failed = 因故停止後仍留在佇列中的筆數（0 代表全部同步成功）。
 */
export async function replay(handlers) {
  const items = await listPending()
  let ok = 0

  for (let i = 0; i < items.length; i += 1) {
    const item = items[i]
    const handler = handlers && handlers[item.type]
    if (typeof handler !== 'function') {
      return { ok, failed: items.length - ok }
    }
    try {
      await handler(item)
    } catch (err) {
      return { ok, failed: items.length - ok }
    }
    await remove(item.id)
    ok += 1
  }

  return { ok, failed: items.length - ok }
}

// 僅供測試使用：重置記憶體備援狀態，避免測試之間互相污染。
export const __testing = { resetMemoryStoreForTests, hasIndexedDb }
