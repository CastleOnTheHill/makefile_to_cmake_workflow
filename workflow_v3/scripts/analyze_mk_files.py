#!/usr/bin/env python3
import argparse
import concurrent.futures
import json
import pathlib
import tempfile

from common import (
    TIMEOUT_RETURNCODE,
    analysis_result_path,
    cmake_path_for_mk,
    extract_json_objects,
    load_config,
    log_path,
    now,
    progress,
    rel,
    resolve_path,
    run,
    stable_id,
    write_jsonl,
    write_text,
)
from excel_board import (
    COL_ANALYSIS_DONE,
    COL_ANALYSIS_OK,
    COL_ANALYSIS_RESULT,
    COL_ANALYSIS_RETURN,
    COL_ANALYSIS_STATUS,
    COL_ANALYSIS_STDERR,
    COL_ANALYSIS_STDOUT,
    COL_ANALYSIS_TARGETS,
    COL_ANALYSIS_UPDATED,
    COL_CMAKE_PATH,
    COL_CONVERSION_DONE,
    COL_CONVERSION_OK,
    COL_CONVERSION_STATUS,
    COL_MK_PATH,
    COL_TASK_ID,
    load_board,
    mutate_board,
    row_by_path,
    yes_no,
)


def prompt_for(cfg: dict, row: dict[str, str]) -> str:
    mk_path = row[COL_MK_PATH]
    return """# workflow_v3 Makefile/Android.mk Analysis Task

Analyze exactly this primary build file and emit JSONL only.

Primary task:
```json
%s
```

Project root: `%s`
Expected CMake output file for later conversion: `%s`

Rules:

- Every discovered Makefile/Android.mk/*.mk file is analyzed independently by
  workflow_v3. Do not skip this primary file just because it is included by
  another mk file. Duplicate target facts are acceptable.
- Read include files only when they affect targets, variables, conditions,
  sources, includes, defines, compile options, link options, or libraries of
  this primary file.
- If the primary file and an included file jointly define one artifact, emit
  one target record for that artifact and put the included file path in
  `included_mk`.
- If the primary file is only an include aggregator, such as
  `include $(LOCAL_PATH)/*/package.mk`, do not expand child targets. Emit one
  `target_type: "include_aggregator"` record and put intended CMake aggregation
  facts in `cmake_includes`.
- If an include uses variables or product-specific path fragments, inspect the
  filesystem when needed and model alternative included files with structured
  conditions. Do not infer conditions from workflow config; workflow_v3 has no
  product-name conditions.
- Preserve conditional logic as structured data. If a condition adds sources,
  include dirs, defines, compile options, link libraries, or link options, fill
  the matching `conditional_*` field. Do not leave this behavior only in prose.
- If a switch variable is not defined in the current file or included files,
  use the variable anyway. Production outer CMake defines these switches.
- Resolve source wildcards and source collection functions when the repository
  tree makes that possible. Never output `src/*.cpp` as a final source list if
  concrete files can be read from the tree.
- If no real target or include-aggregator behavior exists, output nothing.

Output JSONL only. Each line must be a JSON object with this schema:

{
  "schema_version": 1,
  "source_mk": "%s",
  "included_mk": [],
  "target_id": "stable human-readable id",
  "module": "original module or target name",
  "target_type": "shared_library|static_library|executable|gtest|prebuilt|include_aggregator|unknown",
  "conditions": [],
  "conditional_sources": [],
  "conditional_include_dirs": [],
  "conditional_defines": [],
  "conditional_compile_options": [],
  "conditional_link_libraries": [],
  "conditional_link_options": [],
  "sources": [],
  "generated_sources": [],
  "include_dirs": [],
  "export_include_dirs": [],
  "defines": [],
  "compile_options": [],
  "link_libraries": [],
  "link_options": [],
  "cmake_includes": [],
  "c_standard": "",
  "cxx_standard": "",
  "artifacts": [],
  "risks": [],
  "confidence": "high|medium|low"
}
""" % (
        json.dumps(row, ensure_ascii=False, indent=2),
        cfg["project_root"],
        row[COL_CMAKE_PATH],
        mk_path,
    )


