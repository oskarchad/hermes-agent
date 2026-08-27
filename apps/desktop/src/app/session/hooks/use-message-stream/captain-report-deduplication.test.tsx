import { act, cleanup } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { chatMessageText } from '@/lib/chat-messages'

import { renderMessageStream } from './test-harness'

const SID = 'captain-session'

describe('Captain report completion receipts', () => {
  afterEach(cleanup)

  it('keeps one visible assistant report when the completion is replayed after a crash', async () => {
    const stream = renderMessageStream(SID)

    for (let attempt = 0; attempt < 2; attempt += 1) {
      await act(() => stream.handleEvent({ payload: {}, session_id: SID, type: 'message.start' }))
      await act(() =>
        stream.handleEvent({
          payload: { text: 'Captain report' },
          session_id: SID,
          type: 'message.delta'
        })
      )
      await act(() =>
        stream.handleEvent({
          payload: { id: 'kanban-report:board:42', text: 'Captain report' },
          session_id: SID,
          type: 'message.complete'
        })
      )
    }

    const reports = stream
      .state()
      .messages.filter(message => message.role === 'assistant' && chatMessageText(message) === 'Captain report')

    expect(reports).toHaveLength(1)
    expect(stream.state().busy).toBe(false)
  })

  it('does not let a failed attempt consume the stable receipt used by a successful retry', async () => {
    const stream = renderMessageStream(SID)

    await act(() => stream.handleEvent({ payload: {}, session_id: SID, type: 'message.start' }))
    await act(() =>
      stream.handleEvent({
        payload: {
          error: 'temporary persistence failure',
          id: 'kanban-report:board:retry',
          status: 'error',
          text: 'temporary persistence failure'
        },
        session_id: SID,
        type: 'message.complete'
      })
    )

    await act(() => stream.handleEvent({ payload: {}, session_id: SID, type: 'message.start' }))
    await act(() =>
      stream.handleEvent({
        payload: {
          id: 'kanban-report:board:retry',
          status: 'complete',
          text: 'Captain retry succeeded'
        },
        session_id: SID,
        type: 'message.complete'
      })
    )

    expect(
      stream
        .state()
        .messages.filter(
          message => message.role === 'assistant' && chatMessageText(message) === 'Captain retry succeeded'
        )
    ).toHaveLength(1)
  })

  it('does not let an interrupted attempt consume the stable receipt used by a successful retry', async () => {
    const stream = renderMessageStream(SID)

    await act(() => stream.handleEvent({ payload: {}, session_id: SID, type: 'message.start' }))
    await act(() =>
      stream.handleEvent({
        payload: {
          id: 'kanban-report:board:interrupted-retry',
          status: 'interrupted',
          text: 'Captain report interrupted'
        },
        session_id: SID,
        type: 'message.complete'
      })
    )

    await act(() => stream.handleEvent({ payload: {}, session_id: SID, type: 'message.start' }))
    await act(() =>
      stream.handleEvent({
        payload: {
          id: 'kanban-report:board:interrupted-retry',
          status: 'complete',
          text: 'Captain retry after interruption succeeded'
        },
        session_id: SID,
        type: 'message.complete'
      })
    )

    expect(
      stream
        .state()
        .messages.filter(
          message =>
            message.role === 'assistant' &&
            chatMessageText(message) === 'Captain retry after interruption succeeded'
        )
    ).toHaveLength(1)
  })
})
