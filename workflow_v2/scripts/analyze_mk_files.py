#!/usr/bin/env python3
import argparse
import concurrent.futures
import fnmatch
import json
import pathlib
import re

from common import TIMEOUT_RETURNCODE, append_jsonl, extract_json_objects, load_config, now, progress, read_jsonl, rel, run, stable_id, write_jsonl, write_text
from task_board import get_item, load_board, save_board, upsert_item


INCLUDE_RE = re.compile(r"^\s*(?:-|s)?include\s+(.+?)\s*$")
MAKE_VAR_RE = re.compile(r"\$\(([^)]+)\)")


def prompt_for(cfg: dict, mk_row: dict) -> str:
    return """# Makefile Analysis Task

Analyze the primary build file below and emit JSONL only.

Primary file:
```json
%s
```

Project root: `%s`
Scan subdir: `%s`

Pre-scanned include candidate files:
```json
%s
```

If `include` or `-include` uses variables or wildcards and the pre-scanned
candidate list contains matching `package.mk` files, consider those candidates
as possible product/configuration variants of the current primary file. Read a
candidate file only when it is necessary to understand the current primary file,
and put files that actually affect this primary file's target in `included_mk`.

Every discovered Makefile/Android.mk/*.mk file is analyzed independently by the
workflow. Do not suppress the current primary file just because it may be
included by another file, and do not rely on `included_mk` to deduplicate later
analysis tasks.

Workflow config `products` are only build verification entries. They are not
Makefile/CMake conditions. Do not infer target enablement, source selection, or
CMake `if(...)` logic from workflow product names or build commands.

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

Resolve Makefile source wildcards and source collection functions when possible.
For example, `src/*.cpp`, `$(wildcard ...)`, `$(call all-c-files-under,...)`,
and `$(call all-cpp-files-under,...)` should become concrete file lists in
`sources` or `conditional_sources` when the repository tree makes that possible.
If the concrete file list cannot be determined statically, do not invent files;
record the original expression and uncertainty in `risks`.

If a switch variable is not defined in the current file or included files, keep
using the variable anyway. The outer build/CMake project defines production
switches. Do not report undefined production switch variables as risks.
""" % (
        json.dumps(mk_row, ensure_ascii=False, indent=2),
        cfg["project_root"],
        rel(cfg["scan_subdir"]),
        json.dumps(mk_row.get("include_candidates", []), ensure_ascii=False, indent=2),
    )


def normalize_mk_path(cfg: dict, value: str) -> str:
    root = pathlib.Path(cfg["project_root"])
    path = pathlib.Path(value)
    if not path.is_absolute():
        path = root / path
    path = path.resolve()
    if not path.exists():
        raise SystemExit(f"manual mk file does not exist: {value}")
    if not path.is_file():
        raise SystemExit(f"manual mk path is not a file: {value}")
    return rel(path, root)


def read_manual_list(path: str) -> list[str]:
    rows = []
    list_path = pathlib.Path(path)
    with list_path.open() as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            rows.append(line)
    return rows


def strip_make_comment(line: str) -> str:
    escaped = False
    out = []
    for ch in line:
        if ch == "#" and not escaped:
            break
        out.append(ch)
        escaped = ch == "\\" and not escaped
        if ch != "\\":
            escaped = False
    return "".join(out)


def include_tokens(text: str) -> list[str]:
    tokens = []
    for raw in text.splitlines():
        line = strip_make_comment(raw).strip()
        match = INCLUDE_RE.match(line)
        if not match:
            continue
        for token in match.group(1).split():
            if token:
                tokens.append(token)
    return tokens


def fixed_suffix_after_last_make_var(token: str) -> str:
    matches = list(MAKE_VAR_RE.finditer(token))
    suffix = token[matches[-1].end() :] if matches else token
    suffix = suffix.lstrip("/\\")
    if suffix:
        return suffix
    name = pathlib.PurePosixPath(token.replace("\\", "/")).name
    return name if name else "package.mk"


def known_mk_paths(cfg: dict) -> list[str]:
    return list(cfg.get("_known_mk_paths", []))


def known_mk_path_set(cfg: dict) -> set[str]:
    return set(known_mk_paths(cfg))


