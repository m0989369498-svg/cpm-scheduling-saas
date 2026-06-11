// QUAL-2 (Batch 4)：GanttChart 渲染單元測試
// 驗證：要徑條形顏色/類別、條形幾何（left = es*30 / width = duration*30）、
// 依賴箭頭 SVG 覆蓋層（每條 link 一條 path）、實際日期軸（dayDates -> MM/DD）。
import React from 'react'
import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/react'
import GanttChart from './GanttChart.jsx'

const DAY_WIDTH = 30

// 三任務骨架：A(要徑) -> B(要徑, FS)、A -> C(非要徑, SS+1)
function sampleTasks() {
  return [
    {
      task_id: 'A',
      task_name: 'Design',
      duration: 2,
      es: 0,
      ef: 2,
      ls: 0,
      lf: 2,
      float_time: 0,
      is_critical: true,
      predecessors: [],
      links: [],
    },
    {
      task_id: 'B',
      task_name: 'Build',
      duration: 3,
      es: 2,
      ef: 5,
      ls: 2,
      lf: 5,
      float_time: 0,
      is_critical: true,
      predecessors: ['A'],
      links: [{ predecessor_task_id: 'A', dep_type: 'FS', lag_days: 0 }],
    },
    {
      task_id: 'C',
      task_name: 'Paint',
      duration: 1,
      es: 2,
      ef: 3,
      ls: 4,
      lf: 5,
      float_time: 2,
      is_critical: false,
      predecessors: ['A'],
      links: [{ predecessor_task_id: 'A', dep_type: 'SS', lag_days: 1 }],
    },
  ]
}

// jsdom 將 hex 正規化為 rgb()；兩種寫法皆接受
function isCriticalRed(bar) {
  const bg = bar.style.background || bar.style.backgroundColor || ''
  return bg === '#e74c3c' || bg.includes('rgb(231, 76, 60)')
}

describe('GanttChart bars', () => {
  it('marks critical tasks with the critical class and red color', () => {
    const { container } = render(<GanttChart tasks={sampleTasks()} region="TW" />)
    const bars = container.querySelectorAll('.gantt-bar')
    expect(bars).toHaveLength(3)
    expect(bars[0].className).toContain('critical')
    expect(bars[1].className).toContain('critical')
    expect(bars[2].className).toContain('normal')
    expect(isCriticalRed(bars[0])).toBe(true)
    expect(isCriticalRed(bars[2])).toBe(false)
  })

  it('positions each bar at left = es*30 with width = duration*30', () => {
    const { container } = render(<GanttChart tasks={sampleTasks()} region="TW" />)
    const bars = container.querySelectorAll('.gantt-bar')
    // A: es=0 duration=2
    expect(bars[0].style.left).toBe('0px')
    expect(bars[0].style.width).toBe(`${2 * DAY_WIDTH}px`)
    // B: es=2 duration=3
    expect(bars[1].style.left).toBe(`${2 * DAY_WIDTH}px`)
    expect(bars[1].style.width).toBe(`${3 * DAY_WIDTH}px`)
    // C: es=2 duration=1
    expect(bars[2].style.left).toBe(`${2 * DAY_WIDTH}px`)
    expect(bars[2].style.width).toBe(`${1 * DAY_WIDTH}px`)
  })
})

describe('GanttChart dependency arrows', () => {
  it('renders one svg connector path per dependency link', () => {
    const { container } = render(<GanttChart tasks={sampleTasks()} region="TW" />)
    const svg = container.querySelector('svg.gantt-dep-arrows')
    expect(svg).not.toBeNull()
    // 連接線 path 帶 marker-end（marker defs 內的箭頭 path 不帶）
    const connectors = svg.querySelectorAll('path[marker-end]')
    expect(connectors).toHaveLength(2)
  })

  it('colors the connector red when both endpoints are critical', () => {
    const { container } = render(<GanttChart tasks={sampleTasks()} region="TW" />)
    const connectors = container.querySelectorAll('svg.gantt-dep-arrows path[marker-end]')
    const strokes = Array.from(connectors).map((p) => p.getAttribute('stroke'))
    // A->B 兩端皆要徑 = 紅；A->C 一端非要徑 = 灰
    expect(strokes).toContain('#e74c3c')
    expect(strokes).toContain('#95a5a6')
  })

  it('renders no arrow overlay when tasks carry no links/predecessors', () => {
    const tasks = sampleTasks().map((t0) => ({ ...t0, predecessors: [], links: [] }))
    const { container } = render(<GanttChart tasks={tasks} region="TW" />)
    expect(container.querySelector('svg.gantt-dep-arrows')).toBeNull()
  })
})

describe('GanttChart date axis', () => {
  it('shows numeric day ticks when dayDates is not provided', () => {
    const { container } = render(<GanttChart tasks={sampleTasks()} region="TW" />)
    const text = container.textContent
    expect(text).toContain('0')
    expect(text).not.toContain('07/01')
  })

  it('shows MM/DD tick labels when dayDates is provided', () => {
    const dayDates = [
      '2026-07-01',
      '2026-07-02',
      '2026-07-03',
      '2026-07-04',
      '2026-07-05',
      '2026-07-06',
    ]
    const { container } = render(
      <GanttChart tasks={sampleTasks()} region="TW" dayDates={dayDates} />,
    )
    const text = container.textContent
    // 每 2 欄一筆：d=0 -> 07/01、d=2 -> 07/03、d=4 -> 07/05
    expect(text).toContain('07/01')
    expect(text).toContain('07/03')
    expect(text).toContain('07/05')
  })
})
