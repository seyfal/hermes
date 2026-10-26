// @vitest-environment node
import { JsonRpcGatewayClient, JsonRpcGatewayError } from '@hermes/shared'
import { afterEach, expect, test, vi } from 'vitest'

import { HermesGateway } from './client'

interface MountOptions {
  source?: string
  reason?: string
  staysReadOnly?: boolean
  resumeFails?: boolean
}

async function mountedClient({ source, reason = 'session_read_only', staysReadOnly = false, resumeFails = false }: MountOptions) {
  vi.spyOn(JsonRpcGatewayClient.prototype, 'connect').mockResolvedValue()
  let writable = false
  const admitted: Record<string, unknown>[] = []
  const refusal = new JsonRpcGatewayError('Stored conversation', { code: -32000, data: { reason } })

  const wire = vi.spyOn(JsonRpcGatewayClient.prototype, 'request').mockImplementation(async (method, params) => {
    if (method === 'session.resume') {
      if (params?.title) {
        if (resumeFails) { throw refusal }
        writable = !staysReadOnly
      }

      return { session_id: 'imported', stored_session_id: 'imported', revision: 1, execution_generation: 1,
        info: writable ? { source: 'cli', profile_name: 'work' } : { desktop_protocol: 'hermes-gateway-v1' },
        prompts: [], messages: [] } as never
    }

    if (method === 'session.info') { return { source } as never }

    if (method === 'prompt.submit') {
      if (!writable) { throw refusal }
      admitted.push(params!)

      return { admission_id: params!.submission_id, status: 'queued', ref: { session_id: 'imported' } } as never
    }

    throw new Error(`Unexpected method ${method}`)
  })

  const client = new HermesGateway()
  await client.connect('ws://localhost/api/ws?native_dial=fixture&ticket=one-use')
  await client.request('session.resume', { session_id: 'imported', profile: 'work' })

  return { client, wire, admitted, refusal }
}

afterEach(() => vi.restoreAllMocks())

test.each(['claude-code', 'codex-cli'])('first send continues a %s mount once without losing or duplicating the input', async source => {
  const { client, wire, admitted } = await mountedClient({ source })

  const input = { session_id: 'imported', profile: 'work', text: 'Continue this fix.\nKeep the tests.',
    submission_id: 'first-send', attachments: [{ path: '/workspace/attachment.png', mime: 'image/png' }] }

  try {
    // Viewing alone never adopts the transcript.
    expect(wire.mock.calls.map(([method]) => method)).toEqual(['session.resume'])
    await expect(client.request('prompt.submit', input)).resolves.toMatchObject({ status: 'queued', submission_id: 'first-send' })
    const next = { ...input, text: 'And the next step?', submission_id: 'second-send' }
    await client.request('prompt.submit', next)

    const submits = wire.mock.calls.filter(([method]) => method === 'prompt.submit').map(([, params]) => params)
    expect(submits).toHaveLength(3)
    expect(submits[1]).toEqual(submits[0])
    expect(submits[0]).toMatchObject(input)
    expect(admitted).toEqual([submits[1], submits[2]])
    expect(admitted.map(params => params.submission_id)).toEqual(['first-send', 'second-send'])
    const resumes = wire.mock.calls.filter(([method, params]) => method === 'session.resume' && params?.title)
    expect(resumes).toHaveLength(1)
    expect(resumes[0][1]).toMatchObject({ session_id: 'imported', title: 'imported', profile: 'work' })
    expect(wire.mock.calls.every(([, params]) => params?.profile === 'work')).toBe(true)
  } finally {
    client.close()
  }
})

test.each([
  { source: 'cron' },
  { source: 'subagent' },
  { source: 'api' },
  { source: undefined },
  { source: 'claude-code', reason: 'permission_denied' }
])('does not adopt a mount on $source / $reason', async options => {
  const { client, wire, admitted, refusal } = await mountedClient(options)

  try {
    await expect(client.request('prompt.submit', { session_id: 'imported', text: 'more', submission_id: 'input' })).rejects.toBe(refusal)
    expect(wire.mock.calls.filter(([method]) => method === 'prompt.submit')).toHaveLength(1)
    expect(wire.mock.calls.filter(([method, params]) => method === 'session.resume' && params?.title)).toHaveLength(0)
    expect(admitted).toEqual([])
  } finally {
    client.close()
  }
})

test.each([false, true])('a failed continuation is bounded and never admits the refused input (resume fails: %s)', async resumeFails => {
  const { client, wire, admitted, refusal } = await mountedClient({ source: 'claude-code', staysReadOnly: true, resumeFails })
  const input = { session_id: 'imported', profile: 'work', text: 'keep this text', submission_id: 'input' }

  try {
    await expect(client.request('prompt.submit', input)).rejects.toBe(refusal)
    const submits = wire.mock.calls.filter(([method]) => method === 'prompt.submit').map(([, params]) => params)
    expect(submits).toHaveLength(resumeFails ? 1 : 2)
    expect(submits.every(params => params?.text === input.text && params?.submission_id === input.submission_id)).toBe(true)
    // A subsequent send cannot start another adoption loop for the same owner.
    await expect(client.request('prompt.submit', input)).rejects.toBe(refusal)
    expect(wire.mock.calls.filter(([method, params]) => method === 'session.resume' && params?.title)).toHaveLength(1)
    expect(admitted).toEqual([])
  } finally {
    client.close()
  }
})
