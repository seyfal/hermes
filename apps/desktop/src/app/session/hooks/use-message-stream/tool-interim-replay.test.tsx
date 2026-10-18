import type { GatewayEventName } from '@hermes/shared'
import { act, cleanup } from '@testing-library/react'
import { afterEach, expect, it } from 'vitest'

import { chatMessageText } from '@/lib/chat-messages'

import { renderMessageStream } from './test-harness'

afterEach(cleanup)

it('keeps one sealed commentary when its interim is replayed over a live tool bubble', async () => {
  const sid = 'tool-interim-replay'
  const stream = renderMessageStream(sid)
  const comment = 'Inspecting the fixture.'
  const send = (type: GatewayEventName, payload: Record<string, unknown> = {}) =>
    act(() => stream.handleEvent({ type, payload, session_id: sid }))

  await send('message.start')
  await send('message.delta', { text: comment })
  await send('message.interim', { text: comment, already_streamed: true })
  await send('tool.start', { name: 'read_file', tool_id: 'read-fixture', args: {} })

  const liveToolStream = stream.state().streamId
  await send('message.interim', { text: comment, already_streamed: true })

  expect(stream.state().messages.map(chatMessageText).filter(text => text === comment)).toHaveLength(1)
  expect(stream.state().streamId).toBe(liveToolStream)

  await send('tool.complete', { name: 'read_file', tool_id: 'read-fixture', result: 'fixture contents' })
  await send('message.complete', { text: 'Please provide the fixture archive.' })

  const messages = stream.state().messages
  expect(messages.map(chatMessageText).join('\n\n').split(comment)).toHaveLength(2)
  expect(messages.map(chatMessageText).join('\n\n').split('Please provide the fixture archive.')).toHaveLength(2)
  expect(messages.flatMap(message => message.parts.filter(part => part.type === 'tool-call'))).toMatchObject([
    { toolCallId: 'read-fixture', result: 'fixture contents' }
  ])
})
