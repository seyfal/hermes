import type { GatewayEvent } from '@hermes/shared'
import { QueryClient } from '@tanstack/react-query'
import { act, cleanup, renderHook } from '@testing-library/react'
import { useRef } from 'react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import { hydrateStoredSessionTranscript } from '@/app/contrib/hooks/use-background-sync'
import { getLatestSessionMessages } from '@/hermes'
import { chatMessageText } from '@/lib/chat-messages'
import { resetInFlightTurnJournalStateForTests } from '@/lib/inflight-turn-journal'
import { $activeGatewayProfile } from '@/store/profile'
import {
  _resetSessionOwnerHintsForTests,
  setActiveSessionId,
  setAwaitingResponse,
  setBusy,
  setConnection,
  setMessages,
  setSelectedStoredSessionId,
  setSessions
} from '@/store/session'
import { clearAllSessionStates } from '@/store/session-states'
import type { SessionMessage, SessionResumeResult } from '@/types/hermes'

import { useMessageStream } from './use-message-stream'
import { useSessionActions } from './use-session-actions'
import { useSessionStateCache } from './use-session-state-cache'

// Seams mocked: the gateway RPC transport (requestGateway) and the REST transcript read.
vi.mock('@/hermes', async original => ({
  ...(await original<Record<string, unknown>>()),
  getLatestSessionMessages: vi.fn()
}))
vi.mock('@/store/profile', async original => ({
  ...(await original<Record<string, unknown>>()),
  ensureGatewayProfile: vi.fn().mockResolvedValue(undefined)
}))

const storedId = 'rv-runtime-stored'
const runtimeId = 'rv-runtime-live'
const noop = async () => undefined
const NOTICE = 'Background process proc_1 finished'

const prior: SessionMessage[] = [
  { id: 1, role: 'user', content: 'Start the build in the background', timestamp: 1 },
  { id: 2, role: 'assistant', content: 'Started; I will report when it finishes.', timestamp: 2 }
]

const notice: SessionMessage = {
  id: 3,
  role: 'user',
  content: NOTICE,
  display_kind: 'process_complete',
  timestamp: 10.5
}

function mount(snapshot: () => SessionResumeResult) {
  const requestGateway = vi.fn().mockImplementation(async () => snapshot())

  const hook = renderHook(() => {
    const busyRef = useRef(false)
    const creatingSessionRef = useRef(false)
    const queryClient = useRef(new QueryClient()).current

    const cache = useSessionStateCache({
      activeSessionId: null,
      selectedStoredSessionId: null,
      busyRef,
      setMessages,
      setBusy,
      setAwaitingResponse
    })

    const actions = useSessionActions({
      ...cache,
      activeSessionId: null,
      selectedStoredSessionId: null,
      busyRef,
      creatingSessionRef,
      getRouteToken: () => 'A',
      getRoutedStoredSessionId: () => null,
      navigate: vi.fn(),
      requestGateway,
      routedSessionId: null
    })

    // The production post-turn rehydrate (app/contrib/wiring.tsx), REST mocked.
    const hydrateFromStoredSession = (
      attempts = 1,
      stored: string | null = storedId,
      runtime: string | null = runtimeId
    ) =>
      hydrateStoredSessionTranscript({
        attempts,
        storedSessionId: stored ?? storedId,
        runtimeSessionId: runtime ?? runtimeId,
        storedProfile: undefined,
        updateSessionState: cache.updateSessionState
      })

    const stream = useMessageStream({
      ...cache,
      queryClient,
      hydrateFromStoredSession,
      refreshHermesConfig: noop,
      refreshSessions: noop
    })

    return { cache, actions, stream }
  })

  return hook
}

function resetRenderer() {
  cleanup()
  clearAllSessionStates()
  // A renderer restart loses module timers/state, never localStorage.
  resetInFlightTurnJournalStateForTests()
  setMessages([])
  setActiveSessionId(null)
  setSelectedStoredSessionId(null)
  setBusy(false)
  setAwaitingResponse(false)
}

beforeEach(() => {
  vi.useFakeTimers()
  localStorage.clear()
  resetRenderer()
  _resetSessionOwnerHintsForTests()
  $activeGatewayProfile.set('default')
  setConnection(null)
  setSessions([
    {
      id: storedId,
      title: storedId,
      source: 'desktop',
      message_count: 3,
      tool_call_count: 0,
      is_active: false,
      started_at: 1,
      last_active: 1,
      ended_at: null,
      model: null,
      preview: null,
      input_tokens: 0,
      output_tokens: 0
    }
  ])
  vi.mocked(getLatestSessionMessages).mockReset()
})
afterEach(() => {
  resetRenderer()
  localStorage.clear()
  setSessions([])
  vi.useRealTimers()
  vi.restoreAllMocks()
})

