---
description: "Tool-free experimental SDK composition for PhysicalRSI multimodal planning."
kind: "package-bundle"
---

# @deepseek-ai/dsh-experimental-embodied-planner

English | [中文](README.zh.md)

## Summary

A standalone SDK profile for domain planning with durable multimodal Sessions. PhysicalRSI supplies the system prompt, ordered observations, capability catalog and result validator; DSH supplies the model request lifecycle and persistent evidence. This experimental package has no benchmark qualification.

## Table of Contents

- [Use this package](#use-this-package)
- [Understand the implementation](#understand-the-implementation)
- [Further Exploration](#further-exploration)
- [Model Experience](#model-experience)
- [Known Limitations and Deferred Work](#known-limitations-and-deferred-work)
- [Dev Note](#dev-note)

<a id="use-this-package"></a>
## Use this package

Build the repository, install the [Python adapter](../../../python/embodied/README.md), and create a new isolated profile with its source-profile installer. The public launcher remains `dsh --profile embodied-planner`. Required model, prompt and image limits come from the Python deployment configuration; a missing value fails initialization. Credentials use `PHYSICALRSI_AGENT_API_KEY`.

<a id="understand-the-implementation"></a>
## Understand the implementation

The bundle owns its complete Cordis tree. It includes SDK stdio, pi-ai Chat Completions with JSON object mode, attachments, the agent kernel and uncompressed JSONL persistence. The request waterfall applies the configured temperature before the loop logs the request header. There are no registered model tools, automatic compaction, image offload, goals or subagents. Working-directory validation remains active while its prompt context is disabled.

<a id="further-exploration"></a>
## Further Exploration

- [Python adapter](../../../python/embodied/README.md) — deployment, receipts and recovery provider requirements.
- [pi-ai provider](../../llm/llm-pi-ai/README.md) — JSON output and source-image presentation.

<a id="model-experience"></a>
## Model Experience

### PhysicalRSI compatibility planning

#### What the model sees

The native domain system text and ordered `text`/`image_url` blocks, with no Harness persona, directory text or tool schemas. Inline images use normalized stored source bytes without attachment handles; oversized input fails instead of silently offloading images.

#### Token effect

One model request per planning attempt. Compatibility calls use fresh Sessions and no summarization calls. Multimodal input and JSON output consume the selected model’s normal tokens.

#### KV Cache effect

Stable prompt and catalog prefixes may be reused by the provider. Fresh observations change the request suffix. Runtime rotation does not promise provider cache reuse.

## Known Limitations and Deferred Work

<a id="known-limitations-and-deferred-work"></a>

- JSON output currently requires OpenAI Chat Completions. This profile declares a non-reasoning model; reasoning-enabled parity requires an explicit matching native configuration and wire comparison.
- Stored-image normalization may reject or alter inputs outside the configured limits. Qualification requires comparing actual serialized images with native requests.
- Online recovery requires a physical provider with acknowledged stop, fresh observation, precondition validation and binding. The Python coordinator alone does not establish RoboDojo recovery or performance gains.

<a id="dev-note"></a>
### Dev Note

Keep this bundle opt-in. Real-provider smoke tests and paired native evaluation are separate evidence from deterministic protocol and state-machine tests.
