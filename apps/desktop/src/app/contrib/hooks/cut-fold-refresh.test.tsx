// A turn is drawn once after the post-turn page refresh, while the chat stays
// open. The newest page slides forward after every turn and can start inside an
// earlier tool turn; that page's partial fold must not paint the earlier reply a
// second time beside the window's whole fold (cleared only by a reload). Drives the REAL submit (usePromptActions),
// the REAL stream reducer (useMessageStream), the REAL steer entry point, the
// REAL post-turn background sync (useBackgroundSync → reconcileActiveTranscript
// → graftRefreshedTailOntoBackfill) and the REAL assistant-ui runtime, then
// asserts each stored row is on screen once.
import { useAuiState } from '@assistant-ui/react'
import type { GatewayEvent } from '@hermes/shared'
import { useStore } from '@nanostores/react'
import { QueryClient } from '@tanstack/react-query'
import { act, cleanup, render, screen } from '@testing-library/react'
import { useCallback, useRef } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ChatRuntimeBoundary } from '@/app/chat'
import { PRIMARY_SESSION_VIEW } from '@/app/chat/session-view'
import { useMessageStream } from '@/app/session/hooks/use-message-stream'
import { usePromptActions } from '@/app/session/hooks/use-prompt-actions'
import { useSessionStateCache } from '@/app/session/hooks/use-session-state-cache'
import { stubThreadEnvironment, stubThreadViewportSize } from '@/components/assistant-ui/test-utils'
import { Thread } from '@/components/assistant-ui/thread'
import { TranscriptWindowProvider } from '@/components/assistant-ui/thread/transcript-window'
import { getLatestSessionMessages, getOlderSessionMessages } from '@/hermes'
import { chatMessageText, toChatMessages } from '@/lib/chat-messages'
import { resetLiveSync } from '@/store/live-sync'
import {
  setActiveSessionId,
  setAwaitingResponse,
  setBusy,
  setMessages,
  setSelectedStoredSessionId
} from '@/store/session'
import { $sessionStates, $sessionTiles, clearAllSessionStates } from '@/store/session-states'
import { clearSessionTodos } from '@/store/todos'
import type { SessionMessage } from '@/types/hermes'

import { hydrateStoredSessionTranscript, reconcileActiveTranscript, useBackgroundSync } from './use-background-sync'

vi.mock('@/hermes', async original => ({
  ...(await original<Record<string, unknown>>()),
  getLatestSessionMessages: vi.fn(),
  getOlderSessionMessages: vi.fn()
}))

stubThreadEnvironment()
stubThreadViewportSize()

const RUNTIME = 'dup-runtime'
const STORED = 'local-22f871158f267c4bc596a5087d90b0094c5f7690458f46273a1634e0b477cb76'
const PROMPT = 'keep going'
const STEER = 'if this is on the b300s or NFS delete it urself'
const STEER_ROW = `[OUT-OF-BAND USER MESSAGE — a direct message from the user, delivered once at this position; not tool output and not a new delivery when replayed from conversation history]\n${STEER}\n[/OUT-OF-BAND USER MESSAGE]`
const COMMENTARY = 'Network blip cut the watch; re-checking.'
const FINAL = 'All 19 targets are clean; the stale run dirs are gone.'

const PAGE = 120 // LATEST_SESSION_MESSAGES_LIMIT

/** Earlier turns, as on disk (ids below the live turn's). `rounds` tool rounds per turn. */
function priorRows(pairs: number, rounds = 0): SessionMessage[] {
  let id = 500000

  return Array.from({ length: pairs }, (_, index): SessionMessage[] => [
    { id: id++, role: 'user', content: `earlier question ${index}`, timestamp: id },
    ...Array.from({ length: rounds }, (_, round): SessionMessage[] => [
      // Real tool-call rows carry the model's reasoning, so hydration starts the fold AT this row.
      {
        id: id++,
        role: 'assistant',
        content: '',
        reasoning: `checking ${index}-${round}`,
        timestamp: id,
        tool_calls: [call(`p${index}-${round}`, 'terminal', { command: 'ls' })]
      } as SessionMessage,
      {
        id: id++,
        role: 'tool',
        content: '{"output":"ok"}',
        timestamp: id,
        tool_call_id: `p${index}-${round}`,
        tool_name: 'terminal'
      }
    ]).flat(),
    { id: id++, role: 'assistant', content: `earlier answer ${index}`, timestamp: id }
  ]).flat()
}

