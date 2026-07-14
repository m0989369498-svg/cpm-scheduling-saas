// Pro Batch D Feature 1：CostPanel — smoke render 測試（mock store，同 FieldMode.test.jsx 手法）
// 驗證：
//   1. 掛載時（有 currentProject）呼叫 store.loadCost()
//   2. store.cost 有資料時渲染總成本卡片 + 每任務成本表格列
//   3. 無 currentProject 時顯示提示，不呼叫 loadCost
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

import CostPanel from './CostPanel.jsx';

function buildState(overrides = {}) {
  return {
    loading: {},
    errors: {},
    currentProject: { project_id: 'P1', project_name: 'Demo' },
    cost: null,
    loadCost: vi.fn().mockResolvedValue(null),
    ...overrides,
  };
}

beforeEach(() => {
  Object.keys(mockState).forEach((k) => delete mockState[k]);
});

describe('CostPanel render smoke test', () => {
  it('calls loadCost on mount when a project is selected', () => {
    Object.assign(mockState, buildState());
    render(<CostPanel region="TW" />);
    expect(mockState.loadCost).toHaveBeenCalledTimes(1);
  });

  it('renders total cost + per-task rows once store.cost is populated', () => {
    Object.assign(
      mockState,
      buildState({
        cost: {
          total_cost: 100,
          by_resource: { crane: 60 },
          by_category: { equipment: 60 },
          by_wbs: { '1.1': 100 },
          per_task: [{ task_id: 'T1', task_name: 'Dig', duration: 2, cost: 100 }],
          cost_curve: [{ day: 0, cost: 50, cumulative: 50 }],
        },
      }),
    );
    const { getByText, getAllByText } = render(<CostPanel region="TW" />);
    expect(getByText('T1')).toBeInTheDocument();
    expect(getByText('Dig')).toBeInTheDocument();
    // 總成本 + 任務成本皆為 100（表格 + 卡片）
    expect(getAllByText('100').length).toBeGreaterThan(0);
  });

  it('shows the project placeholder and does not call loadCost when there is no current project', () => {
    Object.assign(mockState, buildState({ currentProject: null }));
    const { container } = render(<CostPanel region="TW" />);
    expect(container.textContent).toContain(t('TW', 'projectName'));
    expect(mockState.loadCost).not.toHaveBeenCalled();
  });
});
