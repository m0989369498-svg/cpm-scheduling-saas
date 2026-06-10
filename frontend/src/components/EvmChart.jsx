import React from 'react';
import { t } from '../i18n';

/**
 * EvmChart 實獲值管理圖（純 SVG，無圖表函式庫；對齊 SCurveChart 繪製風格）
 *
 * props:
 *   pvCurve         : list[{day:int, pv:float}]   累積計畫值 (PV) S 曲線（隨日遞增）
 *   ev              : float    資料日當下之實獲值 (EV)
 *   ac              : float    資料日當下之實際成本 (AC)
 *   dataDate        : int      資料日（以垂直線標示）；EV/AC 點繪於此日
 *   projectDuration : int      基準線總工期（x 軸右界回退值，避免 pvCurve 為空時除以 0）
 *   region          : 'TW' | 'CN'   i18n 語系（預設 'TW'）
 *
 * 繪製：x = 天 (day)，y = 金額 0..maxVal。
 *   - PV 以折線 + 面積填色繪製累積計畫值
 *   - data_date 以紅色虛線垂直標示
 *   - EV（綠）、AC（橘）以資料日為 x，畫短柱 + 點 + 標籤
 *   - 圖例：PV(計畫) / EV(實獲) / AC(實際)
 */

const WIDTH = 520; // SVG 視圖寬
const HEIGHT = 260; // SVG 視圖高
const PAD_L = 56; // 左內距（y 軸金額刻度較寬）
const PAD_R = 16; // 右內距
const PAD_T = 16; // 上內距
const PAD_B = 34; // 下內距（x 軸刻度）

// 金額短格式：1234567 -> 1.23M / 12345 -> 12.3k
function fmtMoney(v) {
  const n = Number(v) || 0;
  const abs = Math.abs(n);
  if (abs >= 1e6) return `${(n / 1e6).toFixed(2)}M`;
  if (abs >= 1e3) return `${(n / 1e3).toFixed(1)}k`;
  return `${Math.round(n)}`;
}

const COLOR_PV = '#2980b9'; // 計畫值（藍）
const COLOR_EV = '#27ae60'; // 實獲值（綠）
const COLOR_AC = '#e67e22'; // 實際成本（橘）
const COLOR_DD = '#c0392b'; // 資料日（紅）

