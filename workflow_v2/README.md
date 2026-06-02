# Workflow V2: Multi-Product Makefile/Android.mk to CMake

This directory is a fresh implementation for complex embedded projects where one
codebase supports multiple products and must be validated under multiple build
configurations.

V2 uses three OpenCode agents:

- `v2-mk-analyzer`: reads one Makefile/Android.mk/*.mk at a time and emits
  target JSONL.
- `v2-cmake-converter`: converts one target JSON object into CMake and writes
  trace comments.
- `v2-build-fixer`: receives one build failure excerpt and makes the smallest
  CMake fix.

Generated subdirectory CMake files intentionally do not call `project()`.
Top-level product switches should be defined by your root CMake project; the
converter uses them directly with `if(SWITCH_NAME)`.
Conditional build logic must be implemented as real CMake logic. If a
Makefile/Android.mk switch adds sources, defines, include directories, compile
options, link libraries, or link options, the converter should emit
`if(...)` blocks with the matching `target_*` commands, not leave the behavior
as comments.

Start by copying and editing:

```bash
cp workflow_v2/config.example.json workflow_v2/config.local.json
```

Choose an output layout:

- `centralized`: write generated CMake under `cmake_output_dir`.
- `beside_mk`: write `CMakeLists.txt` beside each target's source
  Makefile/Android.mk/*.mk file.

Then run:

```bash
workflow_v2/scripts/discover_mk_files.py workflow_v2/config.local.json
workflow_v2/scripts/analyze_mk_files.py workflow_v2/config.local.json
workflow_v2/scripts/convert_targets.py workflow_v2/config.local.json
workflow_v2/scripts/build_repair_loop.py workflow_v2/config.local.json
```

For prototype runs, every subcommand accepts `--limit N`, and conversion also
accepts `-j/--jobs`:

```bash
workflow_v2/scripts/run_all.py workflow_v2/config.local.json --limit 5 -j 3
workflow_v2/scripts/convert_targets.py workflow_v2/config.local.json --limit 20 -j 4
```

Parallel conversion serializes tasks that target the same `CMakeLists.txt`, so
different directories can run concurrently while one directory remains safe.

If the build repair loop stops for manual intervention, fix the generated CMake
or project inputs, mark the item, then rerun the loop:

```bash
workflow_v2/scripts/mark_manual_fixed.py workflow_v2/config.local.json <product-or-task-id>
workflow_v2/scripts/build_repair_loop.py workflow_v2/config.local.json
```

Runtime state is written under `workflow_v2/state/` and can be deleted or
archived between experiments.

## Main Files

- `state/mk_files.jsonl`: discovered Makefile/Android.mk/*.mk inputs.
- `state/targets.jsonl`: analyzed target records produced by `v2-mk-analyzer`.
- `state/build_state.jsonl`: build attempts and failure signatures.
- `state/manual_required.md`: stalled failures that require human action.
- `state/prompts/`: exact prompts sent to OpenCode.
- `state/opencode_results/`: stdout returned by OpenCode.
- `state/logs/`: build logs and OpenCode stderr traces.
