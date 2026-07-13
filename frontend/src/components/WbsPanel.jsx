import React, { useEffect, useState } from 'react';
import { useScheduleStore, isLoading, getError } from '../store/scheduleStore';
import { t } from '../i18n';

/**
 * WbsPanel — WBS（工作分解結構）編輯面板（Pro Batch B · Feature 1）
 *
 * 功能：
 *   - 顯示扁平表格：wbs_code / name / parent（下拉選其他列的 code，或「無」代表根節點）/ sort_order
 *   - 新增／移除列（移除某列時，一併清除其他列指向該 code 的 parent_code，
 *     避免送出後端時出現懸空參照而被 422 拒絕）
 *   - 「儲存」-> store.saveWbs(list)：PUT /projects/{pid}/wbs 整批取代 upsert。
 *     唯一碼／parent 是否存在／有無循環由後端把關；422 時錯誤訊息顯示於 errors.wbs
 *     （FastAPI 陣列 detail 已由 store.extractError 人性化）。
 *   - 儲存前僅過濾掉尚未填寫 wbs_code 的空白列（不阻擋送出），其餘驗證交由後端。
 *
 * props:
 *   region   : 'TW' | 'CN'
 *   canWrite : boolean（viewer 唯讀：欄位停用、隱藏新增/移除/儲存按鈕）
 *
 * 資料來源：store.wbs（list[{wbs_code,name,parent_code,sort_order}]）
 */

let rowSeq = 0;
function nextKey() {
  rowSeq += 1;
  return `wbs-row-${rowSeq}`;
}

function rowFromNode(n) {
  return {
    _key: nextKey(),
    wbs_code: (n && n.wbs_code) || '',
    name: (n && n.name) || '',
    parent_code: (n && n.parent_code) || '',
    sort_order: n && Number.isFinite(Number(n.sort_order)) ? Number(n.sort_order) : 0,
  };
}

