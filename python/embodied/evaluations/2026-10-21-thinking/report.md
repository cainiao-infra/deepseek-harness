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

## Live development check

A subsequent simulator batch uses Thinking high and cap 8,192 in every arm: native N, compatibility H0 and online H1. It runs nine assigned episodes on the same explored pickup layout, covering normal execution, four held initial commands and one RGB omission after action three. The [audited results](live-development.json) retain two unsuccessful episodes. All 11 provider requests complete with usage and the same fingerprint as the fixed-input batches; all three H1 chains pass the stop/observe/commit/action audit.

| Condition | N success / native seconds | H0 success / native seconds | H1 success / native seconds |
|---|---|---|---|
| Normal | Yes / 43.79 | Yes / 42.48 | Yes / 43.43 |
| Four held commands | Yes / 41.40 | Yes / 42.37 | Yes / 78.57 |
| One missing RGB observation | No / no terminal | No / no terminal | Yes / 77.00 |

Native seconds include skill cold start and end at the first existing native terminal check, excluding teardown. Missing terminal checks do not become completion times. H1 performs one replacement in each disturbed episode; it makes one initial request on the normal episode. The short stall already recovers with the native controller, so H1's extra replan adds latency without increasing success. This needs detector calibration before performance acceptance. RGB recovery demonstrates an executable benefit on this one development case, not a population success-rate improvement, an L-task result or a Thinking effect isolated from the prompt correction.

An earlier high-effort batch was interrupted after Vulkan initialization failures, before any provider request. Its failed and unfinished assignments remain in the raw archive. The completed batch separately records its EGL Vulkan configuration; it is not pooled with the interrupted batch. These are development results with zero formal held-out runs and an unqualified full native catalog.

## Regression after cadence and shutdown fixes

A separate [nine-episode regression](pickup-regression.json) reuses the same explored pickup scene and three conditions, with high Thinking and cap 8,192 in all arms. Native terminal seconds are N/H0/H1: normal 51.49/43.44/44.13, held commands 43.11/44.86/62.92, and missing RGB no terminal/no terminal/67.46. H1 succeeds in all three cases; N and H0 each succeed in two. All 11 actual provider calls pass raw-control/response/usage checks with the same fingerprint, and all three H1 chains pass the independent Session audit. [The verified archive](pickup-regression-evidence.json) includes every assigned failure and the exact implementation.

The stall replan remains unnecessary and slower than native recovery. Its completion time is lower than the earlier 78.57 seconds, but cross-batch cold-start variation prevents attributing the entire difference to cleanup. The separate worker probe isolates that mechanism. These repeated development cases add no independent held-out scenes and establish neither G2 nor general superiority.

## Zero-call initial binding and detector calibration

A [12-episode development matrix](zero-call-development.json) gives every arm the same explicit native pickup plan and exposes all 24 skills. H1 preserves the unused worker across the initial reset and uses a motion window of eight instead of four. This compares deployable configurations; it does not isolate either code change or detector threshold by itself. The same explored pickup scene supplies four conditions. All four H1 chains pass the configured-plan/model-receipt, stop, fresh-observation, binding and dispatch audit. The [990-file archive](zero-call-development-evidence.json) also retains a prior launch that failed on missing Python import paths before any episode or model call.

| Condition | N native seconds | H0 native seconds | H1 native seconds | H1 model calls |
|---|---:|---:|---:|---:|
| Normal | 38.22 | 37.02 | 42.46 | 0 |
| Four held commands | 38.77 | 38.56 | 42.72 | 0 |
| Eight held commands | 36.05 | 37.78 | 72.34 | 1 |
| One missing RGB observation | Failure / no terminal | Failure / no terminal | 71.04 | 1 |

N and H0 each succeed in three of four assigned episodes with zero model calls; H1 succeeds in four. The short-stall negative control no longer causes a replan, while the longer stall still causes unnecessary recovery because native execution succeeds unaided. Normal H1 completion remains 11.1% slower than N in this single cold-start sample, above the proposed 10% point-ratio target; no formal interval or acceptance claim follows from these data.

A separate [control-path probe](control-path-profile.json) measures 500 warm configuration checks at about 2.7 ms each. Fifty writes per location through the unchanged synchronous immutable-record function have median latency 14.74 ms on NAS and 0.55 ms on local disk. This identifies a storage cost worth testing at episode level. The probe excludes model inference and physical execution; its component speed ratio is not an end-to-end speedup.

## Output-storage comparison

A [second 12-episode matrix](local-disk-development.json) retains the zero-call configuration, source, scenes, arm order and recovery controls, and moves every arm’s evidence output from NAS to local disk. Synchronous immutable recording stays enabled. All four H1 chains and both model calls pass independent audits; the [989-file archive](local-disk-development-evidence.json) preserves failures and frozen inputs.