const call = (id: string, name: string, args: Record<string, unknown>) => ({
  id,
  type: 'function',
  function: { name, arguments: JSON.stringify(args) }
})

interface TurnShape {
  steer: boolean
  /** A second turn's stored ids, tool ids and words (default: the first turn's). */
  base?: number
  tag?: string
  /** Extra tool rounds before the commentary round (each = assistant tool_calls row + tool row). */
  tools: number
}

/** The tool calls the Desktop streams, in order, with the stored row ids they land on. */
function turnTools({ steer, tools, tag = '' }: TurnShape) {
  const before = Array.from({ length: tools }, (_, index) => `${tag}t${index + 1}`)
  const after = steer ? [`${tag}ts`] : []

  return { before, after }
}

function turnWords({ tag = '' }: TurnShape) {
  return {
    prompt: `${tag}${PROMPT}`,
    steer: `${tag}${STEER}`,
    commentary: `${tag}${COMMENTARY}`,
    final: `${tag}${FINAL}`
  }
}

/** The turn as the stored page serves it (hermes_cli/web_routers/sessions.py projections). */
function storedTurn(shape: TurnShape, display: boolean): SessionMessage[] {
  let id = shape.base ?? 588098
  const next = () => id++
  const words = turnWords(shape)
  const rows: SessionMessage[] = [{ id: next(), role: 'user', content: words.prompt, timestamp: 1000 }]
  const { before, after } = turnTools(shape)

  const round = (tool: string) => {
    rows.push(
      {
        id: next(),
        role: 'assistant',
        content: '',
        timestamp: 1000 + id,
        tool_calls: [call(tool, 'terminal', { command: tool })]
      },
      {
        id: next(),
        role: 'tool',
        content: `{"output":"${tool} ok"}`,
        timestamp: 1000 + id,
        tool_call_id: tool,
        tool_name: 'terminal'
      }
    )
  }

  before.forEach(round)

  if (shape.steer) {
    rows.push({
      id: next(),
      role: 'user',
      content: STEER_ROW,
      display_content: words.steer,
      display_kind: 'steer',
      timestamp: 1000 + id
    } as SessionMessage)
    after.forEach(round)
  }

  rows.push(
    {
      id: next(),
      role: 'assistant',
      content: words.commentary,
      timestamp: 1000 + id,
      tool_calls: [call(`${shape.tag ?? ''}tc`, 'terminal', { command: 'gh pr checks 198' })]
    },
    {
      id: next(),
      role: 'tool',
      content: '{"output":"rc=0"}',
      timestamp: 1000 + id,
      tool_call_id: `${shape.tag ?? ''}tc`,
      tool_name: 'terminal'
    },
    { id: next(), role: 'assistant', content: words.final, timestamp: 1000 + id }
  )

  if (!display) {
    return rows
  }

  // seyfal/transcript-state-storage-main serves the display address too.
  return rows.map(row => ({
    ...row,
    display_order: row.id,
    display_identity: `${row.id}`.padStart(64, '0'),
    message_uid: `uid-${row.id}`
  })) as SessionMessage[]
}

const noop = async () => undefined
let cache: ReturnType<typeof useSessionStateCache>
let stream: ReturnType<typeof useMessageStream>
let actions: ReturnType<typeof usePromptActions>
let refresh: () => Promise<void>

const requestGatewayMock = vi.fn(async (method: string, params?: Record<string, unknown>): Promise<unknown> => {
  if (method === 'prompt.submit') {
    return { status: 'started', submission_id: params?.submission_id, session_id: params?.session_id }
  }

  if (method === 'session.steer') {
    return { status: 'queued' }
  }

  if (method === 'session.redirect') {
    return { status: 'redirected' }
  }

  return {}
})

