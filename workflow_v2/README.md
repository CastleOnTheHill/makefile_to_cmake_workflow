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

`products` in the workflow config are build verification entries only. Their
names and build commands are not sent to the Makefile analyzer as CMake
conditions, and the converter strips product labels from target JSON before
prompting OpenCode.

Generated subdirectory CMake files intentionally do not call `project()`.
Top-level build switches should be defined by your root CMake project; the
converter uses those Makefile-derived switches directly with `if(SWITCH_NAME)`.
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

For prototype runs, every subcommand accepts `--limit N`. Analysis and
conversion also accept `-j/--jobs` to run multiple OpenCode tasks in parallel:

```bash
workflow_v2/scripts/run_all.py workflow_v2/config.local.json --limit 5 -j 3
workflow_v2/scripts/analyze_mk_files.py workflow_v2/config.local.json --limit 20 -j 4 --timeout 1800
workflow_v2/scripts/convert_targets.py workflow_v2/config.local.json --limit 20 -j 4 --timeout 1800
```

You can also bypass discovery and analyze a hand-picked file set:

```bash
workflow_v2/scripts/analyze_mk_files.py workflow_v2/config.local.json \
  --mk-file path/to/Android.mk \
  --mk-file path/to/module.mk \
  -j 4

workflow_v2/scripts/analyze_mk_files.py workflow_v2/config.local.json \
  --file-list workflow_v2/state/typical_mk_files.txt \
  -j 4
```

Manual mk paths can be absolute or relative to `project_root`. The list file is
one path per line; blank lines and lines beginning with `#` are ignored.

Parallel analysis writes per-file prompt/stdout/stderr logs independently and
merges `targets.jsonl` in the original mk file order. Parallel conversion
serializes tasks that target the same `CMakeLists.txt`, so different
directories can run concurrently while one directory remains safe.

Analysis and conversion run in resumable mode by default. Existing successful
OpenCode outputs are reused; missing, failed, or timed-out items are retried
once in the next invocation. Use `--no-resume` to force reruns. Per-OpenCode
timeouts default to `analysis_opencode_timeout_seconds` and
`conversion_opencode_timeout_seconds`, falling back to `opencode_timeout_seconds`.
When a timeout fires, the OpenCode process group is terminated and the workflow
continues to the next item.

Before analysis, the script pre-scans `include`, `-include`, and `sinclude`
statements. If one primary package.mk file includes another package.mk file and
they jointly define the same artifact, the included file is marked as covered
and skipped as a later primary input. Variable or wildcard includes are expanded
into candidate `package.mk` paths and passed to the analyzer prompt. Candidate
search is capped by `include_candidate_limit`, defaulting to 200. Candidate
matching uses only `state/mk_files.jsonl`, the file list produced by
`discover_mk_files.py`; `analyze_mk_files.py` does not recursively scan the
filesystem. Use `--no-include-prescan` to bypass this local pre-scan.

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
- `state/analyze_inputs.jsonl`: exact mk inputs used by the latest analysis
  run, including manually specified files.
- `state/skipped_mk_files.jsonl`: mk files skipped because another analyzed
  primary file includes them as part of the same artifact.
- `state/targets.jsonl`: analyzed target records produced by `v2-mk-analyzer`.
- `state/analysis_status.jsonl`: per-mk analysis attempt status, including
  reused, failed, and timed-out runs.
- `state/conversion_status.jsonl`: per-target conversion attempt status,
  including reused, failed, and timed-out runs.
- `state/build_state.jsonl`: build attempts and failure signatures.
- `state/manual_required.md`: stalled failures that require human action.
- `state/prompts/`: exact prompts sent to OpenCode.
- `state/opencode_results/`: stdout returned by OpenCode.
- `state/logs/`: build logs and OpenCode stderr traces.
