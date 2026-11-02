import { transcriptRowIds } from '@/app/session/hooks/use-session-actions/pending-turn-identity'
import type { ChatMessage } from '@/lib/chat-messages'
import { messageDisplayIdentities } from '@/lib/chat-messages/display-identity'

/** Local turn reconciliation runs after page merging, not inside the stored window. */
export function isUnstoredLiveTurnRow(message: ChatMessage): boolean {
  return (
    message.rowId === undefined &&
    ((message.role === 'user' && message.id.startsWith('user-')) ||
      (message.role === 'assistant' && (message.pending === true || message.id.startsWith('assistant-stream-'))))
  )
}

export function transcriptSourceKeys(message: ChatMessage): string[] {
  const identities = messageDisplayIdentities(message)

  if (identities.length) {
    return identities
  }

  return transcriptRowIds(message).map(row => `row:${row}`)
}

/**
 * Every address a stored (fresh) row can be found under: its display
 * identities and its physical source rows. A live row bound by the
 * `persisted_turn` receipt has only `row:N` keys, so a page indexed by
 * identity alone never finds it.
 */
function lookupKeys(message: ChatMessage): string[] {
  return [...new Set([...messageDisplayIdentities(message), ...transcriptRowIds(message).map(row => `row:${row}`)])]
}

/**
 * A receipt-bound live row (no display identity, rowId from `persisted_turn`)
 * is covered when the page holds every row it names. An assistant bubble
 * counts only once its terminal receipt says the whole turn is durable.
 */
function receiptBoundRowCovered(message: ChatMessage, bySource: Map<string, number>): boolean {
  const rows = transcriptRowIds(message)

  if (messageDisplayIdentities(message).length || message.error || !rows.length) {
    return false
  }

  if (!rows.every(row => bySource.has(`row:${row}`))) {
    return false
  }

  return (
    message.role === 'user' ||
    (message.role === 'assistant' && !message.pending && !message.interim && message.durableComplete === true)
  )
}

export function sharesTranscriptSource(first: ChatMessage[], second: ChatMessage[]): boolean {
  const keys = new Set(first.flatMap(transcriptSourceKeys))

  return second.some(message => transcriptSourceKeys(message).some(key => keys.has(key)))
}

/** Canonical occurrences survive physical re-IDs and carry every source in a folded bubble. */
export function mergeProvenancePage(fresh: ChatMessage[], previous: ChatMessage[], older = false): ChatMessage[] {
  const bySource = new Map<string, number>()
  fresh.forEach((message, index) => {
    for (const key of lookupKeys(message)) {
      bySource.set(key, index)
    }
  })

  // Overlap is exactly "some previous row anchors in this page", including a
  // receipt-bound live row that only the page's physical source rows address.
  if (!previous.some(message => transcriptSourceKeys(message).some(key => bySource.has(key)))) {
    return older ? [...previous, ...fresh] : fresh
  }

  const leading = new Map<number, ChatMessage[]>()
  let pending: ChatMessage[] = []
  let anchored = false

  for (const message of previous) {
    const keys = transcriptSourceKeys(message)
    const anchor = keys.map(key => bySource.get(key)).find(index => index !== undefined)

    if (anchor === undefined) {
      if (!isUnstoredLiveTurnRow(message)) {
        pending.push(message)
      }

      continue
    }

    const complete =
      receiptBoundRowCovered(message, bySource) ||
      (keys.every(key => bySource.has(key)) &&
        !message.error &&
        message.parts.every(
          part => part.sourceDisplayIdentities?.length && part.sourceDisplayIdentities.every(key => bySource.has(key))
        ))

    if (!complete) {
      const parts = message.parts.filter(
        part => !part.sourceDisplayIdentities?.length || !part.sourceDisplayIdentities.every(key => bySource.has(key))
      )

      pending.push({ ...message, parts })
    }

    if (pending.length) {
      const before = anchored ? anchor : 0
      leading.set(before, [...(leading.get(before) ?? []), ...pending])
      pending = []
    }

    anchored = true
  }

  const result = [...fresh.flatMap((message, index) => [...(leading.get(index) ?? []), message]), ...pending]

  return result.length === fresh.length && result.every((message, index) => message === fresh[index]) ? fresh : result
}
