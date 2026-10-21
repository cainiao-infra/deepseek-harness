# DeepSeek Thinking diagnostic: frozen RoboDojo inputs

English | [中文](report.zh.md)

## Result

Thinking improves plan admission with the native prompt on these frozen inputs, but does not establish a physical-success improvement. The enhanced online prompt already admits every non-thinking response in this sample. Thinking exposes a historical/current RGB ambiguity; an independently assigned clarification comparison removes the observed failures. Formal basic parity and complex-task superiority remain unqualified.

## Frozen comparison

The first batch contains all 13 provider requests from the earlier development evidence: eight native-format requests and five online initial/recovery requests. Each exact message list, image data URL and historical turn is copied before inference. Five repeats, two token caps and two Thinking settings produce 260 assigned calls. Paired arm order and pair order are randomized with seed 20261021; two workers execute pairs sequentially within each pair. Both arms use the official endpoint, `deepseek-flash`, JSON-object output and nonstreaming transport, with no retries. Thinking is explicitly disabled or enabled with effort `high`. Both retain temperature 0; the provider ignores temperature in thinking mode, so this is a comparison of deployable configurations rather than identical effective sampling controls. See the [official guide](https://api-docs.deepseek.com/guides/thinking_mode/).

Every call returns HTTP 200 with complete usage; all 260 responses report the same model and system fingerprint. Two responses end with `length`; all others end with `stop`. Repetitions of these snapshots are not independent physical scenarios: the original episodes share one known pickup layout. The oracle checks registered capability selection, field types and phase-specific bind/restart decisions. It does not infer physical success from a plan or its rationale.

| Output cap | Thinking | Matched plans / assigned | Median API seconds | p95 API seconds | Truncated |
|---:|---|---:|---:|---:|---:|
| 2,048 | Off | 46 / 65 | 2.03 | 2.67 | 0 |
| 2,048 | High | 61 / 65 | 4.15 | 10.59 | 2 |
| 8,192 | Off | 50 / 65 | 2.03 | 3.04 | 0 |
| 8,192 | High | 63 / 65 | 4.11 | 9.51 | 0 |

At cap 8,192, native-format prompts match in 25/40 off versus 39/40 high; online prompts match in 25/25 off versus 24/25 high. The aggregate gain therefore cannot be attributed to better online recovery. Total completion tokens are 11,321 off versus 39,490 high, including 31,852 reasoning tokens in the high arm. These are API latency and token measurements, not robot completion time or an invoice.

## Observed failure mechanisms

Non-thinking native-format responses repeatedly invent operation objects and parameters instead of registered ID strings. Thinking reduces this failure but does not guarantee admission: one high-effort response adds an unexpected top-level `type` field. JSON-object output guarantees JSON syntax, not the native field set.

On an online recovery input, high-effort responses sometimes treat the earlier `observation_lost` event's `rgb_available=false` as the current camera state, despite fresh images in the last user message. They abort with an empty operation list. The current scene is unchanged between arms; this is a decision error about observation timing, not evidence that fresh RGB is actually absent. At cap 2,048, two high-effort calls consume their entire output allowance in reasoning and return no final plan.

## Independent clarification comparison

The second batch freezes the five online snapshots and compares original versus clarified prompts, crossed with Thinking off/high. Each condition has five repeats per snapshot, cap 8,192 and no retries: 100 assigned calls, randomized with seed 20261022. Clarification adds a description of historical versus current observation timing and a current-camera status derived only from the supplied observation. Historical turns, domain context and image bytes stay unchanged. No new physical scenario or held-out evaluation case is introduced.

| Prompt | Thinking | Matched plans / assigned | Median API seconds | p95 API seconds |
|---|---|---:|---:|---:|
| Original | Off | 25 / 25 | 1.86 | 2.58 |
| Original | High | 22 / 25 | 3.05 | 8.70 |
| Clarified | Off | 25 / 25 | 1.93 | 2.41 |
| Clarified | High | 25 / 25 | 2.98 | 4.04 |

All calls return complete responses and usage with the same fingerprint as batch one. The three original/high failures misread historical RGB loss; the clarified/high condition has none in these 25 requests. This supports the combined timing/status clarification on these snapshots. It does not separate wording from explicit status, prove general reliability, or establish a simulator success-rate gain.

## Integration and acceptance

Online planning now labels current camera availability and refuses recovery observations whose sequence is not newer than the trigger. The development relay can freeze `off`, `low`, `high` or `max` identically for N/H0/H1. The runner separately records the first existing native terminal check, excluding teardown without adding environment queries. Keyless recorded-profile coverage checks the actual high-effort wire fields and retained Session history.

A separate zero-inference native full-catalog constructor probe fails because `openpi` is absent from the installed policy environment. A full installation of the downloaded OpenPI manifest also finds an incompatible training dependency: LeRobot requires a rerun-sdk version needing NumPy 2 while the manifest constrains NumPy below 2. Checkpoint directories therefore do not yet establish an executable strongest-native baseline. The isolated inference environment and real simulator verification remain prerequisites, alongside multi-subgoal execution, observable grasp/target events, independent L/U/X scoring, ablations and preregistered held-out statistics. The [acceptance requirements](../../../../.agents/notes/proposed/architecture/2026-10-20-physicalrsi-phase-one-integration.md) remain unchanged.

## Evidence and reproduction

[The evidence index](evidence.json) links the two audited batch summaries and the raw archive's SHA-256. Raw requests, responses, protocols, source inputs and implementation copies remain in an ignored evidence archive; credentials are excluded. The report tool verifies frozen messages, sent payloads, response digests, usage and recomputed decisions. It rejects modified raw responses rather than trusting launcher summaries.

```sh
PYTHONPATH=python/embodied/src python python/embodied/scripts/thinking_comparison_report.py /absolute/path/to/extracted-batch
```

These experiments answer a configuration and prompt-reliability question. They do not replace the required real-environment S/L/U/X outcome acceptance.
