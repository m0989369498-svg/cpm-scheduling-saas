import React, { useCallback, useEffect, useRef, useState } from 'react';
import { t } from '../i18n';

/**
 * GanttChart 甘特圖 / 工期條形圖
 *
 * props:
 *   tasks  : list[TaskResult]  (含 es/ef/duration/float_time/is_critical 等)
 *   region : 'TW' | 'CN'  (i18n 語系；預設 'TW')
 *   onTaskDurationChange? : (taskId, newDuration) => void   (選用)
 *       提供時：每根條形右緣顯示拖曳把手，拖曳即時預覽寬度，
 *       依 dayWidth(30px=1天) 對齊、最小工期 1；放開時若對齊後的工期
 *       與原工期不同，呼叫 onTaskDurationChange(task_id, newDuration)。
 *       未提供時維持原本（不可拖曳）渲染。
 *   overCapacityDays? : list[int]   (選用，Phase 8 資源撫平)
 *       提供時：於這些「日」欄位疊加半透明紅色警示帶（資源衝突視覺化），
 *       橫跨所有任務列；未提供（或空陣列）時維持原本渲染。
 *   baseline? : BaselineOut   (選用，Phase 9 計畫 vs 實際)
 *       提供時：於每根目前條形「之下」繪製一根細的灰色「計畫」條形
 *       （由 baseline.tasks 的 es/duration 推算位置與寬度），供計畫 vs 實際對照。
 *       未提供時維持原本渲染（不繪計畫條）。
 *   progress? : object{ [task_id]: percent_complete:int }   (選用，Phase 9 完成度)
 *       提供時：依完成百分比於目前條形內以較深色填滿；落後（目前條形右緣
 *       超過計畫條形右緣，且有 baseline）時以紅色外框淡染標示。
 *       未提供時維持原本渲染（不填色、不淡染）。
 *   dayDates? : list[str]   (選用，Batch 3 實際日期軸；ISO 日期，索引=工期偏移 0..N)
 *       提供時：日刻度表頭改顯示 MM/DD（每 2 欄一筆避免擁擠；每月 1 日加粗標記），
 *       刻度與條形 tooltip 顯示實際日期（計畫開工/計畫完工）。
 *       未提供時維持原本「天數」刻度渲染。
 *
 *   依賴箭頭（Batch 3）：當任務帶有 links（[{predecessor_task_id, dep_type, lag_days}]）
 *   或 predecessors（視為 FS+0）時，以絕對定位 SVG 覆蓋層繪製肘形連接線：
 *   起點錨於前置任務條形（FS/FF: 條形結束 x=pred.ef*30；SS/SF: 條形開始 x=pred.es*30），
 *   終點錨於後繼任務條形開始（FS/SS）或結束（FF/SF），末端帶小箭頭；
 *   兩端任務皆為要徑時紅色、否則灰色。
 *
 * 繪製規則 (沿用原型外觀)：
 *   - 每一列代表一個任務 (task)
 *   - 條形 left  = es * dayWidth (dayWidth = 30)
 *   - 條形 width = duration * dayWidth
 *   - 條形高度 ~24px
 *   - 要徑 (critical: is_critical || float_time === 0) => 紅色 #e74c3c
 *     非要徑 => 藍色 #3498db
 *   - 左側 150px 標籤欄顯示 task_id + task_name
 *   - 條形上顯示 工期(duration) + 日標籤；要徑顯示 🔥 標記
 *   - 頂部含「日刻度」表頭 (day-axis header)
 */

const DAY_WIDTH = 30; // 每「天」對應的像素寬度
const ROW_HEIGHT = 34; // 每列高度
const BAR_HEIGHT = 24; // 條形高度
const LABEL_WIDTH = 150; // 左側標籤欄寬度

// 判斷是否為要徑任務 (要徑：被標記為 critical，或總時差為 0)
function isCritical(task) {
  return Boolean(task.is_critical) || task.float_time === 0;
}

// Batch 3：'2026-07-01' -> '07/01'（以字串切割避免時區位移）
function isoMonthDay(iso) {
  const parts = String(iso || '').split('-');
  return parts.length === 3 ? `${parts[1]}/${parts[2]}` : String(iso || '');
}

