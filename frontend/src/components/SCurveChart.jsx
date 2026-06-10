import React from 'react';
import { t } from '../i18n';

/**
 * SCurveChart 蒙地卡羅 S 曲線（純 SVG，無圖表函式庫）
 *
 * props:
 *   sCurve   : list[{duration:int, probability:float}]   累積完工機率（非遞減，0..1）
 *   p50      : int    中位工期（50% 完工）— 以虛線標示
 *   p90      : int    90% 完工工期 — 以虛線標示
 *   deadline : int|null   合約工期 — 若提供以紅色虛線標示
 *   region   : 'TW' | 'CN'   i18n 語系（預設 'TW'）
 *
 * 繪製：x = 工期(duration)，y = 累積機率 0..100%。
 *   折線連接各 (duration, probability)；p50/p90/deadline 以垂直參考線標記。
 */

const WIDTH = 520; // SVG 視圖寬
const HEIGHT = 260; // SVG 視圖高
const PAD_L = 44; // 左內距（y 軸刻度）
const PAD_R = 16; // 右內距
const PAD_T = 16; // 上內距
const PAD_B = 34; // 下內距（x 軸刻度）

export default function SCurveChart({ sCurve = [], p50, p90, deadline = null, region = 'TW' }) {
  const points = Array.isArray(sCurve) ? sCurve.filter((pt) => pt && Number.isFinite(Number(pt.duration))) : [];

  if (points.length === 0) {
    return (
      <div style={{ padding: '16px', color: '#888', fontStyle: 'italic' }}>
        {t(region, 'sCurve')} — {t(region, 'loading')}…
      </div>
    );
  }

  const plotW = WIDTH - PAD_L - PAD_R;
  const plotH = HEIGHT - PAD_T - PAD_B;

  // x 軸範圍：包含曲線工期與 deadline（若有），確保標記在圖內
  const durations = points.map((pt) => Number(pt.duration));
  let minX = Math.min(...durations);
  let maxX = Math.max(...durations);
  const refs = [p50, p90, deadline].map((v) => Number(v)).filter((v) => Number.isFinite(v));
  refs.forEach((v) => {
    if (v < minX) minX = v;
    if (v > maxX) maxX = v;
  });
  if (maxX === minX) maxX = minX + 1; // 避免除以 0

  // 座標轉換：duration -> svg x；probability(0..1) -> svg y（上方=1.0）
  const xOf = (d) => PAD_L + ((Number(d) - minX) / (maxX - minX)) * plotW;
  const yOf = (p) => PAD_T + (1 - Math.max(0, Math.min(1, Number(p)))) * plotH;

  // 折線 path
  const linePath = points
    .map((pt, i) => `${i === 0 ? 'M' : 'L'} ${xOf(pt.duration).toFixed(1)} ${yOf(pt.probability).toFixed(1)}`)
    .join(' ');

  // 面積 path（折線下方填色），由首點底部 -> 折線 -> 末點底部 閉合
  const baseY = yOf(0);
  const areaPath =
    `M ${xOf(points[0].duration).toFixed(1)} ${baseY.toFixed(1)} ` +
    points.map((pt) => `L ${xOf(pt.duration).toFixed(1)} ${yOf(pt.probability).toFixed(1)}`).join(' ') +
    ` L ${xOf(points[points.length - 1].duration).toFixed(1)} ${baseY.toFixed(1)} Z`;

  // y 軸格線/刻度（0/25/50/75/100%）
  const yTicks = [0, 0.25, 0.5, 0.75, 1];
  // x 軸刻度：最多 6 個均勻整數刻度
  const xTickCount = Math.min(6, Math.max(2, Math.round(maxX - minX) + 1));
  const xTicks = [];
  for (let i = 0; i < xTickCount; i += 1) {
    const v = minX + ((maxX - minX) * i) / (xTickCount - 1);
    xTicks.push(Math.round(v));
  }

  // 垂直參考線（p50 藍 / p90 橘 / deadline 紅）
  const markers = [];
  if (Number.isFinite(Number(p50))) {
    markers.push({ x: Number(p50), color: '#2980b9', label: `P50 · ${p50}`, dash: '4 3' });
  }
  if (Number.isFinite(Number(p90))) {
    markers.push({ x: Number(p90), color: '#e67e22', label: `P90 · ${p90}`, dash: '4 3' });
  }
  if (Number.isFinite(Number(deadline))) {
    markers.push({
      x: Number(deadline),
      color: '#c0392b',
      label: `${t(region, 'contractDeadline')} · ${deadline}`,
      dash: '6 3',
    });
  }

  return (
    <div style={{ width: '100%', overflowX: 'auto' }}>
      <svg
        viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
        width="100%"
        style={{ maxWidth: `${WIDTH}px`, fontFamily: 'sans-serif' }}
        role="img"
        aria-label={t(region, 'sCurve')}
      >
        {/* 背景 */}
        <rect x={0} y={0} width={WIDTH} height={HEIGHT} fill="#ffffff" />

        {/* y 軸格線 + 刻度 */}
        {yTicks.map((p) => {
          const y = yOf(p);
          return (
            <g key={`y-${p}`}>
              <line x1={PAD_L} y1={y} x2={WIDTH - PAD_R} y2={y} stroke="#eef1f4" strokeWidth={1} />
              <text x={PAD_L - 6} y={y + 3} fontSize={10} fill="#999" textAnchor="end">
                {Math.round(p * 100)}%
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
        <text
          x={PAD_L + plotW / 2}
          y={HEIGHT - 4}
          fontSize={10}
          fill="#777"
          textAnchor="middle"
        >
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

        {/* 面積填色 + 折線 */}
        <path d={areaPath} fill="rgba(41, 128, 185, 0.12)" stroke="none" />
        <path d={linePath} fill="none" stroke="#2980b9" strokeWidth={2} />

        {/* 資料點 */}
        {points.map((pt, i) => (
          <circle
            key={`pt-${i}`}
            cx={xOf(pt.duration)}
            cy={yOf(pt.probability)}
            r={2}
            fill="#2980b9"
          >
            <title>{`${pt.duration} ${t(region, 'days')} · ${Math.round(Number(pt.probability) * 100)}%`}</title>
          </circle>
        ))}

        {/* 垂直參考線：p50 / p90 / deadline */}
        {markers.map((m, i) => {
          const x = xOf(m.x);
          return (
            <g key={`mk-${i}`}>
              <line
                x1={x}
                y1={PAD_T}
                x2={x}
                y2={HEIGHT - PAD_B}
                stroke={m.color}
                strokeWidth={1.5}
                strokeDasharray={m.dash}
              />
              <text
                x={x}
                y={PAD_T + 10 + i * 12}
                fontSize={10}
                fill={m.color}
                textAnchor={x > PAD_L + plotW / 2 ? 'end' : 'start'}
                dx={x > PAD_L + plotW / 2 ? -4 : 4}
              >
                {m.label}
              </text>
            </g>
          );
        })}
      </svg>
    </div>
  );
}
