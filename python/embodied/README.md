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

The optional [worker shutdown patch](integrations/physicalrsi-worker-close.patch) uses the native worker’s existing close message for healthy idle skills, then awaits exit. Busy or failed workers receive termination; expiry forces a kill and waits for exit and the active pipe consumer before closing handles. `shutdown_timeout_s` and `kill_timeout_s` are positive finite native code-policy settings, defaulting to 15 and 10 seconds. Apply only to the source hashes in [its manifest](integrations/physicalrsi-worker-close.sources.json), in the isolated copy. Use the same worker implementation in every comparison arm.

Copy [the deployment example](configs/deepseek-flash.example.json), set absolute paths, and configure the native planner’s `max_tokens` to the same cap for comparisons. Export `PHYSICALRSI_PLANNER_BACKEND=dsh`, `PHYSICALRSI_DSH_CONFIG=/absolute/path/to/config.json`, and `PHYSICALRSI_AGENT_API_KEY` through your credential environment. The SDK process inherits the credential; neither configuration nor receipts contain it. Native transport remains the `api` backend.

## Planning and evidence

`PlannerBridge` owns one in-flight inference and uses fresh Sessions for compatibility calls. It rotates an idle runtime at the configured call count or when the domain prompt changes. `begin_episode` returns a reset generation; `reset` invalidates late responses. A stale, truncated, invalid, timed-out or duplicate request never returns an executable plan. A timeout has an unknown inference outcome and closes the owned runtime; it does not stop a robot or promise provider billing cancellation.

Each request writes immutable `request.json`, `events.json` and a terminal `receipt.json` under run/episode/request IDs. Receipts bind input and model configuration digests to the SDK Session and validated plan. Successful events are flushed before returning the plan; close the bridge to flush the runtime’s complete JSONL Session logs. Physical observations, actions, binding revisions and native outcomes must still be linked by the runner before an episode qualifies as complete evidence.

