import type { GatewayEventName } from '@hermes/shared'
import { act, cleanup } from '@testing-library/react'
import { afterEach, expect, it } from 'vitest'

import { type ChatMessage, chatMessageText, toChatMessages } from '@/lib/chat-messages'

import { appendLiveSessionProjection } from '../use-session-actions/utils'

import { playFrames, renderMessageStream } from './test-harness'

afterEach(cleanup)

it.each(['tool', 'hydrated'])('keeps one commentary with a queued next prompt (%s)', async shape => {
  const sid = `queued-${shape}`
  const stream = renderMessageStream(sid)

  const send = (type: GatewayEventName, payload: Record<string, unknown> = {}) =>
    act(() => stream.handleEvent({ type, payload, session_id: sid }))

  await send('message.start')
  await send('message.delta', { text: 'Checking.' })
  await send('message.interim', { text: 'Checking.', already_streamed: true })
  await send('tool.start', { name: 'read_file', tool_id: 'call', args: {} })
  const current = stream.state()

  const messages = shape === 'tool' ? current.messages : toChatMessages([
    { id: 1, role: 'user', content: 'prompt' },
    { id: 2, role: 'assistant', content: 'Checking.', tool_calls: [{ id: 'call', type: 'function', function: { name: 'read_file', arguments: '{}' } }] },
    { id: 3, role: 'tool', content: 'ok', tool_call_id: 'call' }
  ])

  stream.states.set(sid, {
    ...current,
    streamId: shape === 'tool' ? current.streamId : null,
    messages: appendLiveSessionProjection(messages, { session_id: sid, queued: { user: 'next prompt' } })
  })
  await send('message.interim', { text: 'Checking.', already_streamed: true })

  expect(stream.state().messages.map(chatMessageText).filter(text => text === 'Checking.')).toHaveLength(1)
  expect(stream.state().messages.at(-1)?.id).toBe(`user-queued-${sid}`)
})

it.each([false, true])('keeps real user boundaries even when a next prompt is queued (hidden: %s)', async hidden => {
  const sid = `correction-${hidden}`
  const stream = renderMessageStream(sid)

  const send = (type: GatewayEventName, payload: Record<string, unknown> = {}) =>
    act(() => stream.handleEvent({ type, payload, session_id: sid }))

  await send('message.start')
  await send('message.interim', { text: 'Checking.' })
  const current = stream.state()
  const correction: ChatMessage = { id: 'correction', role: 'user', hidden, parts: [{ type: 'text', text: 'different file' }] }

  stream.states.set(sid, {
    ...current,
    messages: appendLiveSessionProjection([...current.messages, correction], { session_id: sid, queued: { user: 'next' } })
  })
  await send('tool.start', { name: 'read_file', tool_id: 'call', args: {} })
  await send('message.interim', { text: 'Checking.' })

  expect(stream.state().messages.map(chatMessageText).filter(text => text === 'Checking.')).toHaveLength(2)
})

it('preserves the legitimate repeated-text seal before and after a tool', async () => {
  const messages = await playFrames('repeated-done', [
    ['message.start', {}], ['message.delta', { text: 'Done.' }],
    ['tool.start', { name: 'read_file', tool_id: 'call', args: {} }],
    ['tool.complete', { name: 'read_file', tool_id: 'call', result: 'ok' }],
    ['message.delta', { text: 'Done.' }],
    ['message.interim', { text: 'Done.', already_streamed: true }],
    ['message.complete', { text: 'Done.' }]
  ])

  expect(messages.map(chatMessageText)).toEqual(['Done.'])
})
