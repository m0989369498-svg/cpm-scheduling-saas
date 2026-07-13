// Pro Batch B · Feature 1：WBS（工作分解結構）純函式輔助
//
//   buildWbsTree(flatList) — 由後端 GET /projects/{pid}/wbs 回傳的扁平陣列
//     [{wbs_code, name, parent_code, sort_order}] 建出巢狀樹狀結構，供
//     WbsPanel 顯示層級、ScheduleBoard 任務表格分組、GanttChart 分組列渲染共用。
//     同層節點依 sort_order 遞增排序（相同時以 wbs_code 字串排序，穩定 tiebreaker）。
//     孤兒節點（parent_code 為 null，或指向清單中不存在／自我參照的節點）視為根節點。
//
//   groupTasksByWbs(tasks, wbsList, uncategorizedLabel) — 依 WBS 樹形（前序展開）
//     將任務分組為有序列表 [{type:'header', code, name, depth} | {type:'task', task, depth}]。
//     任務以 task.wbs_code 對應 WBS 節點；無 wbs_code 或指向不存在節點的任務歸入
//     「未分類」群組（置於最後）。wbsList 為空陣列時，回傳未分組（僅 task 列）的原始順序，
//     供呼叫端判斷「flat when none」。
//
// 兩者皆為無副作用的純函式，不觸碰 store／DOM，方便單元測試（見 wbsTree.test.js）。

export function buildWbsTree(flatList) {
  const list = Array.isArray(flatList) ? flatList : [];
  const nodeByCode = new Map();

  list.forEach((n) => {
    if (!n || n.wbs_code == null || n.wbs_code === '') return;
    nodeByCode.set(n.wbs_code, {
      wbs_code: n.wbs_code,
      name: n.name || '',
      parent_code: n.parent_code ?? null,
      sort_order: Number.isFinite(Number(n.sort_order)) ? Number(n.sort_order) : 0,
      children: [],
    });
  });

  const roots = [];
  nodeByCode.forEach((node) => {
    const parent = node.parent_code != null ? nodeByCode.get(node.parent_code) : null;
    // 孤兒節點（parent_code 指向不存在的節點，或自我參照）視為根節點，
    // 避免異常/循環資料造成樹狀結構失真或無限遞迴。
    if (parent && parent !== node) {
      parent.children.push(node);
    } else {
      roots.push(node);
    }
  });

  const byOrder = (a, b) =>
    a.sort_order - b.sort_order || String(a.wbs_code).localeCompare(String(b.wbs_code));

  const sortTree = (nodes) => {
    nodes.sort(byOrder);
    nodes.forEach((n) => sortTree(n.children));
    return nodes;
  };

  return sortTree(roots);
}

// 將樹攤平為前序 (DFS) 走訪順序 [{node, depth}]，供分組渲染使用。
// seen 集合防禦：即使上游資料異常造成同一節點被多處引用，也不會重複輸出或無限遞迴。
function flattenTree(nodes, depth = 0, out = [], seen = new Set()) {
  nodes.forEach((node) => {
    if (seen.has(node.wbs_code)) return;
    seen.add(node.wbs_code);
    out.push({ node, depth });
    if (node.children && node.children.length > 0) {
      flattenTree(node.children, depth + 1, out, seen);
    }
  });
  return out;
}

export function groupTasksByWbs(tasks, wbsList, uncategorizedLabel = '未分類') {
  const list = Array.isArray(tasks) ? tasks : [];
  const wbs = Array.isArray(wbsList) ? wbsList : [];

  // 專案尚未建立 WBS 節點：維持扁平清單（呼叫端據此判斷不渲染分組標頭）。
  if (wbs.length === 0) {
    return list.map((task) => ({ type: 'task', task }));
  }

  const codes = new Set(wbs.map((n) => n.wbs_code));
  const flat = flattenTree(buildWbsTree(wbs));

  const tasksByCode = new Map();
  const uncategorized = [];
  list.forEach((task) => {
    const code = task && task.wbs_code;
    if (code != null && codes.has(code)) {
      if (!tasksByCode.has(code)) tasksByCode.set(code, []);
      tasksByCode.get(code).push(task);
    } else {
      uncategorized.push(task);
    }
  });

  const rows = [];
  flat.forEach(({ node, depth }) => {
    rows.push({ type: 'header', code: node.wbs_code, name: node.name, depth });
    (tasksByCode.get(node.wbs_code) || []).forEach((task) => rows.push({ type: 'task', task, depth }));
  });
  if (uncategorized.length > 0) {
    rows.push({ type: 'header', code: '__uncategorized__', name: uncategorizedLabel, depth: 0 });
    uncategorized.forEach((task) => rows.push({ type: 'task', task, depth: 0 }));
  }
  return rows;
}

export default { buildWbsTree, groupTasksByWbs };
