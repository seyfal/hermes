import { type RefObject, useEffect, useRef } from 'react'

import {
  readThreadScrollResizeMetrics,
  shouldReapplyFrozenThreadScrollOffset,
  THREAD_SCROLL_BOTTOM,
  type ThreadScrollState,
  threadScrollStateFromMetrics,
  threadScrollTargetTop
} from '@/store/thread-scroll'

interface ScrollMetricsOptions {
  viewport: RefObject<HTMLElement | null>
  content: RefObject<HTMLElement | null>
  clearance: RefObject<HTMLElement | null>
  following: RefObject<boolean>
  setFollowing: (following: boolean) => void
  paneVisible: boolean
  isHistorical: boolean
  selecting: (element: HTMLElement) => boolean
}

// use-stick-to-bottom targets one pixel before the maximum scrollTop.
export const isAtThreadBottom = (node: HTMLElement) => node.scrollHeight - node.clientHeight - node.scrollTop <= 1

/** Record visible state and close the frame before the library's resize follow. */
export function useThreadScrollMetrics({
  viewport, content, clearance, following, setFollowing, paneVisible, isHistorical, selecting
}: ScrollMetricsOptions) {
  const liveState = useRef<ThreadScrollState>(THREAD_SCROLL_BOTTOM)

  // eslint-disable-next-line no-restricted-syntax -- DOM-event metrics, not an atom mirror
  useEffect(() => {
    const el = viewport.current
    const body = content.current

    if (!el || !body || !paneVisible) {
      return
    }

    const measure = () => readThreadScrollResizeMetrics(el, clearance.current)
    let previous = measure()
    let previousTop = el.scrollTop

    const update = () => {
      const next = measure()
      const moved = el.scrollTop !== previousTop
      const resized = next.scrollHeight !== previous.scrollHeight || next.clientHeight !== previous.clientHeight

      if (!isHistorical && moved) {
        if (isAtThreadBottom(el)) {
          setFollowing(true)
        } else if (!resized || el.scrollTop < previousTop) {
          setFollowing(false)
        }
      }

      previous = next
      previousTop = el.scrollTop
      liveState.current = following.current ? THREAD_SCROLL_BOTTOM : threadScrollStateFromMetrics(el, 1)
    }

    const onResize = () => {
      const next = measure()

      if (
        following.current && !isHistorical &&
        !el.hasAttribute('data-editing') && !selecting(el) &&
        shouldReapplyFrozenThreadScrollOffset(THREAD_SCROLL_BOTTOM, true, previous, next)
      ) {
        el.scrollTop = threadScrollTargetTop(THREAD_SCROLL_BOTTOM, next)
      }

      previous = next
      previousTop = el.scrollTop
      liveState.current = following.current ? THREAD_SCROLL_BOTTOM : threadScrollStateFromMetrics(el, 1)
    }

    el.addEventListener('scroll', update, { passive: true })
    const observer = new ResizeObserver(onResize)
    observer.observe(body)

    return () => {
      el.removeEventListener('scroll', update)
      observer.disconnect()
    }
  }, [clearance, content, following, isHistorical, paneVisible, selecting, setFollowing, viewport])

  return liveState
}
