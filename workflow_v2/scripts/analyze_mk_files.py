#!/usr/bin/env python3
import json
import pathlib
import sys

from common import append_jsonl, extract_json_objects, load_config, read_jsonl, rel, require_args, run, stable_id, write_text


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

Follow necessary include files if they affect targets, conditions, sources,
flags, or dependencies. Preserve conditional logic in the output JSON.
""" % (
        json.dumps(mk_row, ensure_ascii=False, indent=2),
        json.dumps(products, ensure_ascii=False, indent=2),
        cfg["project_root"],
        rel(cfg["scan_subdir"]),
    )


def main() -> int:
    cfg = load_config(require_args(sys.argv, "usage: analyze_mk_files.py <config.json>"))
    state = pathlib.Path(cfg["state_dir"])
    mk_rows = read_jsonl(state / "mk_files.jsonl")
    targets_path = state / "targets.jsonl"
    prompts_dir = state / "prompts"
    results_dir = state / "opencode_results"
    logs_dir = state / "logs"
    targets_path.unlink(missing_ok=True)

    all_targets = []
    for mk_row in mk_rows:
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

