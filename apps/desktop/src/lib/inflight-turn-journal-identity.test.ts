import { expect, it } from 'vitest'

import type { ChatMessage, ChatMessagePart } from '@/lib/chat-messages'

import { mergeInFlightMessages } from './inflight-turn-journal'

const text = (text: string, sourceRowId?: number): ChatMessagePart => ({ type: 'text', text, sourceRowId })

const final = (folded = false): ChatMessage => ({
  id: 'stored-final', role: 'assistant', rowId: folded ? 2 : 4, durableComplete: true,
  parts: folded ? [text('earlier', 2), text('answer full', 4)] : [text('answer full', 4)]
})

const journal = (extra: Partial<ChatMessage> = {}): ChatMessage => ({
  id: 'assistant-stream-old', role: 'assistant', rowId: 4, pending: true, parts: [text('answer')], ...extra
})

it.each([false, true])('retires a throttled prefix by the durable final source row (folded: %s)', folded => {
  const base = [final(folded)]
  const result = mergeInFlightMessages(base, [journal()])

  expect(result.caughtUp).toBe(true)
  expect(result.messages).toBe(base)
})

it('keeps an unidentified prefix, unfinished final and different source occurrence', () => {
  for (const [base, local] of [
    [final(), journal({ rowId: undefined })],
    [{ ...final(), durableComplete: false }, journal()],
    [final(), journal({ rowId: 8 })],
    [final(true), journal({ rowId: 2 })],
    [final(), journal({ error: 'failed' })]
  ]) {
    const result = mergeInFlightMessages([base], [local])

    expect(result.caughtUp).toBe(false)
    expect(result.messages).toHaveLength(2)
  }
})

it.each(['answer', 'answer full'])('keeps uncommitted tools, reasoning and media beside %s', snapshot => {
  const extras: ChatMessagePart[] = [
    { type: 'tool-call', toolCallId: 'unique', toolName: 'read_file', args: {}, result: 'ok' },
    { type: 'reasoning', text: 'uncommitted thought' },
    { type: 'image', image: 'data:image/png;base64,AAAA' }
  ]

  for (const extra of extras) {
    const result = mergeInFlightMessages([final()], [journal({ parts: [text(snapshot), extra] })])

    expect(result.caughtUp).toBe(false)
    expect(result.messages.at(-1)?.parts).toContainEqual(extra)
  }
})
