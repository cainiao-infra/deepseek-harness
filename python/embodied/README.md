# Experimental embodied Harness adapter

English | [中文](README.zh.md)

The adapter connects PhysicalRSI’s shared domain planner to the public DSH Python SDK. Compatibility planning preserves native messages and validation; execution remains with PhysicalRSI. The online RoboDojo consumer checkpoints synchronous actions, discards cached tails, and binds model-selected recovery through native PhysicalRSI skills. No task-success or latency parity is qualified.

## Install and launch

Use Node supported by the repository and Python 3.11 or later for PhysicalRSI. Build DSH before selecting the built CLI and bundle. The commands below run from the DSH repository root; replace absolute paths with your isolated checkout and Harness home.

```sh
pnpm install --frozen-lockfile
pnpm run build:artifacts
python -m pip install -e python/sdk-runtime -e python/sdk -e python/embodied
python -m deepseek_harness_embodied.profile --dsh-home /absolute/path/to/home --bundle /absolute/path/to/deepseek-harness/packages/experimental/embodied-planner
```

The installer creates a new profile and refuses to replace an existing one. It links a built workspace bundle; it is intended for source checkouts rather than standalone published wheels. Node applications start only through `dsh --profile embodied-planner`; the adapter adds no alternative Node entrypoint.

## PhysicalRSI integration

The [integration patch](integrations/physicalrsi.patch) extracts `build_planner_messages` and `validate_planner_result`, adds the explicit `dsh` backend, and allocates the episode before planning. Apply it only after `git apply --check` succeeds against a copy of the audited local PhysicalRSI sources. Released copies and the remotely modified planner are separate inputs; a failed check requires a reviewed port, never a forced overwrite.

The [remote source variant](integrations/physicalrsi-remote.patch) preserves the audited remote planner’s `api`, `codex` and `remote-codex` backends while adding DSH through the same domain builder and validator. Verify both `before_sha256` values in [its source manifest](integrations/physicalrsi-remote.sources.json), apply only in an isolated PhysicalRSI copy, then verify both `after_sha256` values. A source mismatch requires a new reviewed port.

Copy [the deployment example](configs/deepseek-flash.example.json), set absolute paths, and configure the native planner’s `max_tokens` to the same cap for comparisons. Export `PHYSICALRSI_PLANNER_BACKEND=dsh`, `PHYSICALRSI_DSH_CONFIG=/absolute/path/to/config.json`, and `PHYSICALRSI_AGENT_API_KEY` through your credential environment. The SDK process inherits the credential; neither configuration nor receipts contain it. Native transport remains the `api` backend.

## Planning and evidence

`PlannerBridge` owns one in-flight inference and uses fresh Sessions for compatibility calls. It rotates an idle runtime at the configured call count or when the domain prompt changes. `begin_episode` returns a reset generation; `reset` invalidates late responses. A stale, truncated, invalid, timed-out or duplicate request never returns an executable plan. A timeout has an unknown inference outcome and closes the owned runtime; it does not stop a robot or promise provider billing cancellation.

Each request writes immutable `request.json`, `events.json` and a terminal `receipt.json` under run/episode/request IDs. Receipts bind input and model configuration digests to the SDK Session and validated plan. Successful events are flushed before returning the plan; close the bridge to flush the runtime’s complete JSONL Session logs. Physical observations, actions, binding revisions and native outcomes must still be linked by the runner before an episode qualifies as complete evidence.

`ModelSpec.deepseek_reasoning` explicitly selects `off`, `low`, `high` or `max` through DSH’s existing DeepSeek compatibility settings. An absent value preserves the provider default; it does not disable thinking. `off` sends `thinking.type=disabled` without `reasoning_effort`. Freeze the same setting in every comparison arm; the [official thinking-mode guide](https://api-docs.deepseek.com/guides/thinking_mode/) describes its wire semantics.

## Online recovery provider

`recovery.py` defines `PhysicalRuntime`, `RuntimeState`, `StopReceipt`, `PlanRevision` and `RecoveryCoordinator`. The consumer routes every action chunk through the coordinator. An observed trigger disables dispatch, waits for a bounded acknowledgement with zero queued/in-flight actions, obtains a newer observation, and validates a replacement against pinned identities and current preconditions. It records the commit before binding, and a replacement must change the provider-validated `execution_key` derived from the capability and executable parameters, excluding rationale. Exceptions leave dispatch disabled. Only the native outcome marks success.

The single-environment `robodojo.py` consumer owns action dispatch and its cached tail. At a recovery checkpoint it blocks dispatch, drops the tail, verifies the simulator control queue is empty, and asks `OnlineModel` to reap the synchronous skill worker. No simulator steps run during planning. This acknowledges quiescent simulator control at an action boundary; it does not acknowledge stopped motion on a physical robot. A pending native control queue or unknown RPC outcome prevents replacement.

`online_model.py` preserves PhysicalRSI’s native catalog, memory, domain prompt construction, validation, and binding implementation. Requests carry a phase-specific JSON Schema; operations are registered ID strings and cannot carry invented runtime parameters. Enhanced decisions add `execution`: initial `bind`, same-capability `restart`, different-capability `switch`, or `abort`. Restart creates a new skill worker and recomputes actions from fresh RGB/proprioception without resetting the scene. Configured observable restart preconditions must hold; unknown or closed-gripper states can refuse recovery. Execution digests exclude rationale and history. This version admits synchronous code-policy workers only.

Use [the online configuration](configs/robodojo-online.example.json) through `DSH_ROBODOJO_ONLINE_CONFIG` in both processes. `scripts/robodojo_policy.py` starts RoboDojo’s loopback WebSocket server with an explicit JSON deployment containing `port`, `ready_file` and `model`; `scripts/robodojo_client.py` supplies the online evaluation consumer to native RoboDojo main from an isolated output directory. Both scripts require the audited PhysicalRSI and RoboDojo Python imports. The observed-event detector checks missing RGB, changed instructions, and commanded joint motion absent across a configured window. The motion rule requires configured observation and command preconditions; the pickup example requires both grippers to remain open, so normal closed-gripper contact does not trigger it. Events retain measured joint motion and command error. It does not infer grasp failure or target pose from benchmark state.

Initial and recovery requests share one durable DSH Session. Episode history is bounded by revision/event counts, the provider context limit and a common deadline; runtime rotation during an active chain fails instead of losing history. Automatic context summarization and offline history-to-skill admission remain separate work. Immutable runner records link observed triggers, stop acknowledgement, fresh observation sequence, plan commit, native binding, dispatched actions and native terminal outcome; request receipts link the actual sensor input and Session.

`CallBudget` reserves one immutable slot before each model turn across processes and restarts. Failed and unknown attempts consume their slots. All runs sharing a budget must use the same immutable limit; tools and provider retries remain disabled. A six-slot ledger limits this development batch to at most six new provider requests. Recorded local model tests use a separate ledger. Exhaustion blocks subsequent planning and actions.

## Validation

Unit tests exercise request lifecycle and stop/observe/commit failures. The built-profile test uses a recorded local model response, verifies exact serialized domain text and PNG bytes, and checks persisted Session evidence. It requires a completed build and local socket permission.

```sh
PYTHONPATH=python/embodied/src DSH_EMBODIED_INTEGRATION=1 python -m pytest python/embodied/tests
```

`scripts/smoke.py` performs exactly one native and one DSH live inference over the same generated input, with no robot actions. It requires explicit deployment configuration and credentials. This is protocol evidence only; G1–G4 require the preregistered RoboDojo paired experiments and cannot be passed by this smoke.