const requestGateway = requestGatewayMock as unknown as <T>(
  method: string,
  params?: Record<string, unknown>,
  timeoutMs?: number
) => Promise<T>

function ObserveRuntime() {
  const messages = useAuiState(state => state.thread.messages)

  return (
    <output data-testid="runtime">
      {messages
        .map(
          message =>
            `${message.role}:${message.content
              .filter(part => part.type === 'text')
              .map(part => part.text)
              .join('')}`
        )
        .join('\n')}
    </output>
  )
}

function Harness() {
  const busyRef = useRef(false)
  const queryClient = useRef(new QueryClient()).current
  const requestSequenceRef = useRef(0)
  const signatureRef = useRef(new Map<string, string>())
  const runtimeIdByStoredSessionIdRef = useRef(new Map([[STORED, RUNTIME]]))

  cache = useSessionStateCache({
    activeSessionId: RUNTIME,
    selectedStoredSessionId: STORED,
    busyRef,
    setBusy,
    setAwaitingResponse,
    setMessages
  })
  const { activeSessionIdRef, selectedStoredSessionIdRef, updateSessionState } = cache

  const hydrate = useCallback(
    async (attempts = 1, storedSessionId: string | null = STORED, runtimeSessionId: string | null = RUNTIME) => {
      if (storedSessionId && runtimeSessionId) {
        await hydrateStoredSessionTranscript({
          attempts,
          storedSessionId,
          runtimeSessionId,
          storedProfile: 'default',
          updateSessionState
        })
      }
    },
    [updateSessionState]
  )

  stream = useMessageStream({
    ...cache,
    queryClient,
    hydrateFromStoredSession: hydrate,
    refreshHermesConfig: noop,
    refreshSessions: noop
  })

  actions = usePromptActions({
    activeSessionId: RUNTIME,
    activeSessionIdRef,
    branchCurrentSession: async () => true,
    busyRef,
    createBackendSessionForSend: async () => RUNTIME,
    getRoutedStoredSessionId: () => STORED,
    getRuntimeIdForStoredSession: () => RUNTIME,
    getRouteToken: () => 'token',
    handleSkinCommand: () => '',
    openMemoryGraph: () => undefined,
    refreshSessions: noop,
    requestGateway,
    resumeStoredSession: () => undefined,
    runtimeIdByStoredSessionIdRef,
    selectedStoredSessionIdRef,
    startFreshSessionDraft: () => undefined,
    sttEnabled: false,
    updateSessionState
  })

  refresh = useCallback(
    () =>
      reconcileActiveTranscript({
        activeSessionIdRef,
        selectedStoredSessionIdRef,
        updateSessionState,
        busyRef,
        requestSequenceRef,
        signatureRef,
        resolveSession: () => ({ profile: 'default' })
      }),
    [activeSessionIdRef, selectedStoredSessionIdRef, updateSessionState]
  )
  useBackgroundSync({
    activeConnectionId: 'local',
    activeGatewayProfile: 'default',
    activeIsMessaging: false,
    activeSessionId: RUNTIME,
    activeStoredSessionId: STORED,
    freshDraftReady: false,
    gatewayState: 'open',
    refreshActiveTranscript: refresh,
    refreshCronJobs: noop,
    refreshCurrentModel: noop,
    refreshHermesConfig: noop,
    refreshMessagingSessions: noop,
    refreshSessions: noop,
    requestGateway: async () => ({ sessions: [] }) as never,
    updateSessionState
  })
  const busy = useStore(PRIMARY_SESSION_VIEW.$busy)

  return (
    <ChatRuntimeBoundary
      busy={busy}
      onCancel={noop}
      onEdit={noop}
      onReload={noop}
      onThreadMessagesChange={noop}
      suppressMessages={false}
    >
      <ObserveRuntime />
      {process.env.DUP_THREAD ? (
        <TranscriptWindowProvider value={{ olderAvailable: false, expandWindow: () => {} }}>
          <Thread sessionId={RUNTIME} sessionKey={RUNTIME} />
        </TranscriptWindowProvider>
      ) : null}
    </ChatRuntimeBoundary>
  )
}

