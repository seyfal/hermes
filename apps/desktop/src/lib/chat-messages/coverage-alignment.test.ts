import { describe, expect, it } from 'vitest'

import { reconcilePersistedLiveTurn } from '@/app/session/hooks/use-session-actions/persisted-live-turn'
import { type ChatMessage, type ChatMessagePart, chatMessageText, toChatMessages } from '@/lib/chat-messages'
import type { SessionMessage } from '@/types/hermes'

import { withoutCoveredAssistantPrefix } from './coverage'
import { upsertToolPart } from './tool-parts'

const text = (text: string, sourceRowId?: number): ChatMessagePart => ({ type: 'text', text, sourceRowId })
const tool = (toolCallId: string, result = 'ok'): ChatMessagePart => ({ type: 'tool-call', toolCallId, toolName: 'read_file', args: {}, result })
const row = (parts: ChatMessagePart[]): ChatMessage => ({ id: 'assistant-stream-local', role: 'assistant', parts, pending: true })

describe('assistant suffix coverage', () => {
  it('aligns a surviving local suffix with its unique stored tool occurrence', () => {
    expect(withoutCoveredAssistantPrefix(
      [row([tool('earlier'), text('checking'), tool('same')])],
      [row([text('checking'), tool('same')])]
    )).toEqual([])
  })

  it.each([false, true])('covers a live-produced tool with equivalent hydrated payload (earlier round: %s)', earlier => {
    const stored = toChatMessages([
      { id: 4, role: 'assistant', content: 'checking', tool_calls: [{ id: 'same', type: 'function', function: { name: 'read_file', arguments: '{}' } }] },
      { id: 5, role: 'tool', content: 'ok', tool_call_id: 'same' }
    ])

    if (earlier) {
      stored.unshift(row([tool('earlier')]))
    }

    const liveParts = upsertToolPart([text('checking')], {
      name: 'read_file', tool_id: 'same', args: {}, result: 'ok'
    }, 'complete', 10)

    const suffix = text('still growing')
    const local = row([...liveParts, suffix])

    expect(withoutCoveredAssistantPrefix(stored, [local])).toEqual([{ ...local, parts: [suffix] }])

    for (const extra of [
      { result: 'richer result' },
      { args: { path: 'report.txt' }, argsText: '{"path":"report.txt"}' },
      { argsText: '{"path":' },
      { toolResultMetadata: { inline_diff: '+new evidence' } }
    ]) {
      const richer = [row(liveParts.map(part => part.type === 'tool-call' ? { ...part, ...extra } : part))]

      expect(withoutCoveredAssistantPrefix(stored, richer)).toBe(richer)
    }
  })

  it('aligns source-addressed text and keeps an unrepresented suffix', () => {
    const suffix = { type: 'reasoning' as const, text: 'new thought' }
    const local = row([text('checking', 4), suffix])

    expect(withoutCoveredAssistantPrefix([row([text('earlier', 2), text('checking', 4)])], [local]))
      .toEqual([{ ...local, parts: [suffix] }])
  })

  it('requires an unambiguous occurrence and preserves richer local payloads', () => {
    for (const [stored, local] of [
      [[text('checking')], [text('checking')]],
      [[tool('same'), text('checking'), tool('same')], [text('checking'), tool('same')]],
      [[text('checking'), tool('other')], [text('checking'), tool('same')]],
      [[text('checking'), tool('same', 'partial')], [text('checking'), tool('same', 'complete result')]],
      [[text('checking', 2)], [text('checking', 4)]]
    ]) {
      const messages = [row(local)]

      expect(withoutCoveredAssistantPrefix([row(stored)], messages)).toBe(messages)
    }
  })

  it('does not append a partial live suffix already represented in the hydrated fold', () => {
    const rows: SessionMessage[] = [
      { id: 1, role: 'user', content: 'prompt' },
      { id: 2, role: 'assistant', content: '', tool_calls: [{ id: 'earlier', type: 'function', function: { name: 'read_file', arguments: '{}' } }] },
      { id: 3, role: 'tool', content: 'ok', tool_call_id: 'earlier' },
      { id: 4, role: 'assistant', content: 'checking', tool_calls: [{ id: 'same', type: 'function', function: { name: 'read_file', arguments: '{}' } }] },
      { id: 5, role: 'tool', content: 'ok', tool_call_id: 'same' }
    ]

    const stored = toChatMessages(rows)
    const previous = [stored[0], row([text('checking'), tool('same')])]

    const merged = reconcilePersistedLiveTurn(stored, previous, rows, {
      session_id: 'session', inflight: { user: 'prompt', assistant: 'checking', streaming: true }
    })!

    expect(merged).not.toBeNull()
    expect(merged.map(chatMessageText).join('|').split('checking')).toHaveLength(2)
    expect(merged.flatMap(message => message.parts).filter(part => part.type === 'tool-call' && part.toolCallId === 'same')).toHaveLength(1)
  })
})
