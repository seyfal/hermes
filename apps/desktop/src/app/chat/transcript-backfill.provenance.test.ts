import { describe, expect, it, vi } from 'vitest'

import { type ChatMessage, chatMessageText, toChatMessages } from '@/lib/chat-messages'
import type { SessionMessage } from '@/types/hermes'

import {
  extendRefreshPageToOverlap,
  graftRefreshedTailOntoBackfill,
  mergeOlderTranscriptPage
} from './transcript-backfill'

const row = (id: number, identity = id, content = `row ${identity}`): SessionMessage => ({
  id,
  session_id: 'session',
  display_identity: String(identity).padStart(64, '0'),
  display_order: identity,
  role: 'user',
  content,
  timestamp: id
})

const ids = (rows: ReturnType<typeof toChatMessages>) => rows.map(message => message.rowId)

it('keeps server display order when a physical representative is newer than its successor', () => {
  const previous = toChatMessages([row(5), row(20)])
  const refreshed = toChatMessages([row(30), row(20)])
  expect(ids(graftRefreshedTailOntoBackfill(refreshed, previous))).toEqual([5, 30, 20])
})

it('keeps a backfilled prefix with a larger physical id in front of the shared anchor', () => {
  const previous = toChatMessages([row(50), row(10), row(20)])
  expect(ids(graftRefreshedTailOntoBackfill(toChatMessages([row(10), row(20)]), previous))).toEqual([50, 10, 20])
})

it('finds overlap beyond an older page with small representative ids', async () => {
  const read = vi
    .fn()
    .mockResolvedValueOnce(toChatMessages([row(5)]))
    .mockResolvedValueOnce(toChatMessages([row(100)]))

  const extended = await extendRefreshPageToOverlap(toChatMessages([row(30)]), toChatMessages([row(100)]), read)
  expect(ids(extended)).toEqual([100, 5, 30])
  expect(read).toHaveBeenCalledTimes(2)
})

it('replaces structured carried generations without merging equal-text independent users', () => {
  const call = { id: 'call', type: 'function', function: { name: 'read_file', arguments: '{}' } }

  const before: SessionMessage[] = [
    row(1),
    { ...row(2), role: 'assistant', tool_calls: [call] },
    { ...row(3), role: 'tool', tool_call_id: 'call', content: 'full result' },
    { ...row(4), role: 'assistant' },
    row(5, 5, 'repeat'),
    row(6, 6, '@file:/fixture/report.pdf\n@image:/fixture/image.png\nattachment')
  ]

  const after = [row(0), ...before.map(message => ({ ...message, id: message.id! + 20 })), row(40, 40, 'repeat')]
  const fresh = toChatMessages(after)
  const merged = graftRefreshedTailOntoBackfill(fresh, toChatMessages(before))
  expect(merged).toEqual(fresh)
  expect(merged.flatMap(message => message.parts).filter(part => part.type === 'tool-call')).toHaveLength(1)
  expect(merged.flatMap(message => message.attachmentRefs ?? [])).toEqual([
    '@file:/fixture/report.pdf',
    '@image:/fixture/image.png'
  ])
})

it('distinguishes full and pruned tool results even when the call and UID match', () => {
  const full: SessionMessage = {
    ...row(2),
    role: 'tool',
    tool_call_id: 'call',
    content: 'full result',
    message_uid: 'same'
  }

  const stub: SessionMessage = { ...full, ...row(20, 20), role: 'tool', content: 'pruned' }
  const previous = toChatMessages([row(1), full, row(3)])
  const fresh = toChatMessages([row(0), row(1), stub, row(3)])
  expect(
    graftRefreshedTailOntoBackfill(fresh, previous)
      .flatMap(message => message.parts)
      .filter(part => part.type === 'tool-call')
      .map(part => part.result)
  ).toEqual(['pruned', 'full result'])
})

it('matches folded source occurrences across page boundaries and re-IDs', () => {
  const call = { id: 'call', type: 'function', function: { name: 'read_file', arguments: '{}' } }

  const before = toChatMessages([
    row(1),
    { ...row(2), role: 'assistant', tool_calls: [call] },
    { ...row(3), role: 'assistant' }
  ])

  const fresh = toChatMessages([{ ...row(30, 3), role: 'assistant' }, row(40)])
  // A partial fresh page cannot erase the earlier tool source in the fold.
  const merged = graftRefreshedTailOntoBackfill(fresh, before)
  expect(merged.flatMap(message => message.parts).filter(part => part.type === 'tool-call')).toHaveLength(1)
  expect(
    merged.flatMap(message => message.parts).filter(part => part.type === 'text' && part.text === 'row 3')
  ).toHaveLength(1)
})

it('uses provenance for older-page overlap and does not sort its physical representatives', () => {
  const existing = toChatMessages([row(30, 10), row(20)])
  const older = toChatMessages([row(50), row(10)])
  expect(ids(mergeOlderTranscriptPage(existing, older))).toEqual([50, 30, 20])
})

