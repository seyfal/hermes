import { act, cleanup } from '@testing-library/react'
import { afterEach, expect, it } from 'vitest'

import { type ChatMessage, chatMessageText } from '@/lib/chat-messages'

import { collapseDuplicateFinalAfterToolInterim } from './collapse-duplicate-final'
import { type GatewayFrame, renderMessageStream } from './test-harness'

afterEach(cleanup)

it('collapses an adjacent interim across a system notice without dropping its tool', () => {
  const messages: ChatMessage[] = [
    { id: 'interim', role: 'assistant', parts: [{ type: 'text', text: 'Answer' }], interim: true },
    { id: 'notice', role: 'system', parts: [{ type: 'text', text: 'review:Saved notes' }] },
    { id: 'live', role: 'assistant', parts: [{ type: 'tool-call', toolCallId: 'call', toolName: 'read_file', args: {}, result: 'ok' }], pending: true }
  ]

  const result = collapseDuplicateFinalAfterToolInterim(messages, 2, {
    completeMessage: message => ({ ...message, interim: false, pending: false }),
    finalText: 'Answer',
    hasFailure: false,
    interimBoundaryPending: true
  })

  expect(result?.messages.map(message => message.id)).toEqual(['interim', 'notice'])
  expect(result?.messages[0].parts).toContainEqual(messages[2].parts[0])
})

it('keeps a review summary while settling the reply and tool once', async () => {
  const stream = renderMessageStream('review-summary-completion')

  const frames: GatewayFrame[] = [
    ['message.start', {}],
    ['message.delta', { text: 'Answer' }],
    ['message.interim', { text: 'Answer', already_streamed: true }],
    ['review.summary', { text: 'Saved notes' }],
    ['tool.start', { name: 'read_file', tool_id: 'call', args: {} }],
    ['tool.complete', { name: 'read_file', tool_id: 'call', result: 'ok' }],
    ['message.complete', { text: 'Answer' }]
  ]

  for (const [type, payload] of frames) {
    await act(() => stream.handleEvent({ type, payload, session_id: 'review-summary-completion' }))
  }

  const messages = stream.state().messages
  expect(messages.filter(message => message.role === 'assistant').map(chatMessageText)).toEqual(['Answer'])
  expect(messages.filter(message => message.role === 'system').map(chatMessageText)).toEqual(['review:Saved notes'])
  expect(messages.flatMap(message => message.parts).filter(part => part.type === 'tool-call')).toHaveLength(1)
})