def normalize_mk_path(cfg: dict, value: str) -> str:
    root = pathlib.Path(cfg["project_root"])
    path = pathlib.Path(value)
    if not path.is_absolute():
        path = root / path
    path = path.resolve()
    if not path.exists() or not path.is_file():
        raise SystemExit(f"mk/makefile path does not exist: {value}")
    return rel(path, root)


def read_file_list(path: str) -> list[str]:
    rows = []
    with pathlib.Path(path).open(encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if line and not line.startswith("#"):
                rows.append(line)
    return rows


def selected_rows(cfg: dict, rows: list[dict[str, str]], args: argparse.Namespace) -> list[dict[str, str]]:
    manual = list(args.mk_file or [])
    for list_path in args.file_list or []:
        manual.extend(read_file_list(list_path))
    if not manual:
        return rows

    wanted = {normalize_mk_path(cfg, value) for value in manual}
    by_path = row_by_path(rows)
    missing = sorted(path for path in wanted if path not in by_path)
    if missing:
        raise SystemExit("these paths are not in the Excel board; run discover first: " + ", ".join(missing))
    return [by_path[path] for path in sorted(wanted)]


def analysis_success(row: dict[str, str]) -> bool:
    result = row.get(COL_ANALYSIS_RESULT, "")
    return row.get(COL_ANALYSIS_STATUS) in {"done", "empty"} and bool(result) and resolve_path(result).exists()


def rows_needing_analysis(rows: list[dict[str, str]], force: bool) -> list[dict[str, str]]:
    if force:
        return rows
    return [row for row in rows if not analysis_success(row)]


def normalize_targets(mk_row: dict[str, str], targets: list[dict]) -> list[dict]:
    normalized = []
    mk_path = mk_row[COL_MK_PATH]
    for index, target in enumerate(targets):
        target.setdefault("schema_version", 1)
        target["source_mk"] = mk_path
        target["_workflow_v3_task_id"] = mk_row[COL_TASK_ID]
        if not target.get("target_id"):
            key = f"{mk_path}:{target.get('module', '')}:{target.get('target_type', '')}:{index}"
            target["target_id"] = stable_id(key)
        normalized.append(target)
    return normalized


def write_temp_prompt(cfg: dict, task_id: str, text: str) -> pathlib.Path:
    tmp_dir = pathlib.Path(cfg["state_dir"]) / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=str(tmp_dir), prefix=f"{task_id}.", suffix=".prompt.md") as f:
        f.write(text)
        return pathlib.Path(f.name)


def analyze_one(cfg: dict, row: dict[str, str], index: int, total: int, timeout: int) -> dict:
    task_id = row[COL_TASK_ID]
    mk_path = row[COL_MK_PATH]
    result_path = analysis_result_path(cfg, task_id)
    stdout_path = log_path(cfg, f"{task_id}.analysis", "out")
    stderr_path = log_path(cfg, f"{task_id}.analysis", "err")
    prompt = write_temp_prompt(cfg, task_id, prompt_for(cfg, row))
    try:
        cmd = [
            cfg["opencode_bin"],
            "run",
            "--agent",
            "v3-mk-analyzer",
            "--model",
            cfg["model"],
            "--file",
            str(prompt),
            "--",
            "Analyze this Makefile/Android.mk file and output JSONL only.",
        ]
        cp = run(cmd, cfg, timeout=timeout)
    finally:
        prompt.unlink(missing_ok=True)

    write_text(stdout_path, cp.stdout)
    write_text(stderr_path, cp.stderr)
    targets = normalize_targets(row, extract_json_objects(cp.stdout))
    write_jsonl(result_path, targets)

    if cp.returncode == TIMEOUT_RETURNCODE:
        status = "timeout"
    elif cp.returncode != 0:
        status = "failed"
    else:
        status = "done" if targets else "empty"

    return {
        "mk_path": mk_path,
        "task_id": task_id,
        "status": status,
        "returncode": cp.returncode,
        "targets": len(targets),
        "analysis_result": rel(result_path),
        "stdout_log": rel(stdout_path),
        "stderr_log": rel(stderr_path),
        "message": f"{mk_path}: status={status} returncode={cp.returncode} targets={len(targets)}",
    }


