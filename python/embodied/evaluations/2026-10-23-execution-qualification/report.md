# Execution qualification: native skills and control transforms

English | [中文](report.zh.md)

The subsequent [renderer restoration and physical trials](renderer-restoration.md) resolve this record’s first-frame blocker and complete six native task trials. The following retains the initial execution checks and their limitations.

## Scope and status

Increment A implements versioned capability descriptions, explicit zero-planner binding probes, native-only trials, actual policy-transform diagnostics and digest-checked coverage reports. The implementation and its regression tests are complete; physical capability acceptance remains incomplete because the renderer supplies an empty first frame. No new task effect is marked verified, and these development records do not establish held-out quality or G1–G4 acceptance.

## Confirmed defects and fixes

The original XPolicyLab pi05 training configuration defaults to `adapt_to_pi=false` and delta joint actions. Installed OpenPI defaults to `adapt_to_pi=true`; the port omitted both fields and inherited joint-sign and gripper conversion absent from training. The [source-pinned patch](../../integrations/physicalrsi-pi05-transforms.patch) explicitly sets false/true in an isolated provider copy. The [training comparison](training-transform-comparison.json) retains inspected definitions and source digests. Sparse-memory already sets false/false and was tested separately.

The [camera buffer inspection](camera-buffer.json) records a CUDA image request returning an empty CPU array: zero values instead of 1,228,800 for one 480×640 RGBA frame. The original camera then invokes its copy kernel against that buffer. A 300-second preflight timed out before any observation or action. The [source-pinned camera patch](../../integrations/robodojo-camera-frame.patch) rejects incomplete frames before allocation or kernel dispatch; a separate real simulator run emitted the expected frame-unavailable error and exited. This prevents the observed invalid dispatch but does not restore renderer output. Cache isolation and importing bundled Warp early did not restore a frame; isolated copies of the CPU and CUDA kernel passed synthetic pixel checks. The cause of missing renderer output remains unresolved.

## Measured execution evidence

The [runtime summary](runtime-summary.json) and [archive receipt](runtime-archive.json) retain assignments, failures, source/dependency identities and original outputs. Three GPU probes used the same previously recorded RGB/state input, explicit plans and real checkpoints. Each generated 50 actions, dispatched zero robot actions, and confirmed worker cleanup. Camera HWC-to-CHW encoding, left-arm/gripper/right-arm/gripper state order, transformed tensor integrity, native command conversion and RPC equality passed the offline trace audit for all three probes.

| Probe | Binding and inference | Trace audit | Physical task coverage |
|---|---|---|---|
| pi05 before the fix | Passed; 50 generated actions | Passed | Unverified |
| pi05 after the fix | Passed; 50 generated actions | Passed | Unverified |
| Sparse-memory | Passed; 50 generated actions | Passed | Unverified |

For the identical open-gripper input, the actual normalized gripper value changed from 0.891682 to 1.000398 after the pi05 fix. This demonstrates that the correction reaches preprocessing; it is not a task-success comparison or a validation of all training statistics. The sparse probe starts with 20 masked history frames and does not validate long-horizon memory. Physical cadence, contact behavior, closed-loop success and intermediate effects remain unverified.

The two six-case baseline batches and two three-case candidate batches retain all 18 assignments: two baseline trials were interrupted at first-frame acquisition and 16 never started. A separate original-path preflight timed out. Three camera diagnostics retain an import failure caused by the first diagnostic launcher's virtual-environment resolution, the intentional buffer-inspection stop, and the successful guard rejection. All 22 simulation assignments remain visible; none produced an initial observation or action. The gateway retained zero planning attempts. Diagnostic stops, simulator exit code zero and empty native result lists are not task successes. All experiment processes were confirmed quiescent before archiving.

## Retained earlier evidence

The [historical import audit](import-audit.json) checked all 1,805 members of the prior learned-policy archive. Three original native N episodes remain failed: pickup with 200 actions, stacking with 400, and toolbox with 900; all had zero planning attempts. Their manifests and reports recompute from retained raw binding, result and timing bytes. Historical dependency evidence is incomplete and cannot certify execution coverage. The earlier nine N/H0/H1 failures remain in the original archive; this audit imports only its three native assignments.

The [toolbox coverage report](toolbox-code.report.json) marks hammer, wrench and pliers unsupported by the inspected program; tape-measure placement and robot return remain unverified. Removing `moves[:1]` would not supply object-specific control or independent completion checks. The [earlier sparse probe](sparse-binding-inference.json) only generated actions and did not dispatch them.

## Reproduction and checks

The [deployment reference](deployment.json) retains the user-confirmed SSH host, port, key-file path and project directory, without private-key content or API credentials. The [adapter reference](../../README.md) owns input formats and commands. Apply each external patch only after its source digest and `git apply --check` match, in an isolated copy. The runtime archive retains raw RGB/state, actual transforms, checkpoint file hashes, original assignments, logs and loaded policy source files; its receipt names both remote and local archive locations. Verify the archive hash and every `members.json` entry before replaying its records.

Run `scripts/qualify_execution.py` for each retained manifest with a new output path. Uncovered effects exit with status 2; missing or modified evidence rejects the audit. All five retained coverage reports are recomputed by tests. Failed and unexecuted assignments cannot be removed to improve coverage, and whole-task success cannot certify intermediate effects.

| Local validation | Result |
|---|---|
| `DSH_EMBODIED_INTEGRATION=1`, repository Python paths, `python/sdk/.venv/bin/python -m pytest python/embodied/tests` | 277 passed, including subprocess/profile integration, trace corruption rejection and camera frame protection. |
| Focused `scripts/verify-repository-references.spec.ts` | 19 passed; only the immutable raw episode source-identity field is exempted from prose reference checks. |
| `pnpm run build`, then `pnpm run build:check` | Passed. |
| `pnpm run lint` | Passed, with an existing unused-disable warning in the experimental client translation package. |
| `pnpm run doc-sync` | 42 passed; one existing commit-hash reference at `python/sdk/uv.lock:113` failed. That file matches HEAD exactly. See the [validation receipt](validation.json). |

The next physical verification requires a real nonempty renderer frame, followed by the preserved native pi05/sparse assignments under zero-planner enforcement. Keep the previous failures and unexecuted rows in the experiment record. Do not proceed to capability-dependent recovery acceptance or claim physical task gains from these probes.