def known_candidate_by_exact_path(cfg: dict, path: pathlib.Path) -> list[pathlib.Path]:
    project_root = pathlib.Path(cfg["project_root"]).resolve()
    try:
        rel_path = path.resolve().relative_to(project_root).as_posix()
    except ValueError:
        return []
    if rel_path not in known_mk_path_set(cfg):
        return []
    return [path.resolve()]


def known_candidates_by_pattern(cfg: dict, pattern: str, limit: int) -> list[pathlib.Path]:
    project_root = pathlib.Path(cfg["project_root"]).resolve()
    pattern_text = str(pattern).replace("\\", "/")
    matches = []
    for rel_path in known_mk_paths(cfg):
        abs_path = (project_root / rel_path).resolve()
        rel_text = rel_path.replace("\\", "/")
        abs_text = abs_path.as_posix()
        if fnmatch.fnmatch(abs_text, pattern_text) or fnmatch.fnmatch(rel_text, pattern_text):
            matches.append(abs_path)
            if len(matches) >= limit:
                break
    return matches


def known_candidates_by_suffix(
    cfg: dict,
    suffix: str,
    *,
    allow_pattern: bool,
    limit: int,
) -> list[pathlib.Path]:
    project_root = pathlib.Path(cfg["project_root"]).resolve()
    suffix = suffix.replace("\\", "/").lstrip("/")
    matches = []
    for rel_path in known_mk_paths(cfg):
        rel_text = rel_path.replace("\\", "/")
        matched = fnmatch.fnmatch(rel_text, suffix) if allow_pattern else rel_text.endswith(suffix)
        if matched:
            matches.append((project_root / rel_path).resolve())
            if len(matches) >= limit:
                break
    return matches


def resolve_include_token(cfg: dict, mk_path: pathlib.Path, token: str) -> list[pathlib.Path]:
    root = pathlib.Path(cfg["project_root"]).resolve()
    limit = int(cfg.get("include_candidate_limit", 200))
    local_path = mk_path.parent.resolve()
    cache_key = (str(local_path), token)
    cache = cfg.setdefault("_include_token_cache", {})
    if cache_key in cache:
        return cache[cache_key]

    normalized = token.replace("\\", "/")
    normalized = normalized.replace("$(LOCAL_PATH)", str(local_path))
    normalized = normalized.replace("${LOCAL_PATH}", str(local_path))
    normalized = normalized.replace("$(PROJECT_ROOT)", str(root))
    normalized = normalized.replace("${PROJECT_ROOT}", str(root))
    normalized = normalized.replace("$(project)", str(root))
    normalized = normalized.replace("${project}", str(root))

    if "$(" not in normalized and "${" not in normalized:
        path_pattern = pathlib.Path(normalized)
        if not path_pattern.is_absolute():
            path_pattern = local_path / path_pattern
        if any(ch in str(path_pattern) for ch in "*?["):
            result = known_candidates_by_pattern(cfg, str(path_pattern), limit)
        else:
            result = known_candidate_by_exact_path(cfg, path_pattern)
        cache[cache_key] = result
        return result

    suffix = fixed_suffix_after_last_make_var(normalized)
    suffix = suffix.replace("**/", "")
    if not suffix or suffix == ".":
        suffix = "package.mk"
    allow_pattern = any(ch in suffix for ch in "*?[")
    result = known_candidates_by_suffix(cfg, suffix, allow_pattern=allow_pattern, limit=limit)
    cache[cache_key] = result
    return result


def scan_include_candidates(cfg: dict, mk_row: dict) -> list[dict]:
    if not cfg.get("include_prescan_enabled", True):
        return []
    cache = cfg.setdefault("_include_scan_cache", {})
    cache_key = mk_row["path"]
    if cache_key in cache:
        return cache[cache_key]

    root = pathlib.Path(cfg["project_root"])
    mk_path = root / mk_row["path"]
    if not mk_path.exists():
        cache[cache_key] = []
        return []
    text = mk_path.read_text(errors="replace")
    tokens = include_tokens(text)
    candidates = []
    seen = set()
    for token in tokens:
        resolved = resolve_include_token(cfg, mk_path, token)
        for candidate in resolved:
            rel_candidate = rel(candidate, root)
            if rel_candidate == mk_row["path"] or rel_candidate in seen:
                continue
            seen.add(rel_candidate)
            candidates.append({"from": mk_row["path"], "include": token, "path": rel_candidate})
    cache[cache_key] = candidates
    return candidates


