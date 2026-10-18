import type { GatewayEventName } from '@hermes/shared'
import { act, cleanup } from '@testing-library/react'
import { afterEach, expect, it } from 'vitest'

import type { ClientSessionState } from '@/app/types'
import { type ChatMessage, chatMessageText, toChatMessages } from '@/lib/chat-messages'
import { createClientSessionState } from '@/lib/chat-runtime'
import type { SessionMessage } from '@/types/hermes'

import { renderMessageStream } from './test-harness'

afterEach(cleanup)

it('does not append replayed commentary already carried by a hydrated tool fold', async () => {
  const sid = 'folded-interim-replay'
  const comments = Array.from({ length: 9 }, (_, index) => `Inspecting fixture ${index + 1}.`)
  const rows: SessionMessage[] = [{ id: 1, role: 'user', content: 'Inspect the fixtures' }]

  for (const [index, comment] of comments.entries()) {
    const toolId = `read-${index}`
    rows.push(
      {
        id: index * 2 + 2,
        role: 'assistant',
        content: comment,
        tool_calls: [{ id: toolId, type: 'function', function: { name: 'read_file', arguments: '{}' } }]
      },
      { id: index * 2 + 3, role: 'tool', content: 'fixture', tool_call_id: toolId }
    )
  }

  const hydrated = toChatMessages(rows)
  const states = new Map<string, ClientSessionState>([
    [sid, { ...createClientSessionState(), messages: hydrated, busy: true, turnLive: true }]
  ])
  const stream = renderMessageStream(sid, { states })
  const send = (type: GatewayEventName, payload: Record<string, unknown>) =>
    act(() => stream.handleEvent({ type, payload, session_id: sid }))

  for (const text of comments) {
    await send('message.interim', { text, already_streamed: true })
  }

  const afterReplay = stream.state().messages as ChatMessage[]
  expect(afterReplay).toEqual(hydrated)

  await send('message.delta', { text: 'A newly observed result.' })
  await send('message.interim', { text: 'A newly observed result.', already_streamed: true })

  const text = stream.state().messages.map(chatMessageText).join('\n\n')
  for (const comment of [...comments, 'A newly observed result.']) {
    expect(text.split(comment)).toHaveLength(2)
  }
  expect(stream.state().messages.flatMap(message => message.parts.filter(part => part.type === 'tool-call'))).toHaveLength(9)
})
