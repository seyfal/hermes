import { expect, it } from 'vitest'

import type { ChatMessage, ChatMessagePart } from '@/lib/chat-messages'

import { preserveEquivalentTranscript } from './utils'

const message = (part: ChatMessagePart): ChatMessage => ({ id: 'row', rowId: 1, role: 'assistant', parts: [part] })

it('publishes a canonical display address even when the row and rendered text are unchanged', () => {
  const previous = [message({ type: 'text', text: 'same' })]
  const refreshed = [{ ...previous[0], displayIdentity: 'canonical-occurrence' }]
  expect(preserveEquivalentTranscript(previous, refreshed)).toBe(refreshed)
})

it.each<ChatMessagePart>([
  { type: 'text', text: 'same' },
  { type: 'tool-call', toolCallId: 'call', toolName: 'read_file', args: {}, argsText: '', result: 'same' },
  { type: 'image', image: '/fixture/image.png' }
])('publishes changed $type source coverage without repainting identical coverage', part => {
  const previous = [message({ ...part, sourceDisplayIdentities: ['call-source'] })]
  const refreshed = [message({ ...part, sourceDisplayIdentities: ['call-source', 'result-source'] })]
  expect(preserveEquivalentTranscript(previous, refreshed)).toBe(refreshed)
  const again = [message({ ...part, sourceDisplayIdentities: ['call-source', 'result-source'] })]
  expect(preserveEquivalentTranscript(refreshed, again)).toBe(refreshed)
})
