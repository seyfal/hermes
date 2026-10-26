import { useCallback, useMemo, useRef, useState } from 'react'

/** Pane-local intent, independent of layout clamps and the library's near-bottom band. */
export function useThreadScrollOwner() {
  const [following, setFollowing] = useState(true)
  const current = useRef(true)

  const set = useCallback((next: boolean) => {
    current.current = next
    setFollowing(next)
  }, [])

  return useMemo(() => ({ following, current, set }), [following, set])
}