function send(type: GatewayEvent['type'], payload: GatewayEvent['payload'] = {}) {
  act(() => stream.handleGatewayEvent({ session_id: RUNTIME, type, payload }))
}

const settle = async (ms = 100) => {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms)
  })
}

const stateRows = () =>
  ($sessionStates.get()[RUNTIME]?.messages ?? []).map(
    message =>
      `${message.role}${message.rowId === undefined ? '' : `#${message.rowId}`}[${message.id}]:${chatMessageText(message).slice(0, 40)}`
  )

const screenRows = () => (screen.getByTestId('runtime').textContent ?? '').split('\n')

function tool(id: string, args: Record<string, unknown>, result: string) {
  send('tool.start', { name: 'terminal', tool_id: id, args })
  send('tool.complete', { name: 'terminal', tool_id: id, result })
}

async function runTurn(shape: TurnShape, complete: boolean | null, display: boolean, interim: boolean) {
  // The real composer path: optimistic `user-<submission>` bubble, prompt.submit.
  const words = turnWords(shape)
  await act(async () => {
    await actions.submitText(words.prompt)
  })
  send('message.start')
  const { before, after } = turnTools(shape)

  for (const id of before) {
    tool(id, { command: id }, `{"output":"${id} ok"}`)
  }

  if (shape.steer) {
    await act(async () => {
      await expect(actions.redirectPrompt(words.steer, 'steer')).resolves.toBe(true)
    })

    for (const id of after) {
      tool(id, { command: id }, `{"output":"${id} ok"}`)
    }
  }

  send('message.delta', { text: words.commentary })
  await settle()

  if (interim) {
    // display.interim_assistant_messages defaults on: tool-call commentary seals as an interim.
    send('message.interim', { text: words.commentary, already_streamed: true })
  }

  tool(`${shape.tag ?? ''}tc`, { command: 'gh pr checks 198' }, '{"output":"rc=0"}')
  send('message.delta', { text: words.final })
  await settle()

  const rows = storedTurn(shape, display)
  const users = rows.filter(row => row.role === 'user').map(row => row.id as number)
  send('message.complete', {
    text: words.final,
    status: 'complete',
    persisted_turn:
      complete === null
        ? undefined
        : {
            row_ids: rows.map(row => row.id),
            complete,
            user_row_id: users[0],
            user_row_ids: users,
            final_assistant_row_id: rows.at(-1)!.id
          }
  })
}

beforeEach(() => {
  vi.useFakeTimers()
  vi.mocked(getLatestSessionMessages).mockReset()
  vi.mocked(getOlderSessionMessages).mockReset()
  requestGatewayMock.mockClear()
  setActiveSessionId(RUNTIME)
  setSelectedStoredSessionId(STORED)
})

afterEach(() => {
  cleanup()
  clearSessionTodos(RUNTIME)
  clearAllSessionStates()
  $sessionTiles.set([])
  resetLiveSync()
  setActiveSessionId(null)
  setSelectedStoredSessionId(null)
  setBusy(false)
  setAwaitingResponse(false)
  setMessages([])
  localStorage.clear()
  vi.useRealTimers()
  vi.restoreAllMocks()
})

interface Variant {
  name: string
  steer: boolean
  complete: boolean | null
  display: boolean
  prior: number
  tools: number
  /** Tool rounds in each earlier turn. */
  rounds?: number
}