// Batch 3：是否為每月 1 日（首月標記）
function isFirstOfMonth(iso) {
  return /^\d{4}-\d{2}-01$/.test(String(iso || ''));
}

// Batch 3：取得任務的依賴連結；無 links 時由 predecessors 衍生（FS+0，向後相容）
function taskLinks(task) {
  if (Array.isArray(task.links) && task.links.length > 0) return task.links;
  return (task.predecessors || []).map((p) => ({
    predecessor_task_id: p,
    dep_type: 'FS',
    lag_days: 0,
  }));
}

export default function GanttChart({
  tasks = [],
  region = 'TW',
  onTaskDurationChange,
  overCapacityDays,
  baseline,
  progress,
  dayDates,
}) {
  const draggable = typeof onTaskDurationChange === 'function';

  // Phase 9：基準線（計畫）查詢表 task_id -> {es, duration}；未提供時為空（不繪計畫條）
  const baselineMap = React.useMemo(() => {
    const map = {};
    if (baseline && Array.isArray(baseline.tasks)) {
      baseline.tasks.forEach((bt) => {
        if (bt && bt.task_id != null) {
          map[bt.task_id] = {
            es: Number(bt.es) || 0,
            duration: Math.max(0, Number(bt.duration) || 0),
          };
        }
      });
    }
    return map;
  }, [baseline]);

  // Phase 9：完成度查詢表 task_id -> percent_complete(0..100)；未提供時為空（不填色）
  const progressMap = React.useMemo(() => {
    const map = {};
    if (progress && typeof progress === 'object') {
      Object.keys(progress).forEach((k) => {
        const v = Number(progress[k]);
        if (Number.isFinite(v)) map[k] = Math.max(0, Math.min(100, v));
      });
    }
    return map;
  }, [progress]);

  const hasBaseline = Object.keys(baselineMap).length > 0;
  const hasProgress = progressMap && Object.keys(progressMap).length > 0;

  // 資源超載警示日（Phase 8）：去重後的整數集合；未提供時為空集合（不繪製）
  const overDaysSet = React.useMemo(() => {
    const set = new Set();
    if (Array.isArray(overCapacityDays)) {
      overCapacityDays.forEach((d) => {
        const n = Number(d);
        if (Number.isFinite(n)) set.add(n);
      });
    }
    return set;
  }, [overCapacityDays]);

  // 拖曳狀態：{ taskId, es, startX, originalDuration, previewDuration }
  const [drag, setDrag] = useState(null);
  const dragRef = useRef(null);
  dragRef.current = drag;

  // 指標移動：依位移換算對齊後工期（最小 1 天）
  const handlePointerMove = useCallback((e) => {
    const d = dragRef.current;
    if (!d) return;
    const deltaPx = e.clientX - d.startX;
    const deltaDays = Math.round(deltaPx / DAY_WIDTH);
    const next = Math.max(1, d.originalDuration + deltaDays);
    if (next !== d.previewDuration) {
      setDrag((prev) => (prev ? { ...prev, previewDuration: next } : prev));
    }
  }, []);

  // 指標放開：若對齊後工期變動則回呼，並清理事件
  const handlePointerUp = useCallback(() => {
    const d = dragRef.current;
    setDrag(null);
    window.removeEventListener('pointermove', handlePointerMove);
    window.removeEventListener('pointerup', handlePointerUp);
    if (d && d.previewDuration !== d.originalDuration) {
      onTaskDurationChange(d.taskId, d.previewDuration);
    }
  }, [handlePointerMove, onTaskDurationChange]);

  const startDrag = useCallback(
    (e, task) => {
      if (!draggable) return;
      e.preventDefault();
      e.stopPropagation();
      const originalDuration = Math.max(1, Number(task.duration) || 1);
      setDrag({
        taskId: task.task_id,
        es: Number(task.es) || 0,
        startX: e.clientX,
        originalDuration,
        previewDuration: originalDuration,
      });
      window.addEventListener('pointermove', handlePointerMove);
      window.addEventListener('pointerup', handlePointerUp);
    },
    [draggable, handlePointerMove, handlePointerUp],
  );

  // 卸載時保險：移除可能殘留的全域事件監聽
  useEffect(
    () => () => {
      window.removeEventListener('pointermove', handlePointerMove);
      window.removeEventListener('pointerup', handlePointerUp);
    },
    [handlePointerMove, handlePointerUp],
  );

  // 空資料防呆
  if (!tasks || tasks.length === 0) {
    return (
      <div style={{ padding: '16px', color: '#888', fontStyle: 'italic' }}>
        {t(region, 'loading')}…
      </div>
    );
  }

  // 以最大 ef 推算整體時間軸長度 (天數)；至少 1 天，避免空表頭
  const maxEf = tasks.reduce((m, tk) => Math.max(m, Number(tk.ef) || 0), 0);
  // 拖曳預覽時，可能讓條形超出原時間軸；以預覽結束日 (es + previewDuration)
  // 擴張時間軸，避免條形被裁切。
  const previewEnd = drag ? drag.es + drag.previewDuration : 0;
  // Phase 9：基準線（計畫）條形可能延伸至目前 ef 之後；以其最大結束日擴張時間軸。
  const baselineEnd = hasBaseline
    ? Object.values(baselineMap).reduce((m, b) => Math.max(m, b.es + b.duration), 0)
    : 0;
  const totalDays = Math.max(maxEf, previewEnd, baselineEnd, 1);

  // 產生日刻度陣列 [0,1,2,...,totalDays]
  const dayTicks = [];
  for (let d = 0; d <= totalDays; d += 1) {
    dayTicks.push(d);
  }

  const chartWidth = totalDays * DAY_WIDTH;

  // ---- Batch 3：實際日期軸（dayDates 提供時啟用）----
  const hasDates = Array.isArray(dayDates) && dayDates.length > 0;
  // 每 k 欄顯示一筆 MM/DD 避免擁擠（DAY_WIDTH=30px 下 2 欄一筆恰好）
  const DATE_LABEL_EVERY = 2;

  // ---- Batch 3：依賴箭頭（elbow connector）資料 ----
  // 錨點：FS/FF 起於 pred.ef*30（條形結束）；SS/SF 起於 pred.es*30（條形開始）。
  // 終點：FS/SS 至 succ 條形開始（es*30）；FF/SF 至 succ 條形結束（ef*30）。
  const rowIndexById = {};
  const taskById = {};
  tasks.forEach((tk, i) => {
    rowIndexById[tk.task_id] = i;
    taskById[tk.task_id] = tk;
  });
  const connectors = [];
  tasks.forEach((succ) => {
    taskLinks(succ).forEach((lnk, li) => {
      const pred = taskById[lnk.predecessor_task_id];
      if (!pred || pred.task_id === succ.task_id) return;
      const dep = String(lnk.dep_type || 'FS').toUpperCase();
      const lag = Number(lnk.lag_days) || 0;
      const fromStart = dep === 'SS' || dep === 'SF'; // 錨於前置條形「開始」
      const toEnd = dep === 'FF' || dep === 'SF'; // 指向後繼條形「結束」
      const sx = (fromStart ? Number(pred.es) || 0 : Number(pred.ef) || 0) * DAY_WIDTH;
      const ex = (toEnd ? Number(succ.ef) || 0 : Number(succ.es) || 0) * DAY_WIDTH;
      const sy = rowIndexById[pred.task_id] * ROW_HEIGHT + ROW_HEIGHT / 2;
      const ey = rowIndexById[succ.task_id] * ROW_HEIGHT + ROW_HEIGHT / 2;
      connectors.push({
        key: `dep-${pred.task_id}-${succ.task_id}-${li}`,
        sx,
        sy,
        ex,
        ey,
        critical: isCritical(pred) && isCritical(succ),
        label: `${pred.task_id} ${dep}${lag ? (lag > 0 ? `+${lag}` : `${lag}`) : ''} → ${succ.task_id}`,
      });
    });
  });

  return (
    <div
      style={{
        border: '1px solid #e0e0e0',
        borderRadius: '6px',
        overflowX: 'auto',
        background: '#fff',
      }}
    >
      {/* ===== 表頭：左側標題 + 日刻度軸 ===== */}
      <div style={{ display: 'flex', borderBottom: '2px solid #ddd' }}>
        <div
          style={{
            width: LABEL_WIDTH,
            minWidth: LABEL_WIDTH,
            flex: `0 0 ${LABEL_WIDTH}px`,
            padding: '6px 8px',
            fontWeight: 700,
            fontSize: '13px',
            color: '#333',
            boxSizing: 'border-box',
            borderRight: '1px solid #ddd',
            background: '#f7f9fc',
          }}
        >
          {t(region, 'task')}
        </div>
        {/* 日刻度表頭 */}
        <div
          style={{
            position: 'relative',
            height: '26px',
            width: chartWidth,
            minWidth: chartWidth,
            background: '#f7f9fc',
          }}
        >
          {dayTicks.map((d) => {
            const over = overDaysSet.has(d);
            // Batch 3：實際日期軸 — 有 dayDates 時刻度顯示 MM/DD（每 k 欄一筆 +
            // 每月 1 日必顯示並加粗標記）；tooltip 顯示完整 ISO 日期。
            const iso = hasDates && d < dayDates.length ? dayDates[d] : null;
            const monthStart = iso ? isFirstOfMonth(iso) : false;
            const showDateLabel = iso && (monthStart || d % DATE_LABEL_EVERY === 0);
            const tickTitle = [
              iso || null,
              over ? `${t(region, 'overCapacity')} · ${t(region, 'day')} ${d}` : null,
            ]
              .filter(Boolean)
              .join(' | ');
            return (
              <div
                key={`tick-${d}`}
                style={{
                  position: 'absolute',
                  left: d * DAY_WIDTH,
                  top: 0,
                  width: DAY_WIDTH,
                  height: '26px',
                  borderLeft: monthStart ? '2px solid #8fa3bd' : '1px solid #ececec',
                  fontSize: hasDates ? '9px' : '10px',
                  color: over ? '#c0392b' : monthStart ? '#34495e' : '#999',
                  fontWeight: over || monthStart ? 700 : 400,
                  textAlign: 'left',
                  paddingLeft: '2px',
                  boxSizing: 'border-box',
                  lineHeight: '26px',
                  background: over ? 'rgba(231, 76, 60, 0.14)' : 'transparent',
                }}
                title={tickTitle || undefined}
              >
                {hasDates ? (showDateLabel ? isoMonthDay(iso) : '') : d}
              </div>
            );
          })}
        </div>
      </div>

      {/* ===== 任務列 (bars) ===== */}
      <div style={{ position: 'relative' }}>
        {/* Batch 3：依賴箭頭 SVG 覆蓋層（肘形連接線 + 箭頭；要徑紅/一般灰） */}
        {connectors.length > 0 && (
          <svg
            className="gantt-dep-arrows"
            aria-hidden="true"
            width={chartWidth}
            height={tasks.length * ROW_HEIGHT}
            style={{
              position: 'absolute',
              top: 0,
              left: LABEL_WIDTH,
              width: chartWidth,
              height: tasks.length * ROW_HEIGHT,
              pointerEvents: 'none',
              zIndex: 4,
              overflow: 'visible',
            }}
          >
            <defs>
              {/* 箭頭頭部（orient=auto 沿線段方向）：要徑紅 / 一般灰 */}
              <marker
                id="gantt-arrow-crit"
                markerWidth="7"
                markerHeight="7"
                refX="6"
                refY="3.5"
                orient="auto"
                markerUnits="userSpaceOnUse"
              >
                <path d="M0,0 L7,3.5 L0,7 Z" fill="#e74c3c" />
              </marker>
              <marker
                id="gantt-arrow-norm"
                markerWidth="7"
                markerHeight="7"
                refX="6"
                refY="3.5"
                orient="auto"
                markerUnits="userSpaceOnUse"
              >
                <path d="M0,0 L7,3.5 L0,7 Z" fill="#95a5a6" />
              </marker>
            </defs>
            {connectors.map((c) => {
              // 肘形路徑：自錨點水平外伸 8px -> 垂直至後繼列 -> 水平至目標錨點
              const elbowX = c.sx + 8;
              const d = `M ${c.sx} ${c.sy} L ${elbowX} ${c.sy} L ${elbowX} ${c.ey} L ${c.ex} ${c.ey}`;
              return (
                <path
                  key={c.key}
                  d={d}
                  fill="none"
                  stroke={c.critical ? '#e74c3c' : '#95a5a6'}
                  strokeWidth={c.critical ? 1.8 : 1.4}
                  markerEnd={`url(#${c.critical ? 'gantt-arrow-crit' : 'gantt-arrow-norm'})`}
                >
                  <title>{c.label}</title>
                </path>
              );
            })}
          </svg>
        )}
        {/* 資源超載警示帶（Phase 8）：橫跨所有任務列，疊加於指定日欄位之上 */}
        {overDaysSet.size > 0 &&
          dayTicks
            .filter((d) => overDaysSet.has(d))
            .map((d) => (
              <div
                key={`over-${d}`}
                className="gantt-over-capacity"
                aria-hidden="true"
                style={{
                  position: 'absolute',
                  top: 0,
                  left: LABEL_WIDTH + d * DAY_WIDTH,
                  width: DAY_WIDTH,
                  height: tasks.length * ROW_HEIGHT,
                  background: 'rgba(231, 76, 60, 0.16)',
                  borderLeft: '1px dashed rgba(192, 57, 43, 0.55)',
                  borderRight: '1px dashed rgba(192, 57, 43, 0.55)',
                  pointerEvents: 'none',
                  zIndex: 3,
                }}
                title={`${t(region, 'overCapacity')} · ${t(region, 'day')} ${d}`}
              />
            ))}
        {tasks.map((task, idx) => {
          const critical = isCritical(task);
          const es = Number(task.es) || 0;
          const duration = Number(task.duration) || 0;
          const isDragging = drag && drag.taskId === task.task_id;
          // 拖曳中以預覽工期渲染條形寬度
          const effectiveDuration = isDragging ? drag.previewDuration : duration;
          const barColor = critical ? '#e74c3c' : '#3498db'; // 要徑紅 / 一般藍
          const barLeft = es * DAY_WIDTH;
          const barWidth = Math.max(
            effectiveDuration * DAY_WIDTH,
            effectiveDuration > 0 ? DAY_WIDTH : 4,
          );

          // Phase 9：基準線（計畫）條形位置/寬度（若有此任務基準資料）
          const bl = baselineMap[task.task_id];
          const hasBl = Boolean(bl);
          const blLeft = hasBl ? bl.es * DAY_WIDTH : 0;
          const blWidth = hasBl
            ? Math.max(bl.duration * DAY_WIDTH, bl.duration > 0 ? DAY_WIDTH : 4)
            : 0;
          // 落後判定：有基準線且目前結束日（es+duration）晚於計畫結束日（bl.es+bl.duration）
          const behindSchedule = hasBl && es + duration > bl.es + bl.duration;

          // Phase 9：完成百分比（0..100）；用於目前條形內填色寬度
          const pct = Object.prototype.hasOwnProperty.call(progressMap, task.task_id)
            ? progressMap[task.task_id]
            : null;
          const hasPct = pct != null;
          const fillWidth = hasPct ? (barWidth * pct) / 100 : 0;

          // Batch 3：實際日期 tooltip（計畫開工 = dayDates[es]；計畫完工 = dayDates[ef-1]）
          const startIso = hasDates && es < dayDates.length ? dayDates[es] : null;
          const finishIdx = es + Math.max(duration - 1, 0);
          const finishIso = hasDates && finishIdx < dayDates.length ? dayDates[finishIdx] : null;
          const dateTip =
            startIso && finishIso
              ? ` | ${t(region, 'plannedStart')} ${startIso} | ${t(region, 'plannedFinish')} ${finishIso}`
              : '';

          return (
            <div
              key={task.task_id || idx}
              style={{
                display: 'flex',
                height: ROW_HEIGHT,
                borderBottom: '1px solid #f2f2f2',
                background: idx % 2 === 0 ? '#fff' : '#fbfcfe',
              }}
            >
              {/* 左側標籤欄：task_id + task_name */}
              <div
                style={{
                  width: LABEL_WIDTH,
                  minWidth: LABEL_WIDTH,
                  flex: `0 0 ${LABEL_WIDTH}px`,
                  padding: '4px 8px',
                  boxSizing: 'border-box',
                  borderRight: '1px solid #eee',
                  overflow: 'hidden',
                  whiteSpace: 'nowrap',
                  textOverflow: 'ellipsis',
                  fontSize: '12px',
                }}
                title={`${task.task_id} ${task.task_name || ''}`}
              >
                <span style={{ fontWeight: 700, color: critical ? '#e74c3c' : '#2c3e50' }}>
                  {task.task_id}
                </span>{' '}
                <span style={{ color: '#666' }}>{task.task_name}</span>
              </div>

              {/* 條形繪圖區 */}
              <div
                style={{
                  position: 'relative',
                  width: chartWidth,
                  minWidth: chartWidth,
                  height: ROW_HEIGHT,
                }}
              >
                {/* 背景日格線 */}
                {dayTicks.map((d) => (
                  <div
                    key={`grid-${task.task_id}-${d}`}
                    style={{
                      position: 'absolute',
                      left: d * DAY_WIDTH,
                      top: 0,
                      width: 0,
                      height: ROW_HEIGHT,
                      borderLeft: '1px solid #f4f4f4',
                    }}
                  />
                ))}

                {/* Phase 9：基準線（計畫）細條形，繪於目前條形之下／之後（淡灰） */}
                {hasBl && !isDragging && (
                  <div
                    className="gantt-baseline-bar"
                    aria-hidden="true"
                    style={{
                      position: 'absolute',
                      left: blLeft,
                      top: (ROW_HEIGHT - BAR_HEIGHT) / 2 + BAR_HEIGHT - 5,
                      width: blWidth,
                      height: 6,
                      background: 'repeating-linear-gradient(45deg, #b0b8c4, #b0b8c4 4px, #c8cfd8 4px, #c8cfd8 8px)',
                      borderRadius: '3px',
                      zIndex: 1,
                    }}
                    title={`${t(region, 'baseline')} | ${t(region, 'plannedVsActual')} | ES ${bl.es} · ${bl.duration} ${t(region, 'days')}`}
                  />
                )}

                {/* 任務條形 */}
                <div
                  className={`gantt-bar ${critical ? 'critical' : 'normal'}${
                    isDragging ? ' dragging' : ''
                  }${behindSchedule ? ' behind' : ''}`}
                  style={{
                    position: 'absolute',
                    left: barLeft,
                    top: (ROW_HEIGHT - BAR_HEIGHT) / 2,
                    width: barWidth,
                    height: BAR_HEIGHT,
                    background: barColor,
                    borderRadius: '4px',
                    color: '#fff',
                    fontSize: '11px',
                    lineHeight: `${BAR_HEIGHT}px`,
                    paddingLeft: '6px',
                    paddingRight: draggable ? '12px' : '6px',
                    boxSizing: 'border-box',
                    whiteSpace: 'nowrap',
                    overflow: 'hidden',
                    boxShadow: '0 1px 2px rgba(0,0,0,0.15)',
                    // 落後（且非拖曳中）時加紅色外框淡染標示
                    outline: behindSchedule && !isDragging ? '2px solid rgba(192, 57, 43, 0.85)' : 'none',
                    zIndex: 2,
                  }}
                  title={`${task.task_id} | ${t(region, 'duration')}: ${duration} ${t(
                    region,
                    'days'
                  )} | ES ${es} EF ${task.ef} | ${t(region, 'floatTime')}: ${task.float_time}${dateTip}${
                    hasPct ? ` | ${t(region, 'percentComplete')}: ${pct}%` : ''
                  }${behindSchedule ? ` | ${t(region, 'behindSchedule')}` : ''}${
                    draggable ? ` | ${t(region, 'dragHint')}` : ''
                  }`}
                >
                  {/* Phase 9：完成百分比填色（較深色，自左填入），未提供 progress 時不繪 */}
                  {hasPct && !isDragging && (
                    <div
                      className="gantt-progress-fill"
                      aria-hidden="true"
                      style={{
                        position: 'absolute',
                        left: 0,
                        top: 0,
                        width: fillWidth,
                        height: '100%',
                        background: 'rgba(0, 0, 0, 0.28)',
                        borderTopLeftRadius: '4px',
                        borderBottomLeftRadius: '4px',
                        borderTopRightRadius: pct >= 100 ? '4px' : 0,
                        borderBottomRightRadius: pct >= 100 ? '4px' : 0,
                        pointerEvents: 'none',
                      }}
                    />
                  )}

                  {/* 要徑火焰標記（置於填色之上） */}
                  <span style={{ position: 'relative', zIndex: 1 }}>
                    {critical ? '🔥 ' : ''}
                    {effectiveDuration}
                    {t(region, 'day')}
                    {hasPct ? ` · ${pct}%` : ''}
                  </span>

                  {/* 拖曳把手（僅在提供 onTaskDurationChange 時渲染） */}
                  {draggable && (
                    <span
                      className="gantt-resize-handle"
                      onPointerDown={(e) => startDrag(e, task)}
                      role="separator"
                      aria-label={t(region, 'dragHint')}
                      title={t(region, 'dragHint')}
                    />
                  )}
                </div>

                {/* 拖曳即時預覽標籤：N 天/天 */}
                {isDragging && (
                  <div
                    className="gantt-drag-preview"
                    style={{
                      position: 'absolute',
                      left: barLeft + barWidth + 6,
                      top: (ROW_HEIGHT - 18) / 2,
                    }}
                  >
                    {drag.previewDuration} {t(region, 'days')}
                  </div>
                )}
              </div>
            </div>
          );
        })}
      </div>

      {/* ===== 圖例 (legend) ===== */}
      <div
        style={{
          display: 'flex',
          gap: '16px',
          padding: '8px 12px',
          borderTop: '1px solid #eee',
          fontSize: '12px',
          color: '#555',
          background: '#fafafa',
        }}
      >
        <span style={{ display: 'inline-flex', alignItems: 'center', gap: '6px' }}>
          <span
            style={{
              display: 'inline-block',
              width: '14px',
              height: '14px',
              background: '#e74c3c',
              borderRadius: '3px',
            }}
          />
          🔥 {t(region, 'criticalPath')}
        </span>
        <span style={{ display: 'inline-flex', alignItems: 'center', gap: '6px' }}>
          <span
            style={{
              display: 'inline-block',
              width: '14px',
              height: '14px',
              background: '#3498db',
              borderRadius: '3px',
            }}
          />
          {t(region, 'floatTime')} &gt; 0
        </span>

        {/* Phase 9：計畫條（基準線）圖例 */}
        {hasBaseline && (
          <span style={{ display: 'inline-flex', alignItems: 'center', gap: '6px' }}>
            <span
              style={{
                display: 'inline-block',
                width: '14px',
                height: '6px',
                background:
                  'repeating-linear-gradient(45deg, #b0b8c4, #b0b8c4 4px, #c8cfd8 4px, #c8cfd8 8px)',
                borderRadius: '3px',
              }}
            />
            {t(region, 'baseline')} ({t(region, 'plannedVsActual')})
          </span>
        )}

        {/* Phase 9：完成度填色圖例 */}
        {hasProgress && (
          <span style={{ display: 'inline-flex', alignItems: 'center', gap: '6px' }}>
            <span
              style={{
                display: 'inline-block',
                width: '14px',
                height: '14px',
                background: 'rgba(0, 0, 0, 0.28)',
                borderRadius: '3px',
              }}
            />
            {t(region, 'percentComplete')}
          </span>
        )}
      </div>
    </div>
  );
}
