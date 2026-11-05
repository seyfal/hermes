import { assistantPartCovered, assistantPartOccurrence } from './part-coverage'
import type { ChatMessage, ChatMessagePart } from './types'

function uniqueOccurrences(parts: ChatMessagePart[]): Map<string, number> {
  const positions = new Map<string, number>()

  parts.forEach((part, index) => {
    const key = assistantPartOccurrence(part)

    if (key !== undefined) {
      positions.set(key, positions.has(key) ? -1 : index)
    }
  })

  return positions
}

/** A partial live fold can start after earlier durable tool rounds. */
function coveredPrefixOffset(stored: ChatMessagePart[], local: ChatMessage[]): number {
  const boundary = local.findIndex(message => message.role !== 'assistant' || message.error)
  const localParts = (boundary < 0 ? local : local.slice(0, boundary)).flatMap(message => message.parts)
  const storedPositions = uniqueOccurrences(stored)
  const offsets = new Set<number>()

  for (const [key, index] of uniqueOccurrences(localParts)) {
    const storedIndex = storedPositions.get(key)

    if (index >= 0 && storedIndex !== undefined && storedIndex >= index) {
      offsets.add(storedIndex - index)
    }
  }

  return offsets.size === 1 ? offsets.values().next().value! : -1
}

/** Subtract an ordered, occurrence-anchored prefix within an already matched
 * user interval. Equal prose alone cannot establish coverage. */
export function withoutCoveredAssistantPrefix(stored: ChatMessage[], local: ChatMessage[]): ChatMessage[] {
  const parts = stored.flatMap(message => (message.role === 'assistant' ? message.parts : []))
  let cursor = coveredPrefixOffset(parts, local)

  if (cursor < 0) {
    return local
  }

  let anchored = false
  let stopped = false
  const remaining: ChatMessage[] = []

  for (const message of local) {
    if (stopped || message.role !== 'assistant' || message.error) {
      stopped = true
      remaining.push(message)

      continue
    }

    let consumed = 0

    for (const part of message.parts) {
      if (!parts[cursor] || !assistantPartCovered(parts[cursor], part)) {
        break
      }

      anchored ||= assistantPartOccurrence(part) !== undefined
      cursor += 1
      consumed += 1
    }

    if (consumed < message.parts.length) {
      stopped = true
      remaining.push(consumed ? { ...message, parts: message.parts.slice(consumed) } : message)
    }
  }

  return anchored ? remaining : local
}