def update_board_row(cfg: dict, status_row: dict) -> None:
    board_path = pathlib.Path(cfg["board_path"])

    def update(headers: list[str], rows: list[dict[str, str]]):
        by_path = row_by_path(rows)
        row = by_path.get(status_row["mk_path"])
        if row is None:
            return headers, rows

        status = status_row["status"]
        ok = status in {"done", "empty"}
        row[COL_ANALYSIS_STATUS] = status
        row[COL_ANALYSIS_DONE] = yes_no(ok)
        row[COL_ANALYSIS_OK] = yes_no(ok)
        row[COL_ANALYSIS_RETURN] = str(status_row["returncode"])
        row[COL_ANALYSIS_TARGETS] = str(status_row["targets"])
        row[COL_ANALYSIS_RESULT] = status_row["analysis_result"]
        row[COL_ANALYSIS_STDOUT] = status_row["stdout_log"]
        row[COL_ANALYSIS_STDERR] = status_row["stderr_log"]
        row[COL_ANALYSIS_UPDATED] = now()
        row[COL_CMAKE_PATH] = rel(cmake_path_for_mk(cfg, row[COL_MK_PATH]), pathlib.Path(cfg["project_root"]))

        if ok:
            row[COL_CONVERSION_STATUS] = "no_targets" if status == "empty" else "pending"
            row[COL_CONVERSION_DONE] = yes_no(status == "empty")
            row[COL_CONVERSION_OK] = yes_no(status == "empty")
        return headers, rows

    mutate_board(board_path, update)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("config")
    parser.add_argument("--limit", type=int, default=0, help="maximum number of not-yet-successful rows to analyze")
    parser.add_argument("-j", "--jobs", type=int, default=1, help="parallel OpenCode analysis jobs")
    parser.add_argument("--timeout", type=int, default=0, help="per-OpenCode timeout seconds")
    parser.add_argument("--force", action="store_true", help="rerun selected rows even if analysis is already successful")
    parser.add_argument("--mk-file", action="append", default=[], help="specific mk/makefile path from the Excel board; repeatable")
    parser.add_argument("--file-list", "--mk-list", action="append", default=[], help="text file of mk/makefile paths; repeatable")
    args = parser.parse_args()
    if args.jobs < 1:
        raise SystemExit("--jobs must be >= 1")

    cfg = load_config(args.config)
    timeout = args.timeout or int(cfg.get("analysis_opencode_timeout_seconds", cfg.get("opencode_timeout_seconds", 1800)))
    headers, board_rows = load_board(cfg["board_path"])
    if not board_rows:
        raise SystemExit(f"Excel board is empty or missing; run discover first: {cfg['board_path']}")
    candidates = selected_rows(cfg, board_rows, args)
    tasks = rows_needing_analysis(candidates, args.force)
    if args.limit > 0:
        tasks = tasks[: args.limit]

    print(
        f"analysis board={rel(cfg['board_path'])}; selected={len(candidates)}; "
        f"to_run={len(tasks)}; jobs={args.jobs}; timeout={timeout}s; force={args.force}",
        flush=True,
    )
    if not tasks:
        return 0

    failures = 0
    completed = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as executor:
        futures = [
            executor.submit(analyze_one, cfg, row, index, len(tasks), timeout)
            for index, row in enumerate(tasks, start=1)
        ]
        for future in concurrent.futures.as_completed(futures):
            status_row = future.result()
            update_board_row(cfg, status_row)
            completed += 1
            progress(completed, len(tasks), f"saved analysis {status_row['mk_path']}")
            print(status_row["message"], flush=True)
            if status_row["status"] not in {"done", "empty"}:
                failures += 1

    if failures:
        print(f"analysis finished with {failures} failed/timeout task(s)", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