const variants: Variant[] = [
  { name: 'a: one prompt, tools, complete=true', steer: false, complete: true, display: false, prior: 1, tools: 1 },
  {
    name: 'a+c: one prompt, complete=true, display rows',
    steer: false,
    complete: true,
    display: true,
    prior: 1,
    tools: 1
  },
  { name: 'b: one prompt, complete=false', steer: false, complete: false, display: false, prior: 1, tools: 1 },
  {
    name: 'b: steer (two user rows), complete=false',
    steer: true,
    complete: false,
    display: false,
    prior: 1,
    tools: 1
  },
  { name: 'b+c: steer, complete=false, display rows', steer: true, complete: false, display: true, prior: 1, tools: 1 },
  // No receipt at all (host without persisted_turn, or receipt None).
  { name: 'n: one prompt, no receipt', steer: false, complete: null, display: false, prior: 1, tools: 1 },
  { name: 'n/long chat: no receipt', steer: false, complete: null, display: false, prior: 80, tools: 3 },
  { name: 'n/long chat: steer, no receipt', steer: true, complete: null, display: false, prior: 80, tools: 3 },
  // A real chat: the window holds a full newest page, so the post-turn page starts later.
  { name: 'a/long chat: complete=true', steer: false, complete: true, display: false, prior: 80, tools: 3 },
  { name: 'b/long chat: complete=false', steer: false, complete: false, display: false, prior: 80, tools: 3 },
  { name: 'b/long chat: steer, complete=false', steer: true, complete: false, display: false, prior: 80, tools: 3 },
  {
    name: 'b+c/long chat: steer, complete=false, display rows',
    steer: true,
    complete: false,
    display: true,
    prior: 80,
    tools: 3
  },
  // Tool-heavy earlier turns: the slid newest page starts INSIDE an earlier turn's fold.
  ...[1, 2, 3, 4, 5].flatMap(tools => [
    {
      name: `d/tool chat (shift ${2 * tools + 4}): complete=true`,
      steer: false,
      complete: true,
      display: false,
      prior: 30,
      tools,
      rounds: 5
    },
    {
      name: `d/tool chat (shift ${2 * tools + 5}): steer, complete=false`,
      steer: true,
      complete: false,
      display: false,
      prior: 30,
      tools,
      rounds: 5
    }
  ]),
  // A turn longer than one page (the live case: ~100+ rows): the page starts mid-turn.
  { name: 'a/long turn: complete=true', steer: false, complete: true, display: false, prior: 80, tools: 70 },
  { name: 'b/long turn: complete=false', steer: false, complete: false, display: false, prior: 80, tools: 70 },
  { name: 'b/long turn: steer, complete=false', steer: true, complete: false, display: false, prior: 80, tools: 70 }
]

describe('a finished turn is drawn once after the post-turn page refresh', () => {
  it.each(
    variants.flatMap(v => [
      { ...v, interim: false },
      { ...v, interim: true, name: `${v.name} +interim` }
    ])
  )('$name', async ({ steer, complete, display, prior, tools, interim, rounds = 0 }) => {
    const shape = { steer, tools }
    const before = priorRows(prior, rounds).slice(-PAGE)
    const all = [...priorRows(prior, rounds), ...storedTurn(shape, display)]

    // order: 'latest' paging, offset counted back from the newest row.
    const pageAt = (offset: number) => {
      const messages = all.slice(Math.max(0, all.length - offset - PAGE), all.length - offset)

      return {
        session_id: STORED,
        messages,
        pagination: { limit: PAGE, offset, order: 'latest', returned: messages.length, total: all.length }
      } as never
    }

    vi.mocked(getLatestSessionMessages).mockResolvedValue(pageAt(0))
    vi.mocked(getOlderSessionMessages).mockImplementation(async (_id, _scope, offset) => pageAt(offset))

    render(<Harness />)
    act(() => cache.updateSessionState(RUNTIME, state => ({ ...state, messages: toChatMessages(before) }), STORED))
    await settle()

    await runTurn(shape, complete, display, interim)
    const live = stateRows().slice(-8)
    // Post-turn sync (busy→idle), then a later sessions.changed tick.
    await settle(1000)
    await act(async () => {
      await refresh()
    })
    await settle(1000)

    const rendered = screenRows()

    // The DOM the real <Thread> list paints (DUP_THREAD=1).
    const dom = process.env.DUP_THREAD
      ? [...document.querySelectorAll<HTMLElement>('[data-slot="aui_message-group"]')].map(group =>
          (group.textContent ?? '').replace(/\s+/g, ' ').slice(0, 90)
        )
      : []

    const domText = [...document.querySelectorAll<HTMLElement>('[data-slot="aui_message-group"]')]
      .map(group => group.textContent ?? '')
      .join('\n')

    const domCount = (needle: string) => domText.split(needle).length - 1
    const report = `\nlive (tail):\n  ${live.join('\n  ')}\nafter refresh (state head):\n  ${stateRows().slice(0, 5).join('\n  ')}\nafter refresh (state tail):\n  ${stateRows().slice(-10).join('\n  ')}\nscreen (head):\n  ${rendered.slice(0, 5).join('\n  ')}\nscreen (tail):\n  ${rendered.slice(-10).join('\n  ')}\ndom groups (tail):\n  ${dom.slice(-4).join('\n  ')}\n`

    const count = (needle: string, role: string) =>
      rendered.filter(row => row.startsWith(`${role}:`) && row.includes(needle)).length

    if (process.env.DUP_DEBUG) {
      ;(await import('node:fs')).appendFileSync(
        process.env.DUP_DEBUG,
        `### ${JSON.stringify({ steer, complete, display, prior, tools, interim })}${report}`
      )
    }

    expect.soft(count(PROMPT, 'user'), `prompt ${report}`).toBe(1)
    expect.soft(count(FINAL, 'assistant'), `final ${report}`).toBe(1)

    if (steer) {
      expect.soft(count(STEER, 'user'), `steer ${report}`).toBe(1)
    }

    // Every earlier turn still on screen is drawn once too.
    const twice = [...new Set(rendered)].filter(
      row => /earlier (question|answer)/.test(row) && rendered.filter(r => r === row).length > 1
    )

    expect.soft(twice, `earlier turns ${report}`).toEqual([])

    if (process.env.DUP_THREAD) {
      expect.soft(dom.filter(group => group.includes(PROMPT)).length, `dom prompt ${report}`).toBe(1)
      expect.soft(domCount(FINAL), `dom final ${report}`).toBe(1)
    }

    expect(getLatestSessionMessages).toHaveBeenCalled()
  })
})

