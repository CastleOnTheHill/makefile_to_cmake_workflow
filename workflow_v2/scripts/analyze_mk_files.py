#!/usr/bin/env python3
import argparse
import json
import pathlib

from common import append_jsonl, extract_json_objects, load_config, progress, read_jsonl, rel, run, stable_id, write_text


def prompt_for(cfg: dict, mk_row: dict) -> str:
    products = cfg.get("products", [])
    return """# Makefile Analysis Task

Analyze the primary build file below and emit JSONL only.

Primary file:
```json
%s
```

Products/configurations to preserve:
```json
%s
```

Project root: `%s`
Scan subdir: `%s`

Follow necessary include files only when they affect a real target defined by
the primary file.

If the primary file is only an include aggregator, for example:

```make
include $(LOCAL_PATH)/*/package.mk
```

do not expand the included package.mk files and do not duplicate child targets.
Emit exactly one JSONL record with `"target_type": "include_aggregator"` and put
the suggested CMake include/add_subdirectory entries in `cmake_includes`.

Preserve conditional logic in the output JSON.

If a condition adds sources, defines, include directories, compile options,
link libraries, or link options, put the effect into the matching
`conditional_*` field. Do not leave conditional behavior only as prose in
`conditions`.

If a switch variable is not defined in the current file or included files, keep
using the variable anyway. The outer build/CMake project defines production
switches. Do not report undefined production switch variables as risks.
""" % (
        json.dumps(mk_row, ensure_ascii=False, indent=2),
        json.dumps(products, ensure_ascii=False, indent=2),
        cfg["project_root"],
        rel(cfg["scan_subdir"]),
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("config")
    parser.add_argument("--limit", type=int, default=0, help="maximum number of mk files to analyze")
    args = parser.parse_args()
    cfg = load_config(args.config)
    state = pathlib.Path(cfg["state_dir"])
    mk_rows = read_jsonl(state / "mk_files.jsonl")
    if args.limit > 0:
        mk_rows = mk_rows[: args.limit]
    targets_path = state / "targets.jsonl"
    prompts_dir = state / "prompts"
    results_dir = state / "opencode_results"
    logs_dir = state / "logs"
    targets_path.unlink(missing_ok=True)

    all_targets = []
    for index, mk_row in enumerate(mk_rows, start=1):
        progress(index, len(mk_rows), f"analyze {mk_row['path']}")
        prompt_path = prompts_dir / f"{mk_row['mk_id']}.v2-mk-analyzer.md"
        result_path = results_dir / f"{mk_row['mk_id']}.v2-mk-analyzer.out"
        stderr_path = logs_dir / f"{mk_row['mk_id']}.v2-mk-analyzer.err"
        write_text(prompt_path, prompt_for(cfg, mk_row))
        cmd = [
            cfg["opencode_bin"],
            "run",
            "--agent",
            "v2-mk-analyzer",
            "--model",
            cfg.get("model", "deepseek/deepseek-v4-pro"),
            "--file",
            str(prompt_path),
            "--",
            "Analyze this Makefile/Android.mk file and output JSONL only.",
        ]
        cp = run(cmd, cfg)
        write_text(result_path, cp.stdout)
        write_text(stderr_path, cp.stderr)
        rows = extract_json_objects(cp.stdout)
        print(f"  opencode returncode={cp.returncode}; extracted {len(rows)} target record(s)")
        for idx, row in enumerate(rows):
            row.setdefault("schema_version", 1)
            row.setdefault("source_mk", mk_row["path"])
            row.setdefault("target_id", stable_id(f"{mk_row['path']}:{row.get('module', idx)}"))
            row["_analysis_source"] = mk_row["path"]
            append_jsonl(targets_path, row)
            all_targets.append(row)
        if cp.returncode != 0:
            append_jsonl(
                state / "analysis_failures.jsonl",
                {
                    "mk_id": mk_row["mk_id"],
                    "path": mk_row["path"],
                    "returncode": cp.returncode,
                    "stderr_log": rel(stderr_path),
                    "stdout_log": rel(result_path),
                },
            )

    print(f"wrote {targets_path} ({len(all_targets)} target records)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
