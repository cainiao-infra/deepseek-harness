/**
 * Sampling controls for a tool-free embodied planner; the agent loop logs the resolved request header.
 * @module @deepseek-ai/dsh-experimental-embodied-planner
 */
import type { Context } from '@deepseek-ai/cordis'
import z from '@deepseek-ai/schemastery'
import type {} from '@deepseek-ai/dsh-agent'

export const name = 'embodied-planner'

/** Explicit sampling controls shared by the native and DSH planner. */
export interface Config {
  /** Finite sampling temperature supported by the selected model. */
  temperature: number
}

/** Validate deployment-authored sampling controls. */
export const Config: z<Config> = z.object({
  temperature: z.number().min(0).max(2).required(),
})

/**
 * Set sampling controls through the request waterfall before durable logging.
 * @param ctx - application context owning the listener.
 * @param config - validated sampling controls.
 */
export function apply(ctx: Context, config: Config): void {
  ctx.on('agent/request', async (_payload, next) => ({
    ...await next(), temperature: config.temperature,
  }))
}
