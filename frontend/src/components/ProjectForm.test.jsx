// QUAL-2 (Batch 4)：ProjectForm 用戶端驗證單元測試
// 驗證：任務編號重複 -> 錯誤、工期 < 0 -> 錯誤、合法輸入 -> onSubmit payload
// 形狀正確（含選填 start_date 與 work_days）。
import React from 'react'
import { describe, it, expect, vi } from 'vitest'
import { render, fireEvent, waitFor } from '@testing-library/react'
import ProjectForm from './ProjectForm.jsx'
import { t } from '../i18n/index.js'

function setup(props = {}) {
  const onSubmit = vi.fn().mockResolvedValue({})
  const onCancel = vi.fn()
  const utils = render(
    <ProjectForm region="TW" defaultRegion="TW" onSubmit={onSubmit} onCancel={onCancel} {...props} />,
  )
  return { onSubmit, onCancel, ...utils }
}

function fillProjectName(container, name) {
  const input = container.querySelector('input[placeholder="專案名稱"]')
  fireEvent.change(input, { target: { value: name } })
}

function taskIdInputs(container) {
  return Array.from(container.querySelectorAll('input[placeholder="T-01"]'))
}

function submit(container) {
  fireEvent.submit(container.querySelector('form'))
}

describe('ProjectForm validation', () => {
  it('rejects duplicate task ids with the duplicateTaskId message', async () => {
    const { container, onSubmit, getByText, findByText } = setup()
    fillProjectName(container, '示範專案')
    // 新增第二列後，兩列填入相同 task_id
    fireEvent.click(getByText(`+ ${t('TW', 'addTaskRow')}`))
    const ids = taskIdInputs(container)
    expect(ids).toHaveLength(2)
    fireEvent.change(ids[0], { target: { value: 'T-1' } })
    fireEvent.change(ids[1], { target: { value: 'T-1' } })
    submit(container)
    expect(await findByText(t('TW', 'duplicateTaskId'))).toBeInTheDocument()
    expect(onSubmit).not.toHaveBeenCalled()
  })

  it('rejects a negative duration with the invalidDuration message', async () => {
    const { container, onSubmit, findByText } = setup()
    fillProjectName(container, '示範專案')
    fireEvent.change(taskIdInputs(container)[0], { target: { value: 'T-1' } })
    fireEvent.change(container.querySelector('input[type="number"]'), {
      target: { value: '-1' },
    })
    submit(container)
    expect(await findByText(t('TW', 'invalidDuration'))).toBeInTheDocument()
    expect(onSubmit).not.toHaveBeenCalled()
  })

  it('rejects a missing task id with the taskIdRequired message', async () => {
    const { container, onSubmit, findByText } = setup()
    fillProjectName(container, '示範專案')
    // task_id 留白
    submit(container)
    expect(await findByText(t('TW', 'taskIdRequired'))).toBeInTheDocument()
    expect(onSubmit).not.toHaveBeenCalled()
  })

  it('submits a valid payload including start_date when set', async () => {
    const { container, onSubmit } = setup()
    fillProjectName(container, '橋梁工程')
    fireEvent.change(taskIdInputs(container)[0], { target: { value: 'T-1' } })
    fireEvent.change(container.querySelector('input[type="date"]'), {
      target: { value: '2026-07-01' },
    })
    submit(container)
    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1))
    const payload = onSubmit.mock.calls[0][0]
    expect(payload.project_name).toBe('橋梁工程')
    expect(payload.region).toBe('TW')
    expect(payload.start_date).toBe('2026-07-01')
    expect(payload.work_days).toBe('1111110')
    expect(payload.schedule_data).toHaveLength(1)
    expect(payload.schedule_data[0]).toMatchObject({
      task_id: 'T-1',
      duration: 1,
      predecessors: [],
      status: 'PENDING',
    })
  })

  it('omits start_date from the payload when the date is left blank', async () => {
    const { container, onSubmit } = setup()
    fillProjectName(container, '無日期專案')
    fireEvent.change(taskIdInputs(container)[0], { target: { value: 'T-1' } })
    submit(container)
    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1))
    const payload = onSubmit.mock.calls[0][0]
    expect(payload).not.toHaveProperty('start_date')
  })
})