| Condition | N native seconds | H0 native seconds | H1 native seconds | H1 model calls |
|---|---:|---:|---:|---:|
| Normal | 35.65 | 34.89 | 36.74 | 0 |
| Four held commands | 34.68 | 36.05 | 37.62 | 0 |
| Eight held commands | 35.12 | 35.26 | 67.92 | 1 |
| One missing RGB observation | Failure / no terminal | Failure / no terminal | 68.57 | 1 |

H1 again succeeds in four of four episodes, N and H0 in three each. Normal H1 completion is 3.0% slower than N in this single cold-start comparison; four held commands give an 8.5% difference. These point estimates support local storage as a practical deployment choice, but uncontrolled cross-batch timing and one explored scene cannot establish G2. The eight-command stall still triggers unnecessary recovery. Formal held-out runs remain zero.

## Complex-task development diagnosis

Two additional batches run the same explored development scenes for language-conditioned stacking and toolbox storage, with high Thinking and cap 8,192. Each batch assigns N/H0/H1 once per scene; all 12 episodes are retained and none succeeds. Here H1 explicitly disables recovery (H1-R); all arms use 22 code capabilities and exclude the two learned policies. These runs diagnose execution and routing, not enhanced recovery or the strongest native baseline. See [the complete summaries](complex-development.json) and [archive verification](complex-development-evidence.json).

The first batch fails before action dispatch: its code worker environment lacks Torch, and the online initial-binding path additionally demands a restart guard. The corrected worker environment and initial admission remove those blockers in batch two. All three stacking arms then select the intended capability and execute 400 actions, but finish with native score 20 and no success. This places that observed failure after capability selection; its precise controller/scene cause remains unresolved. All three toolbox arms select `slot-insertion` instead of `oriented-container-placement`. Native and compatibility execution report unavailable RGB tube components; the online arm additionally exposes a NumPy action-evidence serialization failure. The capability descriptions do not adequately distinguish tube/rack geometry from heterogeneous tools and shaped recesses. The current code fixes numeric evidence serialization and preserves native initial observation cadence; those fixes alone do not establish successful toolbox execution.

## Worker shutdown diagnosis

A [same-input lifecycle probe](worker-close.json) isolates native code-worker cleanup, with no planner calls or physical dispatch. Before the shutdown patch, two closes take 15.115 and 15.115 seconds and end in SIGKILL. With the existing cooperative close message, they take 0.965 and 1.015 seconds and exit normally with code 0. Worker resident/high-water memory before close remains approximately 3.37 GiB in both conditions. These two observations support removal of a specific cleanup delay; they do not establish overall task latency, sustained-memory behavior or G2 acceptance. The patch applies equally to every comparison arm.

## Full-catalog simulator diagnosis

A [nine-episode development matrix](full-catalog-development.json) exposes all 24 skills, including both executable learned policies, on three official seed-0/layout-0 scenes excluded from held-out acceptance. All arms use high Thinking and cap 8,192; H1 remains the recovery-disabled H1-R ablation. Pickup succeeds in N/H0/H1. Stacking fails in all three after 400 actions with native score zero, despite every planner selecting `language-conditioned-stacking`. Toolbox storage selects `slot-insertion` in all three and fails before any action; the native code worker expects tube geometry. These results retain all assigned failures and [993 verified evidence files](full-catalog-development-evidence.json).

Providing learned policies does not make the planner select them: all nine decisions use the code library. The stacking failure also occurs on an official scene, so fresh-scene generation alone does not explain it; controller/scene compatibility still needs diagnosis. The toolbox result identifies a capability-description mismatch and an absent recovery path for skill faults. H1 additionally reports its conservative post-fault cleanup failure. First-reset disposal also causes a second full policy load in H1, increasing overhead. This matrix is neither a strongest-native selection nor evidence of enhanced complex-task success.

## Toolbox execution coverage

[Inspection of the frozen skill and native evaluator](toolbox-skill-coverage.json) identifies an execution gap independent of model selection. Native success requires all four tools—pliers, hammer, tape measure and wrench—in their matching positions with containment/orientation checks and the robot back at origin. The inspected visual reader creates tape-measure and optional pliers moves; the executable program takes only `moves[:1]`, and its numerical stages use tape-specific geometry. All four tools are outside the box in the retained development RGB frame.

Choosing `oriented-container-placement` correctly therefore cannot complete this whole task from that scene. Repeatedly selecting the same program does not add hammer, wrench or pliers execution. Improving its generic capability description can correct routing but cannot supply missing control behavior. Complex-task acceptance needs verified execution support for every required subgoal and a planner that can bind and observe those subgoals. This finding describes the inspected frozen release, not every PhysicalRSI implementation; evaluator truth stays outside model inputs.

