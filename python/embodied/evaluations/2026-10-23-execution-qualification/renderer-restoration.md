# Renderer restoration and physical task verification

English | [中文](renderer-restoration.zh.md)

## Repair and scope

The empty-first-frame renderer defect is fixed, and the six remaining native physical trials for increment A are complete. Installed Isaac Sim could not resolve `libGLU.so.1` through its loader search path, preventing `libneuray.so` and the RTX renderer from initializing; the annotator subsequently returned an empty CPU array. Adding the existing isolated library directory to the simulator process restored three RGB cameras and 200 real actions on the same fixed pickup case. Renderer errors disappeared from the client log. Shared system and installed Python environments were not modified.

`simulation_library_dirs` prepends explicitly configured existing absolute directories to the simulator’s `LD_LIBRARY_PATH`. Invalid directories fail before launch; policy-process environments do not consume this setting. The [library record](restored-graphics-runtime.json) retains GLU’s actual path, file digest and dependency resolution; the [deployment reference](deployment.json) retains the reusable directory. The empty-frame guard remains in place to prevent missing images from reaching the copy kernel.

## Physical outcomes

The table covers one original-config pi05 control and six preassigned restored native N trials. The six trials test pi05 and sparse-memory on three fixed development tasks each. Every trial obtained three 480×640 RGB first frames and completed independent evaluation, process exit and cleanup; planning attempts were zero. Task outcomes remain separate from action generation.

| Policy | Task | Dispatched actions | Native task outcome |
|---|---|---|---|
| pi05 (original-config control) | general_pickup | 200 | Failure |
| pi05 | general_pickup | 200 | Failure |
| pi05 | stack_blocks_by_language | 400 | Failure |
| pi05 | store_tools_in_toolbox | 900 | Failure |
| sparse-memory | general_pickup | 76 | Success |
| sparse-memory | stack_blocks_by_language | 400 | Failure |
| sparse-memory | store_tools_in_toolbox | 900 | Failure |

The [machine-readable results](restored-summary.json) link each descriptor, manifest, raw binding, first frame, timing, terminal record and coverage report. Sparse-memory pickup succeeded after 76 actions, with native successful termination at 22.38 seconds and first action at about 13.52 seconds. These times include policy cold start and are not an uninstrumented performance comparison. Coverage verifies only `task-complete` in this fixed development case, with one sample; it does not establish task-family or intermediate-subgoal support.

The other restored trials did not succeed and remain unverified. The pi05 transform correction produced no successful task in these three cases; sparse-memory stacking and toolbox also failed. Previously unsupported toolbox-program effects remain unsupported. Execution verification is complete, while capability coverage targets are not all satisfied; subsequent recovery can use only the measured support scope. Formal held-out runs remain zero and G1–G4 are not accepted.

## Tests and reproducible evidence

The [archive receipt](restored-archive.json) records local/remote locations and SHA-256. The archive retains all seven trials’ raw logs, observations, actual transforms, actions, catalogs and plans, restored adapter sources, actually loaded policy sources, dependency versions and checkpoint inventory. The previous 22 failed or unexecuted assignments and their archive remain intact. Archiving occurred only after all experiment processes stopped.

Tests check directory configuration, parent-environment isolation and rejection, and recompute coverage from all seven raw records. All six frozen assignments must remain present, including failures. The [validation receipt](restored-validation.json) records the executed tests and documentation checks. Every archive payload member was digest-checked; native whole-task success supports only `task-complete`.
