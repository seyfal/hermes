import { AssistantRuntimeProvider, type ThreadMessage, useExternalStoreRuntime } from '@assistant-ui/react'
import { act, render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ScrollToBottomButton } from '@/app/chat/scroll-to-bottom-button'
import { handleServerRequest, type ServerRequestContext } from '@/app/session/hooks/use-message-stream/gateway-event/server-requests'
import { createClientSessionState } from '@/lib/chat-runtime'
import { clearClarifyRequest, sessionClarifyRequest } from '@/store/clarify'
import { resetServerRequestsForTests } from '@/store/server-requests'
import { $threadScrolledUpBySession, setThreadAtBottom } from '@/store/thread-scroll'

import { stubThreadEnvironment, stubThreadViewportSize } from '../test-utils'

import { Thread } from '.'

stubThreadEnvironment()
stubThreadViewportSize()

const SESSION = 'clarify-reader'
let height = 5000
Object.defineProperty(HTMLElement.prototype, 'scrollHeight', { configurable: true, get: () => height })
Object.defineProperty(HTMLElement.prototype, 'clientHeight', { configurable: true, get: () => 600 })

const messages: ThreadMessage[] = [{
  id: 'question', role: 'user', content: [{ type: 'text', text: 'Read this transcript' }],
  attachments: [], createdAt: new Date('2026-08-01T00:00:00Z'), metadata: { custom: {} }
}]

function Harness({ session = SESSION }: { session?: string }) {
  const runtime = useExternalStoreRuntime({ messages, isRunning: false, onNew: async () => {} })

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <Thread clampToComposer={false} sessionId={session} sessionKey={session} />
      <ScrollToBottomButton sessionId={session} />
    </AssistantRuntimeProvider>
  )
}

async function settle() {
  await act(async () => {
    for (let tick = 0; tick < 10; tick++) {
      await new Promise<void>(resolve => window.setTimeout(resolve, 0))
    }
  })
}

function clarify(count: number, replayed = false, session = SESSION) {
  const deps: ServerRequestContext['deps'] = {
    activeSessionIdRef: { current: SESSION },
    sessionInterrupted: () => false,
    sessionStateByRuntimeIdRef: { current: new Map() },
    updateSessionState: (_id, update) => update(createClientSessionState('stored')),
    upsertToolCall: vi.fn()
  }

  return handleServerRequest({
    id: 'clarify-request', method: 'clarify', profile: 'default', replayed,
    params: { session_id: session, questions: Array.from({ length: count }, (_, index) => ({ qid: `q${index}`, question: 'Continue?', choices: ['yes', 'no'] })) },
    respond: vi.fn(), fail: vi.fn(), decline: vi.fn()
  }, deps, SESSION)
}

beforeEach(() => {
  height = 5000
  window.localStorage.clear()
  resetServerRequestsForTests()
})
afterEach(() => clearClarifyRequest())

const viewport = (container: HTMLElement) => container.querySelector<HTMLElement>('[data-slot="aui_thread-viewport"]')!

describe('clarify respects the actual viewport owner', () => {
  it.each([{ count: 1, replayed: false }, { count: 2, replayed: false }, { count: 2, replayed: true }])(
    'leaves the reader in place and offers the jump control: %o', async ({ count, replayed }) => {
      const view = render(<Harness />)
      const vp = viewport(view.container)
      await settle()
      act(() => {
        vp.dispatchEvent(new WheelEvent('wheel', { deltaY: -100 }))
        vp.scrollTop = 3500
        vp.dispatchEvent(new Event('scroll'))
      })
      await settle()
      // The five-second undim path clears this presentation flag, not ownership.
      act(() => setThreadAtBottom(false, SESSION, false))
      expect($threadScrolledUpBySession.get()[SESSION]).toBeUndefined()
      act(() => expect(clarify(count, replayed)).toBe(true))
      await settle()
      expect(sessionClarifyRequest(SESSION).get()?.questions).toHaveLength(count)
      expect(vp.scrollTop).toBe(3500)
      expect(view.getByRole('button', { name: /Scroll to bottom/ })).toBeTruthy()
    }
  )

  it('lets a following surface show clarify without moving another surface of the same session', async () => {
    const reader = render(<Harness />)
    const follower = render(<Harness />)
    await settle()
    act(() => {
      viewport(reader.container).dispatchEvent(new WheelEvent('wheel', { deltaY: -100 }))
      viewport(reader.container).scrollTop = 3500
      viewport(reader.container).dispatchEvent(new Event('scroll'))
    })
    await settle()
    height += 300
    act(() => expect(clarify(1)).toBe(true))
    await settle()
    expect(viewport(reader.container).scrollTop).toBe(3500)
    expect(viewport(follower.container).scrollTop).toBeGreaterThanOrEqual(height - 600 - 1)
  })

  it('does not jump a foreground viewport for another session request', async () => {
    const view = render(<Harness />)
    await settle()
    act(() => {
      viewport(view.container).scrollTop = 3500
      viewport(view.container).dispatchEvent(new Event('scroll'))
    })
    await settle()
    const top = viewport(view.container).scrollTop
    height += 300
    act(() => expect(clarify(1, false, 'background')).toBe(true))
    await settle()
    expect(viewport(view.container).scrollTop).toBe(top)
  })
})
