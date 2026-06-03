#!/usr/bin/env python3
import argparse
import concurrent.futures
import glob
import json
import pathlib
import re

from common import append_jsonl, extract_json_objects, load_config, progress, read_jsonl, rel, run, stable_id, write_jsonl, write_text


INCLUDE_RE = re.compile(r"^\s*-?\s*(?:include|sinclude)\s+(.+?)\s*$")
MAKE_VAR_RE = re.compile(r"\$\(([^)]+)\)")
TARGET_CUES = (
    "LOCAL_MODULE",
    "BUILD_SHARED_LIBRARY",
    "BUILD_STATIC_LIBRARY",
    "BUILD_EXECUTABLE",
    "BUILD_NATIVE_TEST",
    "BUILD_PACKAGE",
    "add_library",
    "add_executable",
)


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
as product/configuration variants of the current primary file. Read the
candidate files that jointly decide this primary file's build target, and put
them in `included_mk`.

If the primary file A and an included package.mk file B jointly decide one
artifact, output the artifact only once as A's target and list B in
`included_mk`. Do not output a duplicate standalone target for B.

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


def file_has_target_cues(text: str) -> bool:
    return any(cue in text for cue in TARGET_CUES)


def is_include_only_aggregator(text: str) -> bool:
    meaningful = []
    for raw in text.splitlines():
        line = strip_make_comment(raw).strip()
        if line:
            meaningful.append(line)
    if not meaningful:
        return False
    def include_or_assignment(line: str) -> bool:
        return bool(
            INCLUDE_RE.match(line)
        or ":=" in line
        or "+=" in line
        or "?=" in line
        or "=" in line
        )
    return not file_has_target_cues(text) and all(include_or_assignment(line) for line in meaningful)


def fixed_suffix_after_last_make_var(token: str) -> str:
    matches = list(MAKE_VAR_RE.finditer(token))
    suffix = token[matches[-1].end() :] if matches else token
    suffix = suffix.lstrip("/\\")
    if suffix:
        return suffix
    name = pathlib.PurePosixPath(token.replace("\\", "/")).name
    return name if name else "package.mk"


def glob_candidates(root: pathlib.Path, pattern: str, limit: int) -> list[pathlib.Path]:
    matches = []
    full_pattern = pattern if pathlib.Path(pattern).is_absolute() else str(root / pattern)
    for raw in sorted(glob.glob(full_pattern, recursive=True)):
        path = pathlib.Path(raw)
        if path.is_file():
            matches.append(path.resolve())
            if len(matches) >= limit:
                break
    return matches


def resolve_include_token(cfg: dict, mk_path: pathlib.Path, token: str) -> list[pathlib.Path]:
    root = pathlib.Path(cfg["project_root"]).resolve()
    limit = int(cfg.get("include_candidate_limit", 200))
    local_path = mk_path.parent.resolve()
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
            return glob_candidates(root, str(path_pattern), limit)
        return [path_pattern.resolve()] if path_pattern.is_file() else []

    suffix = fixed_suffix_after_last_make_var(normalized)
    suffix = suffix.replace("**/", "")
    if not suffix or suffix == ".":
        suffix = "package.mk"
    if any(ch in suffix for ch in "*?["):
        pattern = f"**/{suffix}"
    else:
        pattern = f"**/{suffix}"
    return glob_candidates(root, pattern, limit)


def scan_include_candidates(cfg: dict, mk_row: dict) -> tuple[list[dict], bool]:
    root = pathlib.Path(cfg["project_root"])
    mk_path = root / mk_row["path"]
    if not mk_path.exists():
        return [], False
    text = mk_path.read_text(errors="replace")
    tokens = include_tokens(text)
    candidates = []
    seen = set()
    for token in tokens:
        for candidate in resolve_include_token(cfg, mk_path, token):
            rel_candidate = rel(candidate, root)
            if rel_candidate == mk_row["path"] or rel_candidate in seen:
                continue
            seen.add(rel_candidate)
            candidates.append({"from": mk_row["path"], "include": token, "path": rel_candidate})
    can_cover_includes = bool(tokens) and not is_include_only_aggregator(text)
    return candidates, can_cover_includes


