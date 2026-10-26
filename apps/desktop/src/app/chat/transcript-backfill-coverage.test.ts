import { expect, it } from 'vitest'

import { type ChatMessage, type ChatMessagePart, chatMessageText } from '@/lib/chat-messages'

import { graftRefreshedTailOntoBackfill } from './transcript-backfill'

const text = (value: string, sourceRowId?: number): ChatMessagePart => ({ type: 'text', text: value, sourceRowId })

const tool = (toolCallId: string, result = 'ok'): ChatMessagePart => ({
  type: 'tool-call', toolCallId, toolName: 'read_file', args: {}, result
})

const user = (rowId: number): ChatMessage => ({ id: `user-${rowId}`, rowId, role: 'user', parts: [text('prompt')] })

const interim = (parts: ChatMessagePart[], extra: Partial<ChatMessage> = {}): ChatMessage => ({
  id: 'interim', role: 'assistant', interim: true, parts, ...extra
})

const durable = (parts: ChatMessagePart[], rowId = 11): ChatMessage => ({ id: `stored-${rowId}`, rowId, role: 'assistant', parts })

it('retains the unique tool beside equal interim prose', () => {
  const previous = [user(8), interim([text('checking'), tool('unique-call')]), user(15)]
  const fresh = [durable([text('checking')]), previous[2]]
  const merged = graftRefreshedTailOntoBackfill(fresh, previous)

  expect(merged.flatMap(message => message.parts).filter(part => part.type === 'tool-call')).toEqual([tool('unique-call')])
})

it('subtracts source and tool occurrences while preserving unrepresented parts across repeated refreshes', () => {
  const uncovered: ChatMessagePart[] = [
    tool('unique-call'),
    { type: 'reasoning', text: 'still investigating' },
    { type: 'image', image: 'data:image/png;base64,AAAA' }
  ]

  const covered = [text('checking', 11), tool('same')]
  const previous = [user(8), interim([...covered, ...uncovered]), user(15)]
  const fresh = [durable(covered), previous[2]]
  const merged = graftRefreshedTailOntoBackfill(fresh, previous)

  expect(merged.find(message => message.id === 'interim')?.parts).toEqual(uncovered)
  expect(merged.map(chatMessageText).filter(value => value === 'checking')).toHaveLength(1)
  expect(graftRefreshedTailOntoBackfill(fresh, merged)).toEqual(merged)
})

it('keeps richer tool results, pending ownership and errors', () => {
  const fresh = [durable([text('checking', 11), tool('same', 'partial')]), user(15)]

  for (const extra of [{}, { pending: true }, { error: 'failed' }]) {
    const live = interim([text('checking', 11), tool('same', 'complete result')], extra)
    const merged = graftRefreshedTailOntoBackfill(fresh, [user(8), live, user(15)])
    const kept = merged.find(message => message.id === live.id)

    expect(kept?.parts).toContainEqual(tool('same', 'complete result'))
    expect(kept?.pending).toBe(live.pending)
    expect(kept?.error).toBe(live.error)
  }
})

it('does not use repeated prose, a different tool or another user interval as coverage', () => {
  const live = interim([text('checking'), tool('local')])
  const previous = [user(8), live, user(15)]
  const fresh = [durable([text('checking'), tool('other')]), user(15)]

  expect(graftRefreshedTailOntoBackfill(fresh, previous)).toContainEqual(live)

  const textOnly = interim([text('checking')])
  const ambiguous = [durable([text('checking'), text('checking')]), user(15)]
  const differentTurn = [user(10), durable([text('checking')]), user(15)]
  const repeated = interim([text('checking'), text('checking')])

  expect(graftRefreshedTailOntoBackfill([durable([text('checking')]), user(15)], [user(8), repeated, user(15)])).toContainEqual(repeated)

  expect(graftRefreshedTailOntoBackfill(ambiguous, [user(8), textOnly, user(15)])).toContainEqual(textOnly)
  expect(graftRefreshedTailOntoBackfill(differentTurn, [user(8), textOnly, user(15)])).toContainEqual(textOnly)
})