## Learned-policy task preflight

A [nine-episode pi05 preflight](learned-policy-development.json) uses the same three explored official scenes, an explicit `memory-guided-manipulation` initial plan in every arm, and zero planner calls. H1 disables recovery. Native evaluation completes in all nine episodes, but none succeeds: pickup exhausts 200 actions, stacking 400 and toolbox storage 900, all with native score zero. The [1,805-file verified archive](learned-policy-development-evidence.json) retains videos, observations, actions, frozen implementation and failures. This tests pi05 only; it does not rank the sparse-memory policy.

These failures occur without DeepSeek inference and therefore cannot be attributed to its Thinking switch. The pickup video shows robot motion without completing the target grasp. Loading the checkpoint and producing numeric action chunks is insufficient evidence of task compatibility; checkpoint/data preprocessing, controller conventions and task coverage still need diagnosis. The same failure across N/H0/H1 does not establish a DSH-specific regression or qualify this configuration as the strongest native baseline.

## Real confirmed-fault recovery

A [six-assignment development batch](skill-fault-development.json) runs the new private worker protocol and opt-in confirmed skill-failure recovery on pickup and toolbox storage. All arms expose the full catalog, use real initial planning with high Thinking and cap 8,192, and retain the same source and output-storage configuration. All eight provider calls pass raw-control, response-byte and usage audits. The [435-file archive](skill-fault-development-evidence.json) retains every failure and rejected proposal.

Pickup succeeds in N/H0/H1 with 110 actions each and native completion times 40.94/42.60/44.21 seconds. H1’s successful chain passes independent audit; this single case does not pass G1 or G2. Toolbox storage fails in every arm: N selects `slot-insertion` and fails before dispatch; H0 selects `footprint-relative-placement`, executes 900 actions and finishes with score zero. H1 initially selects `slot-insertion`, records a cleanup-confirmed binding failure, acknowledges zero queued/in-flight actions, captures a fresh observation and makes a real recovery call. It restarts the same capability, encounters a second confirmed fault, stops and replans again. The final proposal has no reviewed restart preconditions and is refused. H1 dispatches zero actions and consumes three calls, including the rejected proposal.

This establishes that a real native fault reaches bounded online replanning without admitting an unreviewed replacement. It does not establish successful skill switching or higher task success. The generic capability descriptions still permit incorrect selection, and the four-tool execution gap remains. Successful replanning mechanics cannot substitute for successful task outcomes. Formal held-out runs remain zero; “basic parity and complex superiority” is unproven.

## Integration and acceptance

Online planning now labels current camera availability and refuses recovery observations whose sequence is not newer than the trigger. The development relay can freeze `off`, `low`, `high` or `max` identically for N/H0/H1. The runner separately records the first existing native terminal check, excluding teardown without adding environment queries. Keyless recorded-profile coverage checks the actual high-effort wire fields and retained Session history.

The initial full-catalog constructor fails on missing OpenPI and incompatible training dependencies. An isolated inference environment [now loads](checkpoint-inference.json) both `pi05` and `pi05-sparse-memory` on CUDA and produces two 50-action chunks per policy from the same recorded observation, with zero planner calls or physical dispatch. A subsequent [owned-process probe](process-binding.json) loads all 24 skills, binds each learned composition in a distinct process, transports 50-action NumPy chunks and confirms both process exits with code 0. Its first attempt fails on missing `msgpack_numpy`; the successful attempt follows installation of that native wire dependency. These probes establish executable isolated bindings, not strongest-native task success. Successful learned-policy task qualification remains a prerequisite, alongside multi-subgoal execution, observable grasp/target events, independent L/U/X scoring, ablations and preregistered held-out statistics. The [acceptance requirements](../../../../.agents/notes/proposed/architecture/2026-10-20-physicalrsi-phase-one-integration.md) remain unchanged.

## Evidence and reproduction

[The evidence index](evidence.json) links the two fixed-input summaries and their raw archive's SHA-256. The separate [live evidence index](live-development-evidence.json) records the simulator archive, including the interrupted batch, native scores, failures, timings, complete H1 Session chains and source commit. Both archives are locally verified against every file in their manifests and scanned for the actual credential with zero matches. Raw requests, responses, protocols, source inputs and implementation copies remain in ignored archives. The fixed-input report tool checks frozen messages, sent payloads, response digests, usage and recomputed decisions, rejecting modified raw responses rather than trusting launcher summaries.

```sh
PYTHONPATH=python/embodied/src python python/embodied/scripts/thinking_comparison_report.py /absolute/path/to/extracted-batch
```

These experiments answer a configuration and prompt-reliability question. They do not replace the required real-environment S/L/U/X outcome acceptance.
