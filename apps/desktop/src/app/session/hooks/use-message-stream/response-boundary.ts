import type { ChatMessage } from '@/lib/chat-messages'

/** A projected queued prompt belongs to the next turn until message.start. */
export function currentResponseUserIndex(messages: ChatMessage[], sessionId: string, hasCurrentResponse: boolean): number {
  return messages.findLastIndex(message =>
    message.role === 'user' && !(hasCurrentResponse && message.id === `user-queued-${sessionId}`)
  )
}
