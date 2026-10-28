import { type ChatMessage, type ChatMessagePart, mergeFinalAssistantText } from '@/lib/chat-messages'

/**
 * A sealed stream can lose a few characters while the authoritative final
 * remains the same reply. Limit the tolerated edit distance so a separate
 * assistant segment cannot replace a merely similar interim.
 */
function hasHighTextOverlap(left: string, right: string): boolean {
  const maxLength = Math.max(left.length, right.length)

  if (maxLength < 160) {
    return false
  }

  const maxEdits = Math.max(1, Math.min(32, Math.floor(maxLength * 0.02)))

  if (Math.abs(left.length - right.length) > maxEdits) {
    return false
  }

  const [shorter, longer] = left.length < right.length ? [left, right] : [right, left]

  let previous = Array.from({ length: shorter.length + 1 }, (_, index) =>
    index <= maxEdits ? index : Number.POSITIVE_INFINITY
  )

  let current = new Array<number>(shorter.length + 1).fill(Number.POSITIVE_INFINITY)

  for (let longerIndex = 1; longerIndex <= longer.length; longerIndex += 1) {
    const start = Math.max(1, longerIndex - maxEdits)
    const end = Math.min(shorter.length, longerIndex + maxEdits)
    current.fill(Number.POSITIVE_INFINITY, start, end + 1)
    current[start - 1] = start === 1 ? longerIndex : Number.POSITIVE_INFINITY

    let rowMinimum = Number.POSITIVE_INFINITY

    for (let shorterIndex = start; shorterIndex <= end; shorterIndex += 1) {
      current[shorterIndex] = Math.min(
        previous[shorterIndex] + 1,
        current[shorterIndex - 1] + 1,
        previous[shorterIndex - 1] + Number(longer[longerIndex - 1] !== shorter[shorterIndex - 1])
      )
      rowMinimum = Math.min(rowMinimum, current[shorterIndex])
    }

    if (rowMinimum > maxEdits) {
      return false
    }

    const nextPrevious = current
    current = previous
    previous = nextPrevious
  }

  return previous[shorter.length] <= maxEdits
}

/** A completion may extend or minimally correct this turn's sealed interim. */
export function continuesInterimResponse(interim: boolean | undefined, finalText: string, existingText: string): boolean {
  return Boolean(
    interim && finalText && existingText &&
    (finalText === existingText || finalText.startsWith(existingText) ||
      existingText.startsWith(finalText) || hasHighTextOverlap(finalText, existingText))
  )
}

/** One live bubble may hold several model responses when an equal interim
 * callback is suppressed. The last tool round, not the bubble, bounds the
 * response that an interim/final frame is allowed to replace. */
export function currentResponseParts(parts: ChatMessagePart[]): ChatMessagePart[] {
  return parts.slice(parts.findLastIndex(part => part.type === 'tool-call') + 1)
}

export function mergeCurrentResponseText(parts: ChatMessagePart[], text: string, timestamp: number): ChatMessagePart[] {
  const boundary = parts.findLastIndex(part => part.type === 'tool-call') + 1

  return [...parts.slice(0, boundary), ...mergeFinalAssistantText(parts.slice(boundary), text, timestamp)]
}

/** Hydration can replace a streaming id before its terminal frame arrives.
 * The receipt still names the same stored response, even if a background reply
 * follows it. A folded bubble's final text source takes precedence over its
 * first row address so an earlier response cannot replace its final suffix. */
export function persistedFinalBubbleIndex(
  messages: readonly ChatMessage[],
  lastUserIndex: number,
  finalRowId: number | null | undefined
): number {
  if (lastUserIndex < 0 || typeof finalRowId !== 'number' || !Number.isSafeInteger(finalRowId) || finalRowId <= 0) {
    return -1
  }

  return messages.findLastIndex((message, index) => {
    if (index <= lastUserIndex || message.role !== 'assistant' || message.hidden) {
      return false
    }

    const finalTextPart = message.parts.findLast(part => part.type === 'text')

    return (finalTextPart?.sourceRowId ?? message.rowId) === finalRowId
  })
}
