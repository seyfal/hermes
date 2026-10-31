import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import { getLatestSessionMessages } from '@/hermes'
import { createClientSessionState } from '@/lib/chat-runtime'
import { $activeGatewayProfile } from '@/store/profile'
import { $sessionStates, $sessionTiles, clearAllSessionStates } from '@/store/session-states'
import type { SessionMessagesResponse } from '@/types/hermes'

import { useTranscriptPeerSync } from './use-transcript-peer-sync'

const bus = vi.hoisted(() => ({ receive: (_payload: { sessionId: string }) => {} }))
vi.mock('@/store/transcript-sync', () => ({
  onTranscriptChanged: (receive: typeof bus.receive) => {
    bus.receive = receive

    return () => {}
  }
}))
vi.mock('@/hermes', async original => ({
  ...(await original<Record<string, unknown>>()),
  getLatestSessionMessages: vi.fn()
}))

function deferred() {
  let resolve!: (page: SessionMessagesResponse) => void
  let reject!: (error: Error) => void

  const promise = new Promise<SessionMessagesResponse>((yes, no) => {
    resolve = yes
    reject = no
  })

  return { promise, resolve, reject }
}

const page = (count: number): SessionMessagesResponse => ({
  session_id: 'stored',
  messages: Array.from({ length: count }, (_, index) => ({
    id: index + 1,
    role: index % 2 ? 'assistant' : 'user',
    content: `row ${index}`,
    timestamp: index
  }))
})

function mount() {
  const activeSessionIdRef = { current: 'runtime' as string | null }
  const selectedStoredSessionIdRef = { current: 'stored' as string | null }
  const busyRef = { current: false }

  const updates = vi.fn((id, updater) => {
    const next = updater($sessionStates.get()[id])
    $sessionStates.set({ ...$sessionStates.get(), [id]: next })

    return next
  })

  const view = renderHook(() =>
    useTranscriptPeerSync({
      activeSessionIdRef,
      selectedStoredSessionIdRef,
      busyRef,
      updateSessionState: updates
    })
  )

  return { ...view, activeSessionIdRef, selectedStoredSessionIdRef, updates }
}

beforeEach(() => {
  vi.mocked(getLatestSessionMessages).mockReset()
  clearAllSessionStates()
  $sessionTiles.set([])
  $activeGatewayProfile.set('default')
  $sessionStates.set({ runtime: createClientSessionState('stored', [{ id: 'seed', role: 'user', parts: [] }]) })
})
afterEach(() => {
  cleanup()
  clearAllSessionStates()
  $sessionTiles.set([])
})

it.each([true, false])('publishes only the latest read into the shared store, newest first=%s', async newestFirst => {
  const first = deferred()
  const second = deferred()
  vi.mocked(getLatestSessionMessages).mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise)
  const view = mount()
  act(() => {
    bus.receive({ sessionId: 'stored' })
    bus.receive({ sessionId: 'stored' })
  })

  const completions = newestFirst
    ? ([
        [second, 4],
        [first, 2]
      ] as const)
    : ([
        [first, 2],
        [second, 4]
      ] as const)

  for (const [request, count] of completions) {
    await act(async () => request.resolve(page(count)))
  }

  expect(view.updates).toHaveBeenCalledTimes(1)
  expect($sessionStates.get().runtime.messages).toHaveLength(4)
})

it.each(['selection', 'runtime', 'profile', 'unmount'] as const)(
  'discards a read after its %s changes',
  async change => {
    const request = deferred()
    vi.mocked(getLatestSessionMessages).mockReturnValue(request.promise)
    const view = mount()
    act(() => bus.receive({ sessionId: 'stored' }))

    if (change === 'selection') {view.selectedStoredSessionIdRef.current = 'other'}

    if (change === 'runtime') {view.activeSessionIdRef.current = 'other'}

    if (change === 'profile') {$activeGatewayProfile.set('other')}

    if (change === 'unmount') {view.unmount()}
    await act(async () => request.resolve(page(2)))
    expect(view.updates).not.toHaveBeenCalled()
  }
)

it.each(['connection', 'profile', 'stored', 'closed'] as const)(
  'discards a tile read after its %s changes',
  async change => {
    const request = deferred()
    vi.mocked(getLatestSessionMessages).mockReturnValue(request.promise)

    const tile = {
      storedSessionId: 'stored',
      runtimeId: 'tile-runtime',
      ownerRoute: { connectionId: 'one', profile: 'default' }
    }

    $sessionTiles.set([tile])
    $sessionStates.set({ ...$sessionStates.get(), 'tile-runtime': createClientSessionState('stored') })
    const view = mount()
    view.selectedStoredSessionIdRef.current = 'other'
    act(() => bus.receive({ sessionId: 'stored' }))
    $sessionTiles.set(
      change === 'closed'
        ? []
        : [
            {
              ...tile,
              storedSessionId: change === 'stored' ? 'other' : 'stored',
              ownerRoute: {
                connectionId: change === 'connection' ? 'two' : 'one',
                profile: change === 'profile' ? 'other' : 'default'
              }
            }
          ]
    )
    await act(async () => request.resolve(page(2)))
    expect(view.updates).not.toHaveBeenCalled()
  }
)

it('starts split-pane reads without waiting for the foreground and keeps their owners separate', async () => {
  const foreground = deferred()
  const tile = deferred()
  vi.mocked(getLatestSessionMessages).mockReturnValueOnce(foreground.promise).mockReturnValueOnce(tile.promise)
  $sessionTiles.set([
    { storedSessionId: 'stored', runtimeId: 'tile-runtime', ownerRoute: { connectionId: 'remote', profile: 'bot' } }
  ])
  $sessionStates.set({ ...$sessionStates.get(), 'tile-runtime': createClientSessionState('stored') })
  mount()
  act(() => bus.receive({ sessionId: 'stored' }))
  expect(getLatestSessionMessages).toHaveBeenCalledTimes(2)
  expect(getLatestSessionMessages).toHaveBeenLastCalledWith('stored', { connectionId: 'remote', profile: 'bot' })
  await act(async () => tile.resolve(page(4)))
  expect($sessionStates.get()['tile-runtime'].messages).toHaveLength(4)
  expect($sessionStates.get().runtime.messages).toHaveLength(1)
  await act(async () => foreground.resolve(page(2)))
})

it('keeps the displayed transcript if the newest request fails rather than publishing an older snapshot', async () => {
  const first = deferred()
  const second = deferred()
  vi.mocked(getLatestSessionMessages).mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise)
  const view = mount()
  act(() => {
    bus.receive({ sessionId: 'stored' })
    bus.receive({ sessionId: 'stored' })
  })
  await act(async () => second.reject(new Error('offline')))
  await act(async () => first.resolve(page(2)))
  expect(view.updates).not.toHaveBeenCalled()
  expect($sessionStates.get().runtime.messages).toHaveLength(1)
})
