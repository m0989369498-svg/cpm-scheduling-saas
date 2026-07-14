// Pro Batch D Feature 2：HealthPanel — smoke render 測試（mock store，同 FieldMode.test.jsx 手法）
// 驗證：
//   1. 掛載時（有 currentProject）呼叫 store.loadHealth()
//   2. store.health 有資料時渲染健康度分數 + 14 點檢查列（名稱 i18n dcma_<key> + 通過/未通過/不適用徽章）
//   3. 無 currentProject 時顯示提示，不呼叫 loadHealth
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

import HealthPanel from './HealthPanel.jsx';

function buildState(overrides = {}) {
  return {
    loading: {},
    errors: {},
    currentProject: { project_id: 'P1', project_name: 'Demo' },
    health: null,
    loadHealth: vi.fn().mockResolvedValue(null),
    ...overrides,
  };
}

beforeEach(() => {
  Object.keys(mockState).forEach((k) => delete mockState[k]);
});

describe('HealthPanel render smoke test', () => {
  it('calls loadHealth on mount when a project is selected', () => {
    Object.assign(mockState, buildState());
    render(<HealthPanel region="TW" />);
    expect(mockState.loadHealth).toHaveBeenCalledTimes(1);
  });

  it('renders the health score and check rows (pass + fail + na badges) once store.health is populated', () => {
    Object.assign(
      mockState,
      buildState({
        health: {
          data_date: 10,
          checks: [
            { key: 'logic', value: 0, threshold: 0.05, comparison: 'lte', count: 0, total: 3, passed: true, detail: [] },
            { key: 'negative_float', value: 1, threshold: 0, comparison: 'eq', count: 1, total: 3, passed: false, detail: ['T1'] },
            { key: 'missed_tasks', value: null, threshold: 0.05, comparison: 'lte', count: 0, total: 0, passed: null, detail: [] },
          ],
          score: 0.5,
          passed_count: 1,
          applicable_count: 2,
          total_count: 14,
        },
      }),
    );
    const { getByText } = render(<HealthPanel region="TW" />);
    expect(getByText(t('TW', 'dcma_logic'))).toBeInTheDocument();
    expect(getByText(t('TW', 'dcma_negative_float'))).toBeInTheDocument();
    expect(getByText(t('TW', 'dcma_missed_tasks'))).toBeInTheDocument();
    expect(getByText(t('TW', 'checkPass'))).toBeInTheDocument();
    expect(getByText(t('TW', 'checkFail'))).toBeInTheDocument();
    expect(getByText(t('TW', 'checkNa'))).toBeInTheDocument();
    // healthScore = 50% (1/2)
    expect(getByText('50% (1/2)')).toBeInTheDocument();
  });

  it('shows the project placeholder and does not call loadHealth when there is no current project', () => {
    Object.assign(mockState, buildState({ currentProject: null }));
    const { container } = render(<HealthPanel region="TW" />);
    expect(container.textContent).toContain(t('TW', 'projectName'));
    expect(mockState.loadHealth).not.toHaveBeenCalled();
  });
});
