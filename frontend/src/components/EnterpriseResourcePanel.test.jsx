// Pro Batch E Feature 1：EnterpriseResourcePanel — smoke render 測試（mock store，同 HealthPanel.test.jsx 手法）
// 驗證：
//   1. 掛載時呼叫 store.loadPool() + store.loadAllocation()
//   2. store.pool 有資料時渲染資源池編輯器列（resource_type/name）
//   3. store.allocation 有資料時渲染熱區圖（週欄 + 超額分配儲存格）
import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render } from '@testing-library/react';
import { t } from '../i18n/index.js';

// ---- mock store（hoisted 可變狀態物件，各測試於 beforeEach 重建）----
const { mockState } = vi.hoisted(() => ({ mockState: {} }));

vi.mock('../store/scheduleStore', () => {
  const useScheduleStore = () => mockState;
  useScheduleStore.getState = () => mockState;
  return {
    useScheduleStore,
    isLoading: (state, scope) => Boolean(state && state.loading && state.loading[scope]),
    getError: (state, scope) => (state && state.errors && state.errors[scope]) || null,
  };
});

import EnterpriseResourcePanel from './EnterpriseResourcePanel.jsx';

function buildState(overrides = {}) {
  return {
    loading: {},
    errors: {},
    role: 'editor',
    pool: [],
    allocation: null,
    loadPool: vi.fn().mockResolvedValue([]),
    savePool: vi.fn().mockResolvedValue([]),
    loadAllocation: vi.fn().mockResolvedValue(null),
    ...overrides,
  };
}

beforeEach(() => {
  Object.keys(mockState).forEach((k) => delete mockState[k]);
});

describe('EnterpriseResourcePanel render smoke test', () => {
  it('calls loadPool + loadAllocation on mount', () => {
    Object.assign(mockState, buildState());
    render(<EnterpriseResourcePanel region="TW" />);
    expect(mockState.loadPool).toHaveBeenCalledTimes(1);
    expect(mockState.loadAllocation).toHaveBeenCalledTimes(1);
  });

  it('renders pool editor rows once store.pool is populated', () => {
    Object.assign(
      mockState,
      buildState({
        pool: [
          { resource_type: 'crane', name: 'Tower Crane', category: 'equipment', capacity: 2, unit_cost: 3200, work_days: '1111100' },
          { resource_type: 'manpower', name: 'Manpower', category: 'labor', capacity: 40, unit_cost: 260, work_days: '1111110' },
        ],
      }),
    );
    const { getByDisplayValue } = render(<EnterpriseResourcePanel region="TW" />);
    expect(getByDisplayValue('crane')).toBeInTheDocument();
    expect(getByDisplayValue('Tower Crane')).toBeInTheDocument();
    expect(getByDisplayValue('manpower')).toBeInTheDocument();
  });

  it('renders the allocation heatmap with week columns and an over-allocated (red) cell', () => {
    Object.assign(
      mockState,
      buildState({
        allocation: {
          weeks: ['2026-W01', '2026-W02'],
          resources: [
            {
              resource_type: 'crane',
              name: 'Tower Crane',
              category: 'equipment',
              capacity: 2,
              unit_cost: 3200,
              by_week: { '2026-W01': 3, '2026-W02': 1 },
              peak: 3,
              over_weeks: ['2026-W01'],
            },
          ],
          unscheduled_projects: ['P9'],
          warnings: ['P9 未設定開工日，已排除於分配計算'],
        },
      }),
    );
    const { getByText, container } = render(<EnterpriseResourcePanel region="TW" />);
    // week column header (short form, year prefix stripped)
    expect(getByText('W01')).toBeInTheDocument();
    expect(getByText('W02')).toBeInTheDocument();
    // resource row label + peak/capacity
    expect(getByText('Tower Crane')).toBeInTheDocument();
    // over-allocated cell renders the demand value and carries a red-tinted background
    const overCell = Array.from(container.querySelectorAll('td')).find(
      (td) => td.textContent === '3' && /rgba\(231, 76, 60/.test(td.getAttribute('style') || ''),
    );
    expect(overCell).toBeTruthy();
    // unscheduled projects + warnings surfaced
    expect(getByText(t('TW', 'unscheduledProjects'), { exact: false })).toBeInTheDocument();
    expect(getByText('P9 未設定開工日，已排除於分配計算')).toBeInTheDocument();
  });

  it('hides pool add/save controls for viewer role (read-only)', () => {
    Object.assign(mockState, buildState({ role: 'viewer' }));
    const { queryByText } = render(<EnterpriseResourcePanel region="TW" />);
    expect(queryByText(`+ ${t('TW', 'addResource')}`)).not.toBeInTheDocument();
  });
});