export default function WbsPanel({ region = 'TW', canWrite = true }) {
  const store = useScheduleStore();
  const { currentProject, wbs, loadWbs, saveWbs } = store;

  // Batch 4 慣例：本面板僅讀取 wbs scope 的載入與錯誤
  const busy = isLoading(store, 'wbs');
  const panelError = getError(store, 'wbs');

  // 本地草稿列（含前端專用 _key，儲存時剝除）
  const [rows, setRows] = useState([]);

  const projectId = currentProject?.project_id;

  // 掛載／切換專案時載入 WBS 節點
  useEffect(() => {
    if (projectId) {
      loadWbs().catch(() => {});
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId]);

  // 後端 WBS 清單回傳後同步至草稿（依 sort_order 排列）
  useEffect(() => {
    if (Array.isArray(wbs)) {
      const sorted = wbs
        .slice()
        .sort((a, b) => (Number(a.sort_order) || 0) - (Number(b.sort_order) || 0));
      setRows(sorted.map(rowFromNode));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [wbs]);

  const updateRow = (key, patch) => {
    setRows((prev) => prev.map((r) => (r._key === key ? { ...r, ...patch } : r)));
  };

  const addRow = () => setRows((prev) => [...prev, rowFromNode({ sort_order: prev.length })]);

  // 移除列：一併清除其他列指向被移除 code 的 parent_code，避免懸空參照
  const removeRow = (key) => {
    setRows((prev) => {
      const removed = prev.find((r) => r._key === key);
      const removedCode = removed ? removed.wbs_code.trim() : '';
      const next = prev.filter((r) => r._key !== key);
      return next.map((r) =>
        removedCode && r.parent_code === removedCode ? { ...r, parent_code: '' } : r,
      );
    });
  };

  // 組裝送出清單：僅濾除尚未填 wbs_code 的空白列；其餘驗證交由後端 422
  const buildPayload = () =>
    rows
      .filter((r) => r.wbs_code.trim())
      .map((r) => ({
        wbs_code: r.wbs_code.trim(),
        name: r.name.trim(),
        parent_code: r.parent_code ? r.parent_code.trim() : null,
        sort_order: Number.parseInt(r.sort_order, 10) || 0,
      }));

  const handleSave = async () => {
    try {
      await saveWbs(buildPayload());
    } catch (e) {
      /* 錯誤已存於 errors.wbs */
    }
  };

  if (!currentProject) {
    return (
      <div style={{ padding: '16px', color: '#999' }}>
        {t(region, 'project')} — {t(region, 'projectName')}
      </div>
    );
  }

  return (
    <div className="panel" style={{ background: '#fff' }}>
      <h3 style={{ marginTop: 0, color: '#2c3e50' }}>{t(region, 'wbsEditor')}</h3>

      {/* ===== 面板自身 scope 的載入/錯誤 ===== */}
      {busy && <div className="notice loading">{t(region, 'loading')}…</div>}
      {panelError && (
        <div className="notice error">
          {t(region, 'error')}: {String(panelError)}
        </div>
      )}

      <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '13px', marginBottom: '12px' }}>
        <thead>
          <tr style={{ background: '#f7f9fc', textAlign: 'left' }}>
            <th style={cellHead}>{t(region, 'wbsCode')}</th>
            <th style={cellHead}>{t(region, 'wbsName')}</th>
            <th style={cellHead}>{t(region, 'wbsParent')}</th>
            <th style={cellHead}>#</th>
            {canWrite && <th style={cellHead} />}
          </tr>
        </thead>
        <tbody>
          {rows.length === 0 && (
            <tr>
              <td style={{ ...cell, textAlign: 'center', color: '#999' }} colSpan={canWrite ? 5 : 4}>
                {t(region, 'none')}
              </td>
            </tr>
          )}
          {rows.map((r) => (
            <tr key={r._key} style={{ borderBottom: '1px solid #eee' }}>
              <td style={cell}>
                <input
                  type="text"
                  readOnly={!canWrite}
                  style={{ width: '110px', ...(canWrite ? {} : { background: '#f1f1f1' }) }}
                  value={r.wbs_code}
                  onChange={(e) => updateRow(r._key, { wbs_code: e.target.value })}
                  placeholder="1.1"
                />
              </td>
              <td style={cell}>
                <input
                  type="text"
                  readOnly={!canWrite}
                  style={{ width: '200px', ...(canWrite ? {} : { background: '#f1f1f1' }) }}
                  value={r.name}
                  onChange={(e) => updateRow(r._key, { name: e.target.value })}
                />
              </td>
              <td style={cell}>
                <select
                  disabled={!canWrite}
                  value={r.parent_code || ''}
                  onChange={(e) => updateRow(r._key, { parent_code: e.target.value })}
                >
                  <option value="">{t(region, 'none')}</option>
                  {rows
                    .filter((o) => o._key !== r._key && o.wbs_code.trim())
                    .map((o) => (
                      <option key={o._key} value={o.wbs_code}>
                        {o.wbs_code}
                      </option>
                    ))}
                </select>
              </td>
              <td style={cell}>
                <input
                  type="number"
                  readOnly={!canWrite}
                  style={{ width: '60px', ...(canWrite ? {} : { background: '#f1f1f1' }) }}
                  value={r.sort_order}
                  onChange={(e) => updateRow(r._key, { sort_order: e.target.value })}
                />
              </td>
              {canWrite && (
                <td style={cell}>
                  <button
                    type="button"
                    className="small danger"
                    onClick={() => removeRow(r._key)}
                    title={t(region, 'removeRow')}
                  >
                    {t(region, 'removeRow')}
                  </button>
                </td>
              )}
            </tr>
          ))}
        </tbody>
      </table>

      {canWrite && (
        <div style={{ display: 'flex', gap: '8px' }}>
          <button type="button" className="secondary" onClick={addRow}>
            + {t(region, 'addWbsRow')}
          </button>
          <button type="button" onClick={handleSave} disabled={busy}>
            {t(region, 'save')}
          </button>
        </div>
      )}
    </div>
  );
}

const cellHead = {
  padding: '6px 10px',
  borderBottom: '2px solid #ddd',
  fontSize: '12px',
  color: '#555',
};
const cell = {
  padding: '5px 10px',
  verticalAlign: 'middle',
};
