# Experimental embodied Harness adapter

English | [中文](README.zh.md)

The adapter connects PhysicalRSI’s shared domain planner to the public DSH Python SDK. Compatibility planning preserves native messages and validation; execution remains with PhysicalRSI. Recovery coordination is available as an explicit provider interface, but the native RoboDojo runner does not yet supply a physical stop acknowledgement. No task-success or latency parity is qualified.

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

## Online recovery provider

`recovery.py` defines `PhysicalRuntime`, `RuntimeState`, `StopReceipt`, `PlanRevision` and `RecoveryCoordinator`. The consumer routes every action chunk through the coordinator. An observed trigger disables dispatch, waits for a bounded acknowledgement with zero queued/in-flight actions, obtains a newer observation, and validates a replacement against pinned identities and current preconditions. It records the commit before binding, and a replacement must change the provider-validated `execution_key` derived from the capability and executable parameters, excluding rationale. Exceptions leave dispatch disabled. Only the native outcome marks success.

Do not enable recovery by adapting `reset()` or `close()` into a stop receipt. The simulator/robot runner must own stop, observation sequencing and action delivery together. Multi-turn enhanced planning, observed-event detectors, runner integration, outcome-linked evaluation and offline history-to-skill admission remain separate work under the design note.

## Validation

Unit tests exercise request lifecycle and stop/observe/commit failures. The built-profile test uses a recorded local model response, verifies exact serialized domain text and PNG bytes, and checks persisted Session evidence. It requires a completed build and local socket permission.

```sh
PYTHONPATH=python/embodied/src DSH_EMBODIED_INTEGRATION=1 python -m pytest python/embodied/tests
```

`scripts/smoke.py` performs exactly one native and one DSH live inference over the same generated input, with no robot actions. It requires explicit deployment configuration and credentials. This is protocol evidence only; G1–G4 require the preregistered RoboDojo paired experiments and cannot be passed by this smoke.
