import type { ChatMessage } from './chat-messages'
import { assistantPartCovered, assistantPartOccurrence } from './chat-messages/part-coverage'

/** A throttled snapshot may stop before the final text, but not before its identity. */
export function durableFinalCoversJournal(base: ChatMessage, journal: ChatMessage): boolean {
  const final = base.parts.findLast(part => part.type === 'text')
  const finalRowId = final?.sourceRowId ?? base.rowId
  const journalRowId = journal.persistedTurn?.final_assistant_row_id ?? journal.rowId

  if (base.durableComplete !== true || journal.persistedTurn?.complete === false ||
    journalRowId === undefined || finalRowId !== journalRowId || final?.type !== 'text') {
    return false
  }

  const unaddressedText = journal.parts.filter(part => part.type === 'text' && part.sourceRowId === undefined)

  return journal.parts.every(part => {
    if (part.type === 'text' && (part.sourceRowId === finalRowId ||
      (part.sourceRowId === undefined && unaddressedText.length === 1))) {
      return final.text.startsWith(part.text)
    }

    const occurrence = assistantPartOccurrence(part)

    const candidates = base.parts.filter(candidate =>
      (occurrence === undefined || assistantPartOccurrence(candidate) === occurrence) && assistantPartCovered(candidate, part)
    )

    return candidates.length === 1
  })
}