export default function EvmChart({
  pvCurve = [],
  ev = 0,
  ac = 0,
  dataDate = 0,
  projectDuration = 0,
  region = 'TW',
}) {
  const points = Array.isArray(pvCurve)
    ? pvCurve.filter((pt) => pt && Number.isFinite(Number(pt.day)) && Number.isFinite(Number(pt.pv)))
    : [];

  if (points.length === 0) {
    return (
      <div style={{ padding: '16px', color: '#888', fontStyle: 'italic' }}>
        {t(region, 'evm')} — {t(region, 'loading')}…
      </div>
    );
  }

  const plotW = WIDTH - PAD_L - PAD_R;
  const plotH = HEIGHT - PAD_T - PAD_B;

  // x 軸範圍（天）：包含 pvCurve 各日、data_date 與 projectDuration
  const days = points.map((pt) => Number(pt.day));
  let minX = Math.min(0, ...days);
  let maxX = Math.max(...days, Number(projectDuration) || 0, Number(dataDate) || 0);
  if (maxX <= minX) maxX = minX + 1; // 避免除以 0

  // y 軸範圍（金額）：包含 PV 各點、EV、AC，並自 0 起算
  const pvVals = points.map((pt) => Number(pt.pv));
  let maxVal = Math.max(0, ...pvVals, Number(ev) || 0, Number(ac) || 0);
  if (maxVal <= 0) maxVal = 1; // 避免除以 0

  // 座標轉換：day -> svg x；amount(0..maxVal) -> svg y（上方=maxVal）
  const xOf = (d) => PAD_L + ((Number(d) - minX) / (maxX - minX)) * plotW;
  const yOf = (v) => PAD_T + (1 - Math.max(0, Math.min(maxVal, Number(v))) / maxVal) * plotH;

  // PV 折線 path
  const linePath = points
    .map((pt, i) => `${i === 0 ? 'M' : 'L'} ${xOf(pt.day).toFixed(1)} ${yOf(pt.pv).toFixed(1)}`)
    .join(' ');

  // PV 面積 path（折線下方填色）
  const baseY = yOf(0);
  const areaPath =
    `M ${xOf(points[0].day).toFixed(1)} ${baseY.toFixed(1)} ` +
    points.map((pt) => `L ${xOf(pt.day).toFixed(1)} ${yOf(pt.pv).toFixed(1)}`).join(' ') +
    ` L ${xOf(points[points.length - 1].day).toFixed(1)} ${baseY.toFixed(1)} Z`;

  // y 軸格線/刻度（0/25/50/75/100% of maxVal）
  const yTicks = [0, 0.25, 0.5, 0.75, 1];
  // x 軸刻度：最多 6 個均勻整數刻度
  const xTickCount = Math.min(6, Math.max(2, Math.round(maxX - minX) + 1));
  const xTicks = [];
  for (let i = 0; i < xTickCount; i += 1) {
    const v = minX + ((maxX - minX) * i) / (xTickCount - 1);
    xTicks.push(Math.round(v));
  }

  const ddNum = Number(dataDate);
  const hasDD = Number.isFinite(ddNum);
  const ddX = hasDD ? xOf(ddNum) : null;
  const evNum = Number(ev) || 0;
  const acNum = Number(ac) || 0;
  // EV/AC 短柱寬度（資料日左右各偏移）
  const barW = 7;
  // 標籤對齊：資料日偏右則靠右，避免標籤超出右界
  const labelAnchor = hasDD && ddX > PAD_L + plotW / 2 ? 'end' : 'start';
  const labelDx = labelAnchor === 'end' ? -6 : 6;

  return (
    <div style={{ width: '100%', overflowX: 'auto' }}>
      <svg
        viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
        width="100%"
        style={{ maxWidth: `${WIDTH}px`, fontFamily: 'sans-serif' }}
        role="img"
        aria-label={t(region, 'evm')}
      >
        {/* 背景 */}
        <rect x={0} y={0} width={WIDTH} height={HEIGHT} fill="#ffffff" />

        {/* y 軸格線 + 金額刻度 */}
        {yTicks.map((f) => {
          const v = maxVal * f;
          const y = yOf(v);
          return (
            <g key={`y-${f}`}>
              <line x1={PAD_L} y1={y} x2={WIDTH - PAD_R} y2={y} stroke="#eef1f4" strokeWidth={1} />
              <text x={PAD_L - 6} y={y + 3} fontSize={10} fill="#999" textAnchor="end">
                {fmtMoney(v)}
              </text>
            </g>
          );
        })}

        {/* x 軸刻度 */}
        {xTicks.map((d, i) => {
          const x = xOf(d);
          return (
            <g key={`x-${d}-${i}`}>
              <line x1={x} y1={PAD_T} x2={x} y2={HEIGHT - PAD_B} stroke="#f6f7f9" strokeWidth={1} />
              <text x={x} y={HEIGHT - PAD_B + 14} fontSize={10} fill="#999" textAnchor="middle">
                {d}
              </text>
            </g>
          );
        })}

        {/* x 軸標題（天） */}
        <text x={PAD_L + plotW / 2} y={HEIGHT - 4} fontSize={10} fill="#777" textAnchor="middle">
          {t(region, 'duration')} ({t(region, 'days')})
        </text>

        {/* 軸線 */}
        <line x1={PAD_L} y1={PAD_T} x2={PAD_L} y2={HEIGHT - PAD_B} stroke="#ccc" strokeWidth={1} />
        <line
          x1={PAD_L}
          y1={HEIGHT - PAD_B}
          x2={WIDTH - PAD_R}
          y2={HEIGHT - PAD_B}
          stroke="#ccc"
          strokeWidth={1}
        />

        {/* PV 面積填色 + 折線 */}
        <path d={areaPath} fill="rgba(41, 128, 185, 0.12)" stroke="none" />
        <path d={linePath} fill="none" stroke={COLOR_PV} strokeWidth={2} />

        {/* PV 資料點 */}
        {points.map((pt, i) => (
          <circle key={`pv-${i}`} cx={xOf(pt.day)} cy={yOf(pt.pv)} r={1.8} fill={COLOR_PV}>
            <title>{`${t(region, 'day')} ${pt.day} · PV ${fmtMoney(pt.pv)}`}</title>
          </circle>
        ))}

        {/* 資料日垂直虛線 */}
        {hasDD && (
          <g>
            <line
              x1={ddX}
              y1={PAD_T}
              x2={ddX}
              y2={HEIGHT - PAD_B}
              stroke={COLOR_DD}
              strokeWidth={1.5}
              strokeDasharray="6 3"
            />
            <text
              x={ddX}
              y={PAD_T + 10}
              fontSize={10}
              fill={COLOR_DD}
              textAnchor={labelAnchor}
              dx={labelDx}
            >
              {t(region, 'dataDate')} · {dataDate}
            </text>
          </g>
        )}

        {/* EV / AC 短柱 + 點 + 標籤（繪於資料日 x） */}
        {hasDD && (
          <g>
            {/* EV 柱（綠，資料日左側） */}
            <rect
              x={ddX - barW - 1}
              y={yOf(evNum)}
              width={barW}
              height={Math.max(0, baseY - yOf(evNum))}
              fill={COLOR_EV}
              opacity={0.85}
            >
              <title>{`EV ${fmtMoney(evNum)}`}</title>
            </rect>
            <circle cx={ddX - barW / 2 - 1} cy={yOf(evNum)} r={3} fill={COLOR_EV} />
            {/* AC 柱（橘，資料日右側） */}
            <rect
              x={ddX + 1}
              y={yOf(acNum)}
              width={barW}
              height={Math.max(0, baseY - yOf(acNum))}
              fill={COLOR_AC}
              opacity={0.85}
            >
              <title>{`AC ${fmtMoney(acNum)}`}</title>
            </rect>
            <circle cx={ddX + barW / 2 + 1} cy={yOf(acNum)} r={3} fill={COLOR_AC} />
            {/* EV/AC 數值標籤 */}
            <text
              x={ddX}
              y={yOf(Math.max(evNum, acNum)) - 6}
              fontSize={10}
              fill="#2c3e50"
              textAnchor={labelAnchor}
              dx={labelDx}
            >
              EV {fmtMoney(evNum)} · AC {fmtMoney(acNum)}
            </text>
          </g>
        )}

        {/* 圖例 PV / EV / AC */}
        <g fontSize={10} fill="#555">
          <rect x={PAD_L} y={PAD_T - 2} width={10} height={10} fill={COLOR_PV} rx={2} />
          <text x={PAD_L + 14} y={PAD_T + 7}>
            {t(region, 'plannedValue')} (PV)
          </text>
          <rect x={PAD_L + 110} y={PAD_T - 2} width={10} height={10} fill={COLOR_EV} rx={2} />
          <text x={PAD_L + 124} y={PAD_T + 7}>
            {t(region, 'earnedValue')} (EV)
          </text>
          <rect x={PAD_L + 220} y={PAD_T - 2} width={10} height={10} fill={COLOR_AC} rx={2} />
          <text x={PAD_L + 234} y={PAD_T + 7}>
            {t(region, 'actualCost')} (AC)
          </text>
        </g>
      </svg>
    </div>
  );
}