`ModelSpec.deepseek_reasoning` explicitly selects `off`, `low`, `high` or `max` through DSH’s existing DeepSeek compatibility settings. An absent value preserves the provider default; it does not disable thinking. `off` sends `thinking.type=disabled` without `reasoning_effort`. Freeze the same setting in every comparison arm; the [official thinking-mode guide](https://api-docs.deepseek.com/guides/thinking_mode/) describes its wire semantics.

## Online recovery provider

`recovery.py` defines `PhysicalRuntime`, `RuntimeState`, `StopReceipt`, `PlanRevision` and `RecoveryCoordinator`. The consumer routes every action chunk through the coordinator. An observed trigger disables dispatch, waits for a bounded acknowledgement with zero queued/in-flight actions, obtains a newer observation, and validates a replacement against pinned identities and current preconditions. It records the commit before binding, and a replacement must change the provider-validated `execution_key` derived from the capability and executable parameters, excluding rationale. Exceptions leave dispatch disabled. Only the native outcome marks success.

The single-environment `robodojo.py` consumer owns action dispatch and its cached tail. At a recovery checkpoint it blocks dispatch, drops the tail, verifies the simulator control queue is empty, and asks `OnlineModel` to reap the synchronous skill worker. No simulator steps run during planning. This acknowledges quiescent simulator control at an action boundary; it does not acknowledge stopped motion on a physical robot. A pending native control queue or unknown RPC outcome prevents replacement.

Action evidence converts numeric arrays and scalars to JSON values before dispatch while passing the original command to the native executor. Unsupported or nonfinite values fail before the dispatch record and physical call. Evidence serialization does not change action units, coordinates or array types consumed by RoboDojo.

`native_binding.py` defines the execution service and its synchronous code-policy provider. It owns native model creation, committed-plan delivery, observations, action chunks, cached tails and worker cleanup. Reopening must reproduce the captured configuration, catalog, operation registry and memory; a mismatch closes the replacement before actions. The online planner consumes this service and includes its provider identity in the execution binding revision. A cleanup exception permanently prevents reuse and further stop acknowledgement, even if the native model sets its closed flag; replace the owning service after resolving worker state.

`native_worker` specifies an absolute Python executable with the complete PhysicalRSI inference dependencies. The owned worker uses RoboDojo’s existing WebSocket protocol, bounds complete calls including reconnection, receives admitted plans without another planner call, and preserves native numeric action arrays. Stopping requires a native close acknowledgement, process exit and descendant quiescence; a new binding starts a new process and must reproduce the capability/memory snapshot. Any RPC or cleanup uncertainty permanently disables the provider. The worker receives a scrubbed credential environment. Its readiness, identity and logs live under the online evidence directory. This interface still requires real learned-policy simulator qualification.

The private `dsh-native-binding/v2` protocol distinguishes completed inference from declared skill failure. `SkillInferenceFailure` is raised only after native cleanup succeeds; the process provider additionally requires a matching close acknowledgement, worker exit and descendant quiescence. Invalid messages, mismatched protocol versions, transport loss and unconfirmed cleanup permanently disable the provider. Diagnostic error text is retained for investigation and is not model-visible event input. This distinction does not acknowledge physical stopping or authorize automatic retries; the online consumer currently ends the episode on these faults.

Worker configuration resolves `startup_timeout_seconds=300`, `request_timeout_seconds=120`, `exit_timeout_seconds=10`, `kill_timeout_seconds=5`, `connect_timeout_seconds=10`, `client_close_timeout_seconds=5` and `poll_interval_seconds=0.05`; each is configurable and must be positive and finite. The online RPC timeout must exceed startup, connection, metadata and binding requests; the recovery stop timeout must cover the request, process exit/kill, client close and the native client’s ten-second drain/join allowance. The surrounding policy-server startup deadline must also cover worker startup. Configure these controls before the run; defaults do not make the example’s shorter stop/RPC budgets sufficient.

Optional `initial_plan` in the online configuration pins an explicit native plan for the first binding without model inference. Native validation runs before the service is ready. The pilot copies the same plan into N and H0 deployments; no task identifier selects a hidden plan. H1 records the configured proposal with its input, catalog and binding digests, and follows the usual validate/commit/bind sequence. A later recovery still calls DSH with the committed plan, current observation and original task instruction. Configured initial binding creates no assistant turn or provider-call receipt. A zero-call run needs its deployment and native binding evidence; missing model receipts cannot certify this route by themselves.

Resetting a ready service before any episode retains its unused policy worker; it cannot discard active or failed execution state. Initial binding delivers its observation once. The next native observation update follows the first completed action, preserving the native observation/action cadence. Recovery still captures a newer observation after acknowledged stopping before binding the replacement.

`online_model.py` preserves PhysicalRSI’s native catalog, memory, domain prompt construction, validation, and binding implementation. Requests carry a phase-specific JSON Schema; operations are registered ID strings and cannot carry invented runtime parameters. Enhanced decisions add `execution`: initial `bind`, same-capability `restart`, different-capability `switch`, or `abort`. Restart creates a new skill worker and recomputes actions from fresh RGB/proprioception without resetting the scene. Initial binding uses native validation; lacking a restart guard does not remove a capability from initial selection. Replacements require configured observable restart preconditions; unknown or closed-gripper states can refuse recovery. Execution digests exclude rationale and history. The default in-process provider admits code-policy workers only. An explicit `native_worker` selects process isolation for the complete native catalog.

Online requests distinguish the historical trigger from `current_observation_status`, which describes the supplied cameras after stopping and recapture. A previous RGB-loss event does not imply that current RGB is absent. Recovery observations must have a sequence newer than the trigger; stale packets fail before inference. The status comes only from supplied observations, excluding disturbance labels and benchmark truth.

`recovery_enabled=false` selects the H1-R recovery ablation. Initial planning, observed-event records, the current native binding and native outcome checks remain active, but events cannot request a stop or replacement plan. Native skill errors still fail the episode. Missing settings resolve to `true`; other types fail during configuration loading. An empty restart-guard mapping is allowed only with recovery disabled. Disabling recovery is not evidence that a capability has safe restart support.

Use [the online configuration](configs/robodojo-online.example.json) through `DSH_ROBODOJO_ONLINE_CONFIG` in both processes. `scripts/robodojo_policy.py` starts RoboDojo’s loopback WebSocket server with an explicit JSON deployment containing `port`, `ready_file` and `model`; `scripts/robodojo_client.py` supplies the online evaluation consumer to native RoboDojo main from an isolated output directory. Both scripts require the audited PhysicalRSI and RoboDojo Python imports. The observed-event detector checks missing RGB, changed instructions, and commanded joint motion absent across a configured window. The motion rule requires configured observation and command preconditions; the pickup example requires both grippers to remain open, so normal closed-gripper contact does not trigger it. Events retain measured joint motion and command error. It does not infer grasp failure or target pose from benchmark state.

Initial and recovery requests share one durable DSH Session. Episode history is bounded by revision/event counts, the provider context limit and a common deadline; runtime rotation during an active chain fails instead of losing history. Automatic context summarization and offline history-to-skill admission remain separate work. Immutable runner records link observed triggers, stop acknowledgement, fresh observation sequence, plan commit, native binding, dispatched actions and native terminal outcome; request receipts link the actual sensor input and Session.

If evaluation and subsequent worker cleanup both fail, `ExecutionCleanupFailure` retains both errors and reports that dispatch remains disabled. A cleanup failure after native task success still fails the episode; the native terminal record does not certify worker exit.

`CallBudget` reserves one immutable slot before each model turn across processes and restarts. Failed and unknown attempts consume their slots. All runs sharing a budget must use the same immutable limit; tools and provider retries remain disabled. Recorded local model tests use a separate ledger. Exhaustion blocks subsequent planning and actions.

## Paired development pilot

Copy [the pilot specification](configs/paired-pilot.example.json), set absolute deployment paths, and export `PHYSICALRSI_AGENT_API_KEY`. Each assigned case runs native planning (N), DSH compatibility planning (H0), and online recovery (H1) in the declared order with independent simulator resets. The native planner and skill execution remain active in N/H0; `--mode native` selects them in both RoboDojo scripts. The example uses a previously explored pickup case and is development data.

```sh
python python/embodied/scripts/paired_pilot.py --spec /absolute/path/to/pilot.json
```

The loopback relay uses an ephemeral port and a private token; it forwards to the official DeepSeek endpoint and retains received/sent requests, response bytes, all provider attempts, elapsed time and usage. The specification's `deepseek_reasoning` selects `off`, `low`, `high` or `max` identically in every arm; an absent value retains the original pilot's `off` setting. Other messages and images stay unchanged. Conflicting model/output controls fail before inference. Native transport retries consume separate provider slots. Missing usage remains unknown. Child processes receive the relay token; the real API key is not written to evidence.

The pilot preserves native result files, exceptions, process deadlines, action counts and initial observable state/RGB digests. Reported evaluator time includes skill cold start and each evaluator's own teardown; it cannot qualify the formal native-terminal latency criterion. A failed episode is retained without automatic rerun. The assigned run order is development configuration, not a statistical randomization protocol. This runner refuses non-development partitions and never declares G1–G4 passed; formal acceptance additionally needs a qualified strongest-native configuration, complete S/L/U/X coverage, independent held-out cases, power calculation, preregistered paired intervals and common cost accounting.

The pilot report recomputes each retained provider request from its frozen model controls and received input. It verifies sent payload equality, response-byte SHA-256, HTTP status and raw JSON/SSE usage against the receipt. Missing or altered files cannot support a completed receipt; interrupted attempts remain visible as incomplete evidence. This audit verifies recorded calls, not physical success or statistical acceptance.

`scripts/online_chain_audit.py` audits one completed H1 pilot episode independently of the launcher summary. It checks each configured or model-produced plan against its receipt and executable input digest, then verifies stop/recapture ordering, immutable binding identities, revision-tagged dispatch and the recorded native outcome. Configured starts are checked against the frozen pilot input; they create no model receipt. Missing, altered, incomplete or failed chains cannot pass. This complements the separate native-result and raw-provider audits; it does not qualify held-out statistics.

`native-terminal.json` separately records the first existing native terminal check and first action dispatch. It adds no environment queries, ignores success flags before terminal, and excludes evaluator teardown from completion time. Cold start remains included; this record alone does not qualify the required warm-up or resource controls.

Optional absolute `online_config` and `policy_python` paths select a reviewed recovery template and policy interpreter for every arm. Omission retains the repository example and original policy environment. The runner validates both before creating assignments or reserving model calls, captures the template once in `resolved-inputs.json`, and records its SHA-256 and the interpreter file's SHA-256. Episode-specific evidence paths and common deadlines replace the captured template's placeholders. Detector thresholds and restart guards remain captured configuration. The executable hash does not identify installed dependencies or qualify a learned policy.

A case can pin `layout_id` and `layout_sha256` together. The index selects a native numerically sorted layout inside the CLI seed directory; changing `--seed` alone does not select another file within it. The client verifies the original file, retains an exact immutable snapshot and makes the native loader read that snapshot. It preserves native resume exclusions. The report checks the selected index and snapshot digest against the case; missing selection cannot support a successful pinned case. Layout files and identifiers never enter model inputs.

`scripts/generate_layout_cohort.py` uses the installed PhysicalRSI configuration catalog and input guard in isolated workers, with no model calls or benchmark-layout reads. It freezes assignments, sampler sources and split-specific seed streams before generation. Geometric generation does not validate physics or benchmark distribution. Validation cohorts require a frozen comparison SHA-256. A development case's `fresh_layout` contains an absolute `manifest`, its `manifest_sha256`, original cohort `index` and `split=development`, together with `layout_id=0` and the scene's file SHA-256. The native fresh-layout provider binds that one verified row at index zero; the retained selection preserves the original cohort identity. The development pilot refuses validation cohorts. Changed files, duplicate scenes, incomplete cohorts or missing receipts cannot reach binding.

`scripts/paired_pilot_report.py /absolute/path/to/pilot-output` prints the descriptive report after all assigned arms finish. Missing or duplicate assignments and mixed source versions reject report generation. Native results, exceptions, common deadlines and provider reservations are checked independently; zero-exit failures stay in the denominator and a missing provider receipt leaves usage unknown. Result-file digests support verification against retained evidence. The report always marks statistical acceptance as insufficient evidence.

The [acceptance record](evaluations/2026-10-21-acceptance/report.md) contains fixed-case development outcomes, evidence digests and the open formal acceptance conditions.

## Fixed-input Thinking comparison

`scripts/thinking_comparison.py` verifies an evidence archive's SHA-256, freezes every archived provider request and a randomized paired assignment before inference, then sends each assigned request once to the official DeepSeek endpoint. Both arms retain identical message text, images and history; both use nonstreaming transport. Token caps, repetitions, worker count, timeout and seed are explicit CLI options. Thinking uses effort `high`; temperature is retained in both payloads but ignored by the provider when thinking. A raw key file can be supplied with `--key-file`; credentials never enter saved requests or the protocol.

The oracle admits only the inspected single-capability pickup catalog. It checks native field types, registered handles and initial/recovery execution choices; it does not score physical success or rationale quality. Missing, failed, truncated and invalid requests remain in denominators. `--online-variants` adds a separately frozen original/clarified online prompt comparison without changing historical turns or images. `scripts/thinking_comparison_report.py` recomputes decisions from raw responses and rejects requests, receipts or responses that disagree with their frozen assignments. Repeated calls on these development snapshots do not qualify G1–G4.

The [Thinking diagnostic](evaluations/2026-10-21-thinking/report.md) records the configuration and prompt comparisons, failure mechanisms and evidence digests.

## Validation

Unit tests exercise request lifecycle and stop/observe/commit failures. The built-profile test uses a recorded local model response, verifies exact serialized domain text and PNG bytes, and checks persisted Session evidence. It requires a completed build and local socket permission.

```sh
PYTHONPATH=python/embodied/src DSH_EMBODIED_INTEGRATION=1 python -m pytest python/embodied/tests
```

`scripts/smoke.py` performs exactly one native and one DSH live inference over the same generated input, with no robot actions. It requires explicit deployment configuration and credentials. This is protocol evidence only; G1–G4 require the preregistered RoboDojo paired experiments and cannot be passed by this smoke.