def load_discovered_mk_paths(state: pathlib.Path) -> list[str]:
    known = []
    seen = set()
    for row in read_jsonl(state / "mk_files.jsonl"):
        path = row.get("path")
        if path and path not in seen:
            seen.add(path)
            known.append(path)
    return known


def enrich_mk_rows(cfg: dict, rows: list[dict], state: pathlib.Path) -> list[dict]:
    cfg["_known_mk_paths"] = load_discovered_mk_paths(state)
    if not cfg.get("include_prescan_enabled", True):
        print("include pre-scan disabled; analyzing every mk file without include candidate context", flush=True)
        return rows

    enriched = []
    print(
        "pre-scanning include candidates "
        f"for {len(rows)} mk file(s); enabled={cfg.get('include_prescan_enabled', True)}; "
        f"known_mk_files={len(known_mk_paths(cfg))}; "
        f"candidate_source={state / 'mk_files.jsonl'}",
        flush=True,
    )
    for index, row in enumerate(rows, start=1):
        if index == 1 or index == len(rows) or index % 50 == 0:
            progress(index, len(rows), f"prescan {row['path']}")
        candidates = scan_include_candidates(cfg, row)
        if candidates:
            row = dict(row)
            row["include_candidates"] = candidates
        enriched.append(row)
    return enriched


def mk_rows_from_manual_files(cfg: dict, values: list[str]) -> list[dict]:
    rows = []
    seen = set()
    for value in values:
        mk_path = normalize_mk_path(cfg, value)
        if mk_path in seen:
            continue
        seen.add(mk_path)
        rows.append(
            {
                "schema_version": 1,
                "mk_id": stable_id(mk_path),
                "path": mk_path,
                "status": "manual",
            }
        )
    return rows


def load_mk_rows(cfg: dict, state: pathlib.Path, args: argparse.Namespace) -> list[dict]:
    manual_files = list(args.mk_file or [])
    for list_path in args.file_list or []:
        manual_files.extend(read_manual_list(list_path))
    if manual_files:
        return mk_rows_from_manual_files(cfg, manual_files)
    return read_jsonl(state / "mk_files.jsonl")


def latest_status_by_key(path: pathlib.Path, key: str) -> dict[str, dict]:
    latest = {}
    for row in read_jsonl(path):
        value = row.get(key)
        if value:
            latest[value] = row
    return latest


def merge_legacy_analysis_failures(latest: dict[str, dict], failure_path: pathlib.Path) -> dict[str, dict]:
    for row in read_jsonl(failure_path):
        mk_id = row.get("mk_id")
        if mk_id and mk_id not in latest:
            latest[mk_id] = {
                "mk_id": mk_id,
                "path": row.get("path"),
                "status": row.get("status", "failed"),
                "returncode": row.get("returncode", 1),
            }
    return latest


def seed_analysis_board(
    board: dict,
    mk_rows: list[dict],
    latest_status: dict[str, dict],
    results_dir: pathlib.Path,
    logs_dir: pathlib.Path,
) -> None:
    for index, row in enumerate(mk_rows, start=1):
        mk_id = row["mk_id"]
        result_path = results_dir / f"{mk_id}.v2-mk-analyzer.out"
        stderr_path = logs_dir / f"{mk_id}.v2-mk-analyzer.err"
        item = get_item(board, "analysis", mk_id)
        fields = {
            "path": row["path"],
            "order": index,
            "stdout_log": rel(result_path),
            "stderr_log": rel(stderr_path),
        }
        if item is None:
            legacy = latest_status.get(mk_id, {})
            status = legacy.get("status", "pending")
            if status in {"done", "empty"} and not result_path.exists():
                status = "pending"
            fields.update(
                {
                    "status": status,
                    "returncode": legacy.get("returncode"),
                    "rows": legacy.get("rows"),
                }
            )
            upsert_item(board, "analysis", mk_id, **fields)
        else:
            upsert_item(board, "analysis", mk_id, touch=False, **fields)