describe('several turns in a row in a tool-heavy chat', () => {
  it.each([false, true])('draws every turn once after each post-turn refresh (steer: %s)', async steer => {
    const prior = priorRows(30, 5)

    const turns: TurnShape[] = [0, 1, 2, 3].map(index => ({
      steer,
      tools: 1 + index,
      base: 600000 + index * 100,
      tag: `T${index} `
    }))

    let all = prior

    const pageAt = (offset: number) => {
      const messages = all.slice(Math.max(0, all.length - offset - PAGE), all.length - offset)

      return {
        session_id: STORED,
        messages,
        pagination: { limit: PAGE, offset, order: 'latest', returned: messages.length }
      } as never
    }

    vi.mocked(getLatestSessionMessages).mockImplementation(async () => pageAt(0))
    vi.mocked(getOlderSessionMessages).mockImplementation(async (_id, _scope, offset) => pageAt(offset))
    render(<Harness />)
    act(() =>
      cache.updateSessionState(RUNTIME, state => ({ ...state, messages: toChatMessages(prior.slice(-PAGE)) }), STORED)
    )
    await settle()

    const reports: string[] = []

    for (const shape of turns) {
      all = [...all, ...storedTurn(shape, false)]
      await runTurn(shape, !steer, false, true)
      await settle(1000)
      await act(async () => {
        await refresh()
      })
      await settle(1000)
      const rendered = screenRows()

      // Text-less tool bubbles of different turns render alike; only drawn words count.
      const twice = [...new Set(rendered)].filter(
        row => /^\w+:\S/.test(row) && rendered.filter(r => r === row).length > 1
      )

      reports.push(`${shape.tag}: twice=${JSON.stringify(twice)}`)
    }

    expect.soft(reports.join('\n')).not.toMatch(/twice=\["/)
    const rendered = screenRows()

    for (const shape of turns) {
      const words = turnWords(shape)
      expect
        .soft(
          rendered.filter(row => row === `user:${words.prompt}`).length,
          `${words.prompt}\n${reports.join('\n')}\n${rendered.slice(0, 8).join('\n')}`
        )
        .toBe(1)
    }
  })
})

// The same flow over a real session's rows (REST /messages projection of the
// live state.db, built by a scratch script). Not committed: it holds user data.
const FIXTURE = process.env.DUP_FIXTURE

describe.runIf(Boolean(FIXTURE))('real session rows', () => {
  it.each([
    { complete: false, interim: true },
    { complete: false, interim: false },
    { complete: true, interim: true }
  ])('draws the real steered turn once (complete=$complete, interim=$interim)', async ({ complete, interim }) => {
    const fs = await import('node:fs')
    const { before, after, turn } = JSON.parse(fs.readFileSync(FIXTURE!, 'utf8')) as Record<string, SessionMessage[]>

    const page = {
      session_id: STORED,
      messages: after,
      pagination: { limit: PAGE, offset: 0, order: 'latest', returned: after.length }
    }

    vi.mocked(getLatestSessionMessages).mockResolvedValue(page as never)
    vi.mocked(getOlderSessionMessages).mockImplementation(
      async () =>
        ({
          session_id: STORED,
          messages: [],
          pagination: { limit: PAGE, offset: PAGE, order: 'latest', returned: 0 }
        }) as never
    )

    render(<Harness />)
    act(() => cache.updateSessionState(RUNTIME, state => ({ ...state, messages: toChatMessages(before) }), STORED))
    await settle()

    const prompt = String(turn[0].content)
    await act(async () => {
      await actions.submitText(prompt)
    })
    send('message.start')

    for (const row of turn.slice(1)) {
      if (row.role === 'user') {
        await act(async () => {
          await expect(actions.redirectPrompt(String(row.display_content ?? row.content), 'steer')).resolves.toBe(true)
        })
      } else if (row.role === 'assistant') {
        const text = typeof row.content === 'string' ? row.content : ''

        if (text.trim()) {
          send('message.delta', { text })
          await settle(50)
        }

        if (Array.isArray(row.tool_calls) && row.tool_calls.length) {
          if (interim && text.trim()) {
            send('message.interim', { text, already_streamed: true })
          }

          for (const toolCall of row.tool_calls as Array<{
            id: string
            function: { name: string; arguments: string }
          }>) {
            send('tool.start', {
              name: toolCall.function.name,
              tool_id: toolCall.id,
              args: JSON.parse(toolCall.function.arguments || '{}')
            })
          }
        }
      } else if (row.role === 'tool') {
        send('tool.complete', {
          name: row.tool_name,
          tool_id: row.tool_call_id,
          result: String(row.content).slice(0, 100)
        })
      }
    }

    const ids = turn.map(row => row.id as number)
    const users = turn.filter(row => row.role === 'user').map(row => row.id as number)
    send('message.complete', {
      text: String(turn.at(-1)!.content),
      status: 'complete',
      persisted_turn: {
        row_ids: ids,
        complete,
        user_row_id: users[0],
        user_row_ids: users,
        final_assistant_row_id: ids.at(-1)
      }
    })
    const live = stateRows().slice(-8)
    await settle(1000)
    await act(async () => {
      await refresh()
    })
    await settle(1000)

    const rendered = screenRows()
    const report = `\nlive (tail):\n  ${live.join('\n  ')}\nafter (state tail):\n  ${stateRows().slice(-10).join('\n  ')}\n`

    if (process.env.DUP_DEBUG) {
      fs.appendFileSync(process.env.DUP_DEBUG, `### real ${JSON.stringify({ complete, interim })}${report}`)
    }

    const final = String(turn.at(-1)!.content).slice(0, 40)
    expect
      .soft(rendered.filter(row => row.startsWith('user:') && row.includes(prompt)).length, `prompt ${report}`)
      .toBe(1)
    expect
      .soft(rendered.filter(row => row.startsWith('assistant:') && row.includes(final)).length, `final ${report}`)
      .toBe(1)

    // Whole messages, not lines: a steer's command can repeat inside the reply.
    const drawn = ($sessionStates.get()[RUNTIME]?.messages ?? [])
      .map(message => `${message.role}:${chatMessageText(message)}`)
      .filter(row => /^\w+:\S/.test(row))

    const twice = [...new Set(drawn)].filter(row => drawn.filter(r => r === row).length > 1)
    expect
      .soft(
        twice.map(row => row.slice(0, 60)),
        `earlier turns ${report}`
      )
      .toEqual([])
  })
})
