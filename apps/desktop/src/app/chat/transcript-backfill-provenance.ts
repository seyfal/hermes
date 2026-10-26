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

export function sharesTranscriptSource(first: ChatMessage[], second: ChatMessage[]): boolean {
  const keys = new Set(first.flatMap(transcriptSourceKeys))

  return second.some(message => transcriptSourceKeys(message).some(key => keys.has(key)))
}

/** Keep server array order, placing retained-only rows immediately before their next shared anchor. */
export function mergeDisplayOrder(previous: number[], refreshed: number[]): number[] {
  const fresh = new Set(refreshed)
  const leading = new Map<number, number[]>()
  let pending: number[] = []
  let anchored = false

  for (const row of previous) {
    if (fresh.has(row)) {
      const before = anchored ? row : refreshed[0]
      leading.set(before, [...(leading.get(before) ?? []), ...pending])
      pending = []
      anchored = true
    } else {
      pending.push(row)
    }
  }

  return [...refreshed.flatMap(row => [...(leading.get(row) ?? []), row]), ...pending]
}

/** Canonical occurrences survive physical re-IDs and carry every source in a folded bubble. */
export function mergeProvenancePage(fresh: ChatMessage[], previous: ChatMessage[], older = false): ChatMessage[] {
  const bySource = new Map<string, number>()
  fresh.forEach((message, index) => {
    for (const key of transcriptSourceKeys(message)) {
      bySource.set(key, index)
    }
  })

  if (!sharesTranscriptSource(fresh, previous)) {
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
      keys.every(key => bySource.has(key)) &&
      !message.error &&
      message.parts.every(
        part => part.sourceDisplayIdentities?.length && part.sourceDisplayIdentities.every(key => bySource.has(key))
      )

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
