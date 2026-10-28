import { expect, it, vi } from 'vitest'

import { toChatMessages } from '@/lib/chat-messages'
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
