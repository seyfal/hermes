import { type MutableRefObject, useEffect, useRef } from 'react'

import {
  hydrateStoredSessionTranscript,
  profileScopeForTranscriptSession,
  resolveActiveTranscriptSession
} from '@/app/contrib/hooks/use-background-sync'
import type { ClientSessionState } from '@/app/types'
import type { ProfileScope } from '@/hermes'
import { $activeGatewayProfile } from '@/store/profile'
import { $connection } from '@/store/session'
import { $sessionTiles, type SessionTile } from '@/store/session-states'
import { onTranscriptChanged } from '@/store/transcript-sync'

interface TranscriptPeerSyncOptions {
  activeSessionIdRef: MutableRefObject<string | null>
  busyRef: MutableRefObject<boolean>
  selectedStoredSessionIdRef: MutableRefObject<string | null>
  updateSessionState: (
    sessionId: string,
    updater: (state: ClientSessionState) => ClientSessionState,
    storedSessionId?: string | null
  ) => ClientSessionState
}

/** Ambient routing matters only when the target has no explicit connection/profile. */
function scopeKey(scope: ProfileScope): string {
  return JSON.stringify([
    typeof scope === 'object' && scope?.connectionId ? scope.connectionId : $connection.get(),
    typeof scope === 'string' ? scope : (scope?.profile ?? $activeGatewayProfile.get())
  ])
}

function tileScope(tile: SessionTile): ProfileScope {
  return profileScopeForTranscriptSession(
    tile.ownerRoute
      ? { ownerRoute: tile.ownerRoute }
      : tile.ownerProfile
        ? { profile: tile.ownerProfile }
        : resolveActiveTranscriptSession(tile.storedSessionId, tile.runtimeId!)
  )
}

/**
 * When another window finishes a turn on a stored session this window is
 * showing, re-pull that transcript. The completing window does not receive
 * its own broadcast. Submit still refuses if this pull has not landed yet.
 */
export function useTranscriptPeerSync({
  activeSessionIdRef,
  busyRef,
  selectedStoredSessionIdRef,
  updateSessionState
}: TranscriptPeerSyncOptions): void {
  // Multiple completed-turn broadcasts can arrive before the first persisted
  // transcript read returns. Keep only the newest snapshot per runtime: an
  // earlier response would otherwise overwrite the later one on arrival.
  const refreshSequenceByRuntimeRef = useRef(new Map<string, number>())

  useEffect(() => {
    let cancelled = false

    const latestRefresh = (runtimeId: string) => {
      const sequence = (refreshSequenceByRuntimeRef.current.get(runtimeId) ?? 0) + 1
      refreshSequenceByRuntimeRef.current.set(runtimeId, sequence)

      return () => !cancelled && refreshSequenceByRuntimeRef.current.get(runtimeId) === sequence
    }

    const unsubscribe = onTranscriptChanged(payload => {
      const storedSessionId = payload.sessionId

      if (!storedSessionId) {
        return
      }

      const runtimeId = activeSessionIdRef.current

      const activeScope = () =>
        profileScopeForTranscriptSession(resolveActiveTranscriptSession(storedSessionId, runtimeId!))

      const selected = selectedStoredSessionIdRef.current === storedSessionId

      if (selected && runtimeId && !busyRef.current) {
        const storedProfile = activeScope()
        const owner = scopeKey(storedProfile)
        const latest = latestRefresh(runtimeId)
        void hydrateStoredSessionTranscript({
          attempts: 1,
          isCurrent: () =>
            latest() &&
            activeSessionIdRef.current === runtimeId &&
            selectedStoredSessionIdRef.current === storedSessionId &&
            !busyRef.current &&
            scopeKey(activeScope()) === owner,
          runtimeSessionId: runtimeId,
          storedProfile,
          storedSessionId,
          updateSessionState
        })
      }

      // Capture and fence all targets before awaiting any read. A slow foreground
      // must not let an older notification launch a newer tile request later.
      for (const tile of $sessionTiles.get()) {
        if (tile.storedSessionId !== storedSessionId || !tile.runtimeId || (selected && tile.runtimeId === runtimeId)) {
          continue
        }

        const storedProfile = tileScope(tile)
        const owner = scopeKey(storedProfile)
        const latest = latestRefresh(tile.runtimeId)
        void hydrateStoredSessionTranscript({
          attempts: 1,
          isCurrent: () =>
            latest() &&
            $sessionTiles
              .get()
              .some(
                current =>
                  current.runtimeId === tile.runtimeId &&
                  current.storedSessionId === storedSessionId &&
                  scopeKey(tileScope(current)) === owner
              ),
          runtimeSessionId: tile.runtimeId,
          storedProfile,
          storedSessionId,
          updateSessionState
        })
      }
    })

    return () => {
      cancelled = true
      unsubscribe()
    }
  }, [activeSessionIdRef, busyRef, selectedStoredSessionIdRef, updateSessionState])
}
