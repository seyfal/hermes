import type { ChatMessage } from '@/lib/chat-messages'
import { assistantPartCovered, assistantPartOccurrence } from '@/lib/chat-messages/part-coverage'

/** Locate a closed user interval, including a refreshed page starting mid-turn. */
function refreshedInterval(previous: ChatMessage[], index: number, refreshed: ChatMessage[]): ChatMessage[] {
  const start = previous.findLastIndex((message, offset) => offset < index && message.role === 'user')
  const end = previous.findIndex((message, offset) => offset > index && message.role === 'user')
  const first = previous[start]?.rowId
  const last = previous[end]?.rowId

  if (first === undefined || last === undefined) {
    return []
  }

  const freshEnd = refreshed.findIndex(message => message.role === 'user' && message.rowId === last)
  const freshStart = refreshed.findLastIndex((message, offset) => offset < freshEnd && message.role === 'user')

  if (freshEnd < 0 || (freshStart >= 0 && refreshed[freshStart].rowId !== first)) {
    return []
  }

  // This merge uses stored-row order. A truncated page must still fit between
  // the same two user rows, rather than borrowing equal prose from an older turn.
  return refreshed.slice(freshStart + 1, freshEnd).filter(message =>
    message.role === 'assistant' && !message.hidden && !message.pending && !message.error &&
    message.rowId !== undefined && message.rowId > first && message.rowId < last
  )
}

/** Subtract represented parts, never a whole mixed bubble based on its prose. */
export function withoutRefreshedInterimParts(previous: ChatMessage[], refreshed: ChatMessage[]): ChatMessage[] {
  return previous.flatMap((message, index) => {
    if (message.role !== 'assistant' || message.rowId !== undefined || !message.interim || message.pending || message.error || message.hidden) {
      return [message]
    }

    const interval = refreshedInterval(previous, index, refreshed)
    const stored = interval.flatMap(candidate => candidate.parts)
    const textOnly = message.parts.every(part => part.type === 'text')
    const start = previous.findLastIndex((candidate, offset) => offset < index && candidate.role === 'user')
    const end = previous.findIndex((candidate, offset) => offset > index && candidate.role === 'user')
    const local = previous.slice(start + 1, end).flatMap(candidate => candidate.role === 'assistant' ? candidate.parts : [])

    const parts = message.parts.filter(part => {
      const occurrence = assistantPartOccurrence(part)

      const candidates = stored.filter(candidate => occurrence
        ? assistantPartOccurrence(candidate) === occurrence
        : textOnly && part.type === 'text' && assistantPartCovered(candidate, part))

      // Old runtimes have no source address on sealed text. Permit only an
      // exact, unique text-only counterpart in this closed user interval.
      const occurrences = local.filter(candidate => occurrence
        ? assistantPartOccurrence(candidate) === occurrence
        : assistantPartCovered(candidate, part))

      return occurrences.length !== 1 || candidates.length !== 1 || !assistantPartCovered(candidates[0], part)
    })

    if (parts.length === message.parts.length) {
      return [message]
    }

    return parts.length ? [{ ...message, parts }] : []
  })
}

/** Keep uncovered closed-turn remnants when a later refresh takes the splice path. */
export function retainUncoveredInterims(messages: ChatMessage[], previous: ChatMessage[]): ChatMessage[] {
  const remaining = withoutRefreshedInterimParts(previous, messages)
  let result = messages

  for (const [index, message] of remaining.entries()) {
    if (!message.interim || message.rowId !== undefined || result.some(candidate => candidate.id === message.id)) {
      continue
    }

    if (!refreshedInterval(remaining, index, messages).length) {
      continue
    }

    const nextUser = remaining.find((candidate, offset) => offset > index && candidate.role === 'user')
    const position = result.findIndex(candidate => candidate.role === 'user' && candidate.rowId === nextUser?.rowId)

    if (position >= 0) {
      result = [...result.slice(0, position), message, ...result.slice(position)]
    }
  }

  return result
}
