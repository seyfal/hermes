import { useCallback, useRef, useState } from 'react'
import { type GetTargetScrollTop, useStickToBottom } from 'use-stick-to-bottom'

/** Reader intent belongs to this pane, not to a layout-derived near-bottom band. */
export function useThreadScrollOwner(resolveTarget: GetTargetScrollTop, selecting: (node: Element | null) => boolean) {
  const [following, setFollowingState] = useState(true)
  const followingRef = useRef(true)

  const setFollowing = useCallback((next: boolean) => {
    followingRef.current = next
    setFollowingState(next)
  }, [])

  const { scrollRef, contentRef, scrollToBottom, stopScroll } = useStickToBottom({
    initial: 'instant',
    resize: 'instant',
    targetScrollTop: (top, elements) => followingRef.current ? resolveTarget(top, elements) : elements.scrollElement.scrollTop
  })

  // Explicit snaps respect the same text-selection pin as resize following.
  const scrollToBottomUnlessSelecting = useCallback(
    (...args: Parameters<typeof scrollToBottom>) => {
      if (selecting(scrollRef.current)) {
        return undefined
      }

      setFollowing(true)

      return scrollToBottom(...args)
    },
    [scrollRef, scrollToBottom, selecting, setFollowing]
  )

  return { following, followingRef, setFollowing, scrollRef, contentRef, scrollToBottomUnlessSelecting, stopScroll }
}