def update_analysis_board(board: dict, status_row: dict) -> None:
    upsert_item(
        board,
        "analysis",
        status_row["mk_id"],
        status=status_row.get("status"),
        path=status_row.get("path"),
        returncode=status_row.get("returncode"),
        rows=status_row.get("rows"),
        reused=status_row.get("reused"),
        timeout_seconds=status_row.get("timeout_seconds"),
        stdout_log=status_row.get("stdout_log"),
        stderr_log=status_row.get("stderr_log"),
    )


def normalize_rows(mk_row: dict, rows: list[dict]) -> list[dict]:
    normalized = []
    for idx, row in enumerate(rows):
        row.setdefault("schema_version", 1)
        row.setdefault("source_mk", mk_row["path"])
        row.setdefault("target_id", stable_id(f"{mk_row['path']}:{row.get('module', idx)}"))
        row["_analysis_source"] = mk_row["path"]
        normalized.append(row)
    return normalized


def rows_from_existing_result(mk_row: dict, result_path: pathlib.Path) -> list[dict]:
    if not result_path.exists():
        return []
    return normalize_rows(mk_row, extract_json_objects(result_path.read_text(errors="replace")))


def should_reuse_analysis(
    mk_row: dict,
    board_item: dict | None,
    latest_status: dict | None,
    result_path: pathlib.Path,
    stderr_path: pathlib.Path,
    resume: bool,
) -> bool:
    if not resume or not result_path.exists():
        return False
    if board_item:
        return board_item.get("status") in {"done", "empty"}
    if latest_status:
        return latest_status.get("status") in {"done", "empty"}
    rows = extract_json_objects(result_path.read_text(errors="replace"))
    if rows:
        return True
    if stderr_path.exists() and not stderr_path.read_text(errors="replace").strip():
        return True
    return False


