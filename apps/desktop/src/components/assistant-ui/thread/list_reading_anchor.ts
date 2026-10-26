export interface ReadingAnchor {
  node: HTMLElement
  top: number
}

const GROUP = '[data-slot="aui_message-group"]'

/** Keep the actual mounted occurrence, not an id shared by repeated messages. */
export function captureReadingAnchor(viewport: HTMLElement): ReadingAnchor | null {
  const top = viewport.getBoundingClientRect().top
  const bottom = top + viewport.clientHeight

  for (const group of viewport.querySelectorAll<HTMLElement>(GROUP)) {
    const rect = group.getBoundingClientRect()

    if (rect.bottom <= top || rect.top >= bottom) {
      continue
    }

    // A tall response can reflow within its turn. Prefer the visible text block
    // over the turn's top, which may be several screens above the reader.
    for (const node of group.querySelectorAll<HTMLElement>('p, pre, li')) {
      const block = node.getBoundingClientRect()

      if (block.bottom > top && block.top < bottom) {
        return { node, top: block.top - top }
      }
    }

    return { node: group, top: rect.top - top }
  }

  return null
}

export function readingAnchorTop(viewport: HTMLElement, anchor: ReadingAnchor | null): number | undefined {
  return anchor && viewport.contains(anchor.node)
    ? anchor.node.getBoundingClientRect().top - viewport.getBoundingClientRect().top
    : undefined
}

/** Native anchoring may already have compensated. Apply only what remains. */
export function restoreReadingAnchor(viewport: HTMLElement, anchor: ReadingAnchor | null): boolean {
  const top = readingAnchorTop(viewport, anchor)

  if (top === undefined || !anchor) {
    return false
  }

  viewport.scrollTop += top - anchor.top

  return true
}