it('replaces an unanchored compaction window and scopes identity to its source session', () => {
  const previous = toChatMessages([row(1)])
  const fresh = toChatMessages([row(50, 2)])
  expect(graftRefreshedTailOntoBackfill(fresh, previous)).toBe(fresh)
  const other = toChatMessages([{ ...row(50, 1), session_id: 'other' }])
  expect(mergeOlderTranscriptPage(previous, other)).toHaveLength(2)
})

describe('receipt-bound live turn against a stored page', () => {
  const stored = (id: number, role: SessionMessage['role'], content: string, extra: Partial<SessionMessage> = {}) => ({
    ...row(id, id, content),
    role,
    ...extra
  })

  const turn4 = [stored(1, 'user', 'question 4'), stored(2, 'assistant', 'answer 4')]
  const turn5 = [stored(3, 'user', 'question 5'), stored(4, 'assistant', 'answer 5')]

  // A settled live turn: the optimistic prompt bound to its stored row by the
  // submit receipt and the stream bubble bound by `persisted_turn`
  // (withPersistedIdentity). Neither carries a display identity.
  const liveUser = (rowId: number, text = 'question 5'): ChatMessage => ({
    id: 'user-1790000200000-abc',
    role: 'user',
    rowId,
    parts: [{ type: 'text', text }]
  })

  const liveAssistant = (rowId: number, finalRowId = rowId, overrides: Partial<ChatMessage> = {}): ChatMessage => ({
    id: 'assistant-stream-1790000200500-1',
    role: 'assistant',
    rowId,
    pending: false,
    interim: false,
    durableComplete: true,
    parts: [{ type: 'text', text: 'answer 5', sourceRowId: finalRowId }],
    ...overrides
  })

  const texts = (messages: ChatMessage[]) => messages.map(message => `${message.role}:${chatMessageText(message)}`)

  it('replaces the bound live turn with its stored rows instead of painting it twice', () => {
    const window = [...toChatMessages(turn4), liveUser(3), liveAssistant(4)]
    const fresh = toChatMessages([...turn4, ...turn5])
    const merged = graftRefreshedTailOntoBackfill(fresh, window)

    expect(merged).toHaveLength(4)
    expect(texts(merged)).toEqual(['user:question 4', 'assistant:answer 4', 'user:question 5', 'assistant:answer 5'])
    expect(merged).toEqual(fresh)
  })

  it('keeps one assistant bubble for a tool turn whose stored head row differs from the final row', () => {
    const call = { id: 'call', type: 'function', function: { name: 'read_file', arguments: '{}' } }

    const toolTurn = [
      stored(3, 'user', 'question 5'),
      stored(4, 'assistant', 'let me look', { tool_calls: [call] }),
      stored(5, 'tool', 'file body', { tool_call_id: 'call' }),
      stored(6, 'assistant', 'answer 5')
    ]

    const fresh = toChatMessages([...turn4, ...toolTurn])
    const freshAssistant = fresh.filter(message => message.role === 'assistant').at(-1)!
    // The page folds the turn into one bubble addressed by its head row.
    expect(freshAssistant.rowId).toBe(4)

    const window = [...toChatMessages(turn4), liveUser(3), liveAssistant(6, 6)]
    const merged = graftRefreshedTailOntoBackfill(fresh, window)
    const turn = merged.slice(merged.findIndex(message => chatMessageText(message) === 'question 5'))

    expect(turn.filter(message => message.role === 'user')).toHaveLength(1)
    expect(turn.filter(message => message.role === 'assistant')).toHaveLength(1)
  })

  it('keeps the live bubble while the receipt does not vouch for the whole turn', () => {
    const window = [...toChatMessages(turn4), liveUser(3), liveAssistant(4, 4, { durableComplete: false })]
    const merged = graftRefreshedTailOntoBackfill(toChatMessages([...turn4, ...turn5]), window)

    expect(merged.some(message => message.id === 'assistant-stream-1790000200500-1')).toBe(true)
    expect(merged.filter(message => message.role === 'user' && chatMessageText(message) === 'question 5')).toHaveLength(
      1
    )
  })

  it('still drops unbound live rows of a turn the page has stored', () => {
    const unboundUser: ChatMessage = { ...liveUser(3), rowId: undefined }

    const unboundAssistant: ChatMessage = {
      ...liveAssistant(4),
      rowId: undefined,
      parts: [{ type: 'text', text: 'answer 5' }]
    }

    const window = [...toChatMessages(turn4), unboundUser, unboundAssistant]
    const fresh = toChatMessages([...turn4, ...turn5])

    expect(texts(graftRefreshedTailOntoBackfill(fresh, window))).toEqual([
      'user:question 4',
      'assistant:answer 4',
      'user:question 5',
      'assistant:answer 5'
    ])
  })
})
