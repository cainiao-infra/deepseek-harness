/** Sampling changes are logged and the plugin's listener is removed on disposal. */
import { Context } from '@deepseek-ai/cordis'
import { mountAgentLoopTestDependencies, mountAgentLoopTestHarness } from '@deepseek-ai/dsh-agent-loop-testkit'
import { createUserMessage } from '@deepseek-ai/dsh-llm'
import { SessionId } from '@deepseek-ai/dsh-session'
import { expect, it } from 'vitest'
import { MockAdapter, textResponse } from '../../../core/agent-loop/tests/mock-adapter.ts'
import * as Planner from '../src/index.ts'

it('logs explicit sampling and removes its contribution after disposal', async () => {
  const ctx = new Context()
  try {
    await mountAgentLoopTestDependencies(ctx)
    const adapter = new MockAdapter([textResponse('first'), textResponse('second')])
    ctx.llm.registerAdapter(['mock'], adapter)
    const harness = await mountAgentLoopTestHarness(ctx)
    const fiber = ctx.plugin(Planner, { temperature: 0.2 })
    await fiber
    const first = await harness.create(SessionId('embodied-first'), { provider: 'mock', model: 'mock' })
    first.followup(createUserMessage({ content: [{ type: 'text', text: 'Plan' }], source: { kind: 'user' } }))
    await first.whenIdle()
    expect(adapter.requests[0]?.temperature).toBe(0.2)
    expect(first.session.requestHeader()?.config.temperature).toBe(0.2)
    await fiber.dispose()
    const second = await harness.create(SessionId('embodied-second'), { provider: 'mock', model: 'mock' })
    second.followup(createUserMessage({ content: [{ type: 'text', text: 'Plan' }], source: { kind: 'user' } }))
    await second.whenIdle()
    expect(adapter.requests[1]?.temperature).toBeUndefined()
  } finally {
    await ctx.fiber.dispose()
  }
})
