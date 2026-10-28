import type { SessionMessage } from '@/types/hermes'

import type { ChatMessage, TimelinePartMetadata } from './types'

export function storedDisplayIdentity(message: SessionMessage): string | undefined {
  return message.session_id && message.display_identity
    ? JSON.stringify([message.session_id, message.display_identity])
    : undefined
}

export function storedMessageIdentity(message: SessionMessage): Pick<ChatMessage, 'displayIdentity'> {
  const identity = storedDisplayIdentity(message)

  return identity ? { displayIdentity: identity } : {}
}

export function storedPartIdentity(message: SessionMessage, existing: TimelinePartMetadata = {}): TimelinePartMetadata {
  const identity = storedDisplayIdentity(message)

  return identity ? { sourceDisplayIdentities: [...(existing.sourceDisplayIdentities ?? []), identity] } : {}
}

export function messageDisplayIdentities(message: ChatMessage): string[] {
  return [
    ...new Set([
      ...(message.displayIdentity ? [message.displayIdentity] : []),
      ...message.parts.flatMap(part => part.sourceDisplayIdentities ?? [])
    ])
  ]
}