it.each([
  [false, 'process_complete'],
  [true, 'process_complete'],
  [false, 'async_delegation_complete'],
  [true, 'async_delegation_complete']
] as const)('re-activates a wake without duplicating output, REST first=%s kind=%s', async (restFirst, kind) => {
  let snapshot: SessionResumeResult = {
    session_id: runtimeId,
    resumed: storedId,
    messages: [],
    message_count: 2,
    running: false
  }

  vi.mocked(getLatestSessionMessages).mockResolvedValue({ session_id: storedId, messages: prior })
  const view = mount(() => snapshot)
  await act(async () => {
    await view.result.current.actions.resumeSession(storedId, true)
  })

  const send = (type: GatewayEvent['type'], payload: GatewayEvent['payload'] = {}) =>
    act(() => view.result.current.stream.handleGatewayEvent({ session_id: runtimeId, type, payload }))

  // A background-process wake runs in this session while it is open.
  send('message.start')
  send('message.delta', { text: 'Checking the log.' })
  send('message.interim', { text: 'Checking the log.', already_streamed: true })
  send('tool.start', { name: 'read_file', tool_id: 'call-1', args: {} })
  send('tool.complete', { name: 'read_file', tool_id: 'call-1', result: 'ok' })
  send('message.delta', { text: 'The build passed' })
  await act(async () => {
    await vi.advanceTimersByTimeAsync(600)
  })

  // The user switches back to it (activate) while the wake still runs.
  snapshot = {
    session_id: runtimeId,
    resumed: storedId,
    messages: [],
    messages_omitted: true,
    message_count: 5,
    running: true,
    turn_started_at: 10,
    inflight: {
      user: NOTICE,
      display_kind: kind,
      user_originated: false,
      assistant: 'Checking the log.\n\nThe build passed',
      streaming: true
    }
  } as SessionResumeResult
  vi.mocked(getLatestSessionMessages).mockResolvedValue({
    session_id: storedId,
    messages: [
      ...prior,
      { ...notice, display_kind: kind },
      {
        id: 4,
        role: 'assistant',
        content: 'Checking the log.',
        timestamp: 11,
        tool_calls: [{ id: 'call-1', type: 'function', function: { name: 'read_file', arguments: '{}' } }]
      },
      { id: 5, role: 'tool', content: 'ok', tool_call_id: 'call-1', timestamp: 12 }
    ]
  })
  await act(async () => {
    await view.result.current.actions.resumeSession(storedId, true)
  })
  await act(async () => {
    await vi.advanceTimersByTimeAsync(600)
  })

  const summarize = () => {
    const now = view.result.current.cache.sessionStateByRuntimeIdRef.current.get(runtimeId)!.messages

    const text = now
      .filter(m => m.role === 'assistant')
      .map(chatMessageText)
      .join('|')

    return {
      checking: text.split('Checking the log.').length - 1,
      passed: text.split('The build passed').length - 1,
      tools: now.flatMap(m => m.parts).filter(p => p.type === 'tool-call').length,
      pending: now.filter(m => m.pending).length
    }
  }

  const afterSwitch = summarize()
  send('message.delta', { text: ' with 0 warnings.' })
  await act(async () => {
    await vi.advanceTimersByTimeAsync(200)
  })
  vi.mocked(getLatestSessionMessages).mockResolvedValue({
    session_id: storedId,
    messages: [
      ...prior,
      { ...notice, display_kind: kind },
      {
        id: 4,
        role: 'assistant',
        content: 'Checking the log.',
        timestamp: 11,
        tool_calls: [{ id: 'call-1', type: 'function', function: { name: 'read_file', arguments: '{}' } }]
      },
      { id: 5, role: 'tool', content: 'ok', tool_call_id: 'call-1', timestamp: 12 },
      { id: 6, role: 'assistant', content: 'The build passed with 0 warnings.', timestamp: 13 }
    ]
  })

  if (restFirst) {
    await act(async () => view.result.current.actions.resumeSession(storedId, true))
  }

  send('message.complete', { text: 'The build passed with 0 warnings.' })
  await act(async () => {
    await vi.advanceTimersByTimeAsync(2000)
  })
  const settled = summarize()
  expect(afterSwitch).toEqual({ checking: 1, passed: 1, tools: 1, pending: 1 })
  expect(settled).toEqual({ checking: 1, passed: 1, tools: 1, pending: 0 })
})