def analyze_one(
    cfg: dict,
    mk_row: dict,
    index: int,
    total: int,
    prompts_dir: pathlib.Path,
    results_dir: pathlib.Path,
    logs_dir: pathlib.Path,
    board_item: dict | None,
    latest_status: dict | None,
    timeout: int | None,
    resume: bool,
) -> tuple[int, list[dict], dict | None, dict, str]:
    prompt_path = prompts_dir / f"{mk_row['mk_id']}.v2-mk-analyzer.md"
    result_path = results_dir / f"{mk_row['mk_id']}.v2-mk-analyzer.out"
    stderr_path = logs_dir / f"{mk_row['mk_id']}.v2-mk-analyzer.err"
    if should_reuse_analysis(mk_row, board_item, latest_status, result_path, stderr_path, resume):
        rows = rows_from_existing_result(mk_row, result_path)
        status = "done" if rows else "empty"
        status_row = {
            "mk_id": mk_row["mk_id"],
            "path": mk_row["path"],
            "status": status,
            "returncode": 0,
            "rows": len(rows),
            "reused": True,
            "time": now(),
            "stdout_log": rel(result_path),
            "stderr_log": rel(stderr_path),
        }
        message = f"{mk_row['path']}: reused existing result; extracted {len(rows)} target record(s)"
        return index, rows, None, status_row, message

    progress(index, total, f"analyze {mk_row['path']}")
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
    cp = run(cmd, cfg, timeout=timeout)
    write_text(result_path, cp.stdout)
    write_text(stderr_path, cp.stderr)
    normalized = normalize_rows(mk_row, extract_json_objects(cp.stdout))
    failure = None
    if cp.returncode != 0:
        failure = {
            "mk_id": mk_row["mk_id"],
            "path": mk_row["path"],
            "returncode": cp.returncode,
            "status": "timeout" if cp.returncode == TIMEOUT_RETURNCODE else "failed",
            "stderr_log": rel(stderr_path),
            "stdout_log": rel(result_path),
        }
    status = "timeout" if cp.returncode == TIMEOUT_RETURNCODE else "failed" if cp.returncode != 0 else "done" if normalized else "empty"
    status_row = {
        "mk_id": mk_row["mk_id"],
        "path": mk_row["path"],
        "status": status,
        "returncode": cp.returncode,
        "rows": len(normalized),
        "reused": False,
        "time": now(),
        "timeout_seconds": timeout,
        "stdout_log": rel(result_path),
        "stderr_log": rel(stderr_path),
    }
    message = (
        f"{mk_row['path']}: returncode={cp.returncode}; "
        f"extracted {len(normalized)} target record(s)"
    )
    return index, normalized, failure, status_row, message


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("config")
    parser.add_argument("--limit", type=int, default=0, help="maximum number of mk files to analyze")
    parser.add_argument(
        "--mk-file",
        action="append",
        default=[],
        help="specific Makefile/Android.mk/*.mk file to analyze; repeatable",
    )
    parser.add_argument(
        "--file-list",
        "--mk-list",
        action="append",
        default=[],
        help="text file containing Makefile/Android.mk/*.mk paths, one per line; repeatable",
    )
    parser.add_argument("-j", "--jobs", type=int, default=1, help="parallel OpenCode analysis jobs")
    parser.add_argument(
        "--timeout",
        type=int,
        default=0,
        help="per-OpenCode analysis timeout in seconds; defaults to analysis_opencode_timeout_seconds or opencode_timeout_seconds from config",
    )
    parser.add_argument("--no-resume", action="store_true", help="rerun every analysis task instead of reusing existing successful results")
    parser.add_argument(
        "--no-include-prescan",
        action="store_true",
        help="disable local include pre-scan; useful when include candidate matching is too noisy",
    )
    args = parser.parse_args()
    if args.jobs < 1:
        raise SystemExit("--jobs must be >= 1")
    cfg = load_config(args.config)
    if args.no_include_prescan:
        cfg["include_prescan_enabled"] = False
    timeout = args.timeout or int(cfg.get("analysis_opencode_timeout_seconds", cfg.get("opencode_timeout_seconds", 1800)))
    resume = not args.no_resume
    state = pathlib.Path(cfg["state_dir"])
    mk_rows = enrich_mk_rows(cfg, load_mk_rows(cfg, state, args), state)
    if args.limit > 0:
        mk_rows = mk_rows[: args.limit]
    write_jsonl(state / "analyze_inputs.jsonl", mk_rows)
    write_jsonl(state / "skipped_mk_files.jsonl", [])
    targets_path = state / "targets.jsonl"
    status_path = state / "analysis_status.jsonl"
    prompts_dir = state / "prompts"
    results_dir = state / "opencode_results"
    logs_dir = state / "logs"
    latest_status = merge_legacy_analysis_failures(
        latest_status_by_key(status_path, "mk_id"),
        state / "analysis_failures.jsonl",
    )
    board = load_board(cfg)
    seed_analysis_board(board, mk_rows, latest_status, results_dir, logs_dir)
    board_path = save_board(cfg, board)
    targets_path.unlink(missing_ok=True)

    print(
        f"analyzing {len(mk_rows)} mk file(s) with jobs={args.jobs}; "
        f"timeout={timeout}s; resume={resume}; include-covered skip disabled; "
        f"board={rel(board_path)}"
    )
    results: dict[int, tuple[list[dict], dict | None, dict, str]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as executor:
        futures = [
            executor.submit(
                analyze_one,
                cfg,
                mk_row,
                index,
                len(mk_rows),
                prompts_dir,
                results_dir,
                logs_dir,
                get_item(board, "analysis", mk_row["mk_id"]),
                latest_status.get(mk_row["mk_id"]),
                timeout,
                resume,
            )
            for index, mk_row in enumerate(mk_rows, start=1)
        ]
        for future in concurrent.futures.as_completed(futures):
            index, rows, failure, status_row, message = future.result()
            results[index] = (rows, failure, status_row, message)
            update_analysis_board(board, status_row)
            save_board(cfg, board)
            print(message)

    all_targets = []
    failures = 0
    for index in sorted(results):
        rows, failure, status_row, _ = results[index]
        append_jsonl(status_path, status_row)
        for row in rows:
            append_jsonl(targets_path, row)
            all_targets.append(row)
        if failure:
            append_jsonl(state / "analysis_failures.jsonl", failure)
            failures += 1
    print(f"wrote {targets_path} ({len(all_targets)} target records)")
    if failures:
        print(f"analysis finished with {failures} failed OpenCode run(s)")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
