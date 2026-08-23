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
      await act(() =>
        stream.handleEvent({ payload: {}, session_id: SID, type: 'message.start' })
      )
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
})