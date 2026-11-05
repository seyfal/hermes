import { normalizeWs } from './parts'
import type { ChatMessagePart } from './types'

/** Identity locates an occurrence, but richer local payloads still need recovery. */
export function assistantPartCovered(stored: ChatMessagePart, local: ChatMessagePart): boolean {
  if (stored.type !== local.type || (local.sourceRowId !== undefined && stored.sourceRowId !== local.sourceRowId)) {
    return false
  }

  if ((stored.type === 'text' || stored.type === 'reasoning') && local.type === stored.type) {
    return normalizeWs(stored.text) === normalizeWs(local.text)
  }

  const ignored = new Set(['timestamp', 'completedAt', 'sourceRowId'])

  if (local.type === 'tool-call') {
    // Hydration may omit derived argument text and empty result metadata.
    if (!local.argsText || local.argsText === JSON.stringify(local.args)) {
      ignored.add('argsText')
    }

    if (!Object.keys(local.toolResultMetadata ?? {}).length) {
      ignored.add('toolResultMetadata')
    }
  }

  return Object.entries(local).every(([key, value]) =>
    ignored.has(key) || value === undefined || JSON.stringify(Reflect.get(stored, key)) === JSON.stringify(value)
  )
}

export function assistantPartOccurrence(part: ChatMessagePart): string | undefined {
  if (part.type === 'tool-call' && part.toolCallId) {
    return `tool:${part.toolCallId}`
  }

  return part.sourceRowId === undefined ? undefined : `${part.type}:${part.sourceRowId}`
}