def collect_transitive_include_candidates(cfg: dict, row: dict) -> list[dict]:
    collected = []
    seen = set()
    origin = row["path"]

    def visit(current: dict) -> None:
        candidates, _ = scan_include_candidates(cfg, current)
        for candidate in candidates:
            path = candidate["path"]
            if path == origin or path in seen:
                continue
            seen.add(path)
            collected.append(candidate)
            visit({"path": path})

    visit(row)
    return collected


def enrich_and_filter_mk_rows(cfg: dict, rows: list[dict]) -> tuple[list[dict], list[dict]]:
    by_path = {row["path"]: row for row in rows}
    covered_by: dict[str, str] = {}
    enriched = []
    for row in rows:
        direct_candidates, can_cover = scan_include_candidates(cfg, row)
        candidates = collect_transitive_include_candidates(cfg, row) if can_cover else direct_candidates
        if candidates:
            row = dict(row)
            row["include_candidates"] = candidates
        enriched.append(row)
        if can_cover:
            for candidate in candidates:
                candidate_path = candidate["path"]
                if candidate_path in by_path and candidate_path not in covered_by:
                    covered_by[candidate_path] = row["path"]

    filtered = []
    skipped = []
    for row in enriched:
        covering_path = covered_by.get(row["path"])
        if covering_path:
            skipped.append(
                {
                    "schema_version": 1,
                    "path": row["path"],
                    "status": "skipped_included_mk",
                    "covered_by": covering_path,
                    "reason": "included by another primary mk file that is analyzed as the owning build target",
                }
            )
            continue
        filtered.append(row)
    return filtered, skipped


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


def analyze_one(
    cfg: dict,
    mk_row: dict,
    index: int,
    total: int,
    prompts_dir: pathlib.Path,
    results_dir: pathlib.Path,
    logs_dir: pathlib.Path,
) -> tuple[int, list[dict], dict | None, str]:
    progress(index, total, f"analyze {mk_row['path']}")
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
    normalized = []
    for idx, row in enumerate(rows):
        row.setdefault("schema_version", 1)
        row.setdefault("source_mk", mk_row["path"])
        row.setdefault("target_id", stable_id(f"{mk_row['path']}:{row.get('module', idx)}"))
        row["_analysis_source"] = mk_row["path"]
        normalized.append(row)
    failure = None
    if cp.returncode != 0:
        failure = {
            "mk_id": mk_row["mk_id"],
            "path": mk_row["path"],
            "returncode": cp.returncode,
            "stderr_log": rel(stderr_path),
            "stdout_log": rel(result_path),
        }
    message = (
        f"{mk_row['path']}: returncode={cp.returncode}; "
        f"extracted {len(normalized)} target record(s)"
    )
    return index, normalized, failure, message


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
    args = parser.parse_args()
    if args.jobs < 1:
        raise SystemExit("--jobs must be >= 1")
    cfg = load_config(args.config)
    state = pathlib.Path(cfg["state_dir"])
    mk_rows, skipped_rows = enrich_and_filter_mk_rows(cfg, load_mk_rows(cfg, state, args))
    if args.limit > 0:
        mk_rows = mk_rows[: args.limit]
    write_jsonl(state / "analyze_inputs.jsonl", mk_rows)
    write_jsonl(state / "skipped_mk_files.jsonl", skipped_rows)
    targets_path = state / "targets.jsonl"
    prompts_dir = state / "prompts"
    results_dir = state / "opencode_results"
    logs_dir = state / "logs"
    targets_path.unlink(missing_ok=True)

    print(f"analyzing {len(mk_rows)} mk file(s) with jobs={args.jobs}; skipped {len(skipped_rows)} covered include file(s)")
    results: dict[int, tuple[list[dict], dict | None, str]] = {}
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
            )
            for index, mk_row in enumerate(mk_rows, start=1)
        ]
        for future in concurrent.futures.as_completed(futures):
            index, rows, failure, message = future.result()
            results[index] = (rows, failure, message)
            print(message)

    all_targets = []
    failures = 0
    for index in sorted(results):
        rows, failure, _ = results[index]
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
