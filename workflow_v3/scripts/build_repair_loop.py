#!/usr/bin/env python3
import argparse
import difflib
import fnmatch
import json
import os
import pathlib
import queue
import re
import shlex
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any

from common import (
    TIMEOUT_RETURNCODE,
    error_excerpt,
    failure_key,
    failure_signature,
    load_config,
    now,
    progress,
    rel,
    resolve_path,
    run,
    shell_env,
    write_text,
)


TEXT_FILE_NAMES = {"Android.mk", "CMakeLists.txt", "Makefile"}
SNAPSHOT_FILE_NAMES = {"CMakeLists.txt"}


@dataclass(frozen=True)
class BuildCommand:
    name: str
    cwd: pathlib.Path
    command: str
    timeout: int


@dataclass(frozen=True)
class SnapshotEntry:
    text: str


@dataclass(frozen=True)
class FileChange:
    status: str
    rel_path: str
    diff: str


def project_path(cfg: dict[str, Any], value: str | pathlib.Path) -> pathlib.Path:
    path = pathlib.Path(value)
    return path if path.is_absolute() else pathlib.Path(cfg["project_root"]) / path


def state_file(cfg: dict[str, Any], key: str, default_name: str) -> pathlib.Path:
    value = cfg.get(key)
    if value:
        return resolve_path(value)
    return pathlib.Path(cfg["state_dir"]) / default_name


def sanitize_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_") or "command"


def append_text(path: pathlib.Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(text)


def read_json(path: pathlib.Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as f:
        value = json.load(f)
    return value if isinstance(value, dict) else {}


def write_json(path: pathlib.Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def configured_build_commands(cfg: dict[str, Any]) -> list[BuildCommand]:
    raw_commands = cfg.get("build_commands") or []
    if not raw_commands and cfg.get("products"):
        for product in cfg.get("products", []):
            if product.get("build_command"):
                raw_commands.append(
                    {
                        "name": product.get("name") or f"product-{len(raw_commands) + 1}",
                        "cwd": ".",
                        "command": product["build_command"],
                    }
                )

    default_timeout = int(cfg.get("build_command_timeout_seconds", 3600))
    commands: list[BuildCommand] = []
    for index, item in enumerate(raw_commands, start=1):
        if isinstance(item, str):
            name = f"build-{index}"
            cwd = pathlib.Path(cfg["project_root"])
            command = item
            timeout = default_timeout
        elif isinstance(item, dict):
            name = str(item.get("name") or f"build-{index}")
            cwd = project_path(cfg, item.get("cwd") or ".")
            command = str(item.get("command") or "")
            timeout = int(item.get("timeout_seconds") or default_timeout)
        else:
            continue
        if command.strip():
            commands.append(BuildCommand(name=name, cwd=cwd, command=command, timeout=timeout))

    if not commands:
        raise SystemExit("config must define build_commands with at least one command")
    return commands


def run_shell_command(command: BuildCommand, cfg: dict[str, Any]) -> subprocess.CompletedProcess[str]:
    echo_output = bool(cfg.get("build_repair_echo_output", True))
    proc = subprocess.Popen(
        command.command,
        shell=True,
        cwd=str(command.cwd),
        env=shell_env(cfg),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        executable="/bin/bash",
        start_new_session=True,
    )
    output_queue: queue.Queue[str] = queue.Queue()

    def read_output() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            output_queue.put(line)

    reader = threading.Thread(target=read_output, daemon=True)
    reader.start()
    lines: list[str] = []

    def drain_output() -> None:
        while True:
            try:
                line = output_queue.get_nowait()
            except queue.Empty:
                return
            lines.append(line)
            if echo_output:
                print(line, end="", flush=True)

    deadline = time.monotonic() + command.timeout
    timed_out = False
    while proc.poll() is None:
        drain_output()
        if time.monotonic() > deadline:
            timed_out = True
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            break
        time.sleep(0.1)

    if timed_out:
        stop_deadline = time.monotonic() + 5
        while proc.poll() is None and time.monotonic() < stop_deadline:
            drain_output()
            time.sleep(0.1)
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
        reader.join(timeout=5)
        drain_output()
        timeout_msg = f"\n[TIMEOUT] build command exceeded timeout_seconds={command.timeout}; process group was terminated.\n"
        if echo_output:
            print(timeout_msg, end="", flush=True)
        return subprocess.CompletedProcess(command.command, TIMEOUT_RETURNCODE, "".join(lines) + timeout_msg, "")

    reader.join(timeout=5)
    drain_output()
    return subprocess.CompletedProcess(command.command, proc.returncode, "".join(lines), "")


def relative_to_project(cfg: dict[str, Any], path: pathlib.Path) -> str:
    return rel(path, pathlib.Path(cfg["project_root"])).replace("\\", "/")


def ignored_patterns(cfg: dict[str, Any]) -> list[str]:
    patterns = list(cfg.get("ignore_dirs", [])) + list(cfg.get("build_repair_ignore_dirs", []))
    state_dir = pathlib.Path(cfg["state_dir"])
    try:
        patterns.append(str(state_dir.resolve().relative_to(pathlib.Path(cfg["project_root"]).resolve())).replace("\\", "/"))
    except ValueError:
        pass
    return patterns + [".git", "__pycache__"]


def should_ignore_dir(cfg: dict[str, Any], path: pathlib.Path) -> bool:
    rel_path = relative_to_project(cfg, path)
    name = path.name
    if name in {".git", "__pycache__"}:
        return True
    for pattern in ignored_patterns(cfg):
        normalized = str(pattern).strip().rstrip("/").replace("\\", "/")
        if not normalized:
            continue
        if rel_path == normalized or rel_path.startswith(normalized + "/"):
            return True
        if fnmatch.fnmatch(rel_path, normalized) or fnmatch.fnmatch(rel_path + "/", normalized.rstrip("/") + "/"):
            return True
    return False


def is_snapshot_candidate(path: pathlib.Path) -> bool:
    return path.name in SNAPSHOT_FILE_NAMES


def read_snapshot_text(path: pathlib.Path, max_bytes: int) -> str | None:
    try:
        if path.stat().st_size > max_bytes:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in data:
        return None
    return data.decode("utf-8", errors="replace")


def snapshot_roots(cfg: dict[str, Any]) -> list[pathlib.Path]:
    raw_roots = cfg.get("build_repair_snapshot_roots") or [cfg["scan_subdir"]]
    roots: list[pathlib.Path] = []
    for item in raw_roots:
        path = project_path(cfg, item)
        if path.exists():
            roots.append(path)
    return roots


def take_snapshot(cfg: dict[str, Any]) -> dict[str, SnapshotEntry]:
    max_bytes = int(cfg.get("build_repair_snapshot_max_file_bytes", 2_000_000))
    snapshot: dict[str, SnapshotEntry] = {}
    for root in snapshot_roots(cfg):
        if root.is_file():
            files = [root]
        else:
            files = []
            for dirpath, dirnames, filenames in os.walk(root):
                current = pathlib.Path(dirpath)
                dirnames[:] = [name for name in dirnames if not should_ignore_dir(cfg, current / name)]
                for filename in filenames:
                    files.append(current / filename)
        for path in files:
            if not is_snapshot_candidate(path):
                continue
            text = read_snapshot_text(path, max_bytes)
            if text is None:
                continue
            snapshot[relative_to_project(cfg, path)] = SnapshotEntry(text=text)
    return snapshot


def unified_diff(rel_path: str, old: str, new: str) -> str:
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"a/{rel_path}",
            tofile=f"b/{rel_path}",
        )
    )


def diff_snapshots(before: dict[str, SnapshotEntry], after: dict[str, SnapshotEntry]) -> list[FileChange]:
    changes: list[FileChange] = []
    for rel_path in sorted(set(before) | set(after)):
        old = before.get(rel_path)
        new = after.get(rel_path)
        if old is None and new is not None:
            changes.append(FileChange("added", rel_path, unified_diff(rel_path, "", new.text)))
        elif old is not None and new is None:
            changes.append(FileChange("deleted", rel_path, unified_diff(rel_path, old.text, "")))
        elif old is not None and new is not None and old.text != new.text:
            changes.append(FileChange("modified", rel_path, unified_diff(rel_path, old.text, new.text)))
    return changes


def restore_deleted_files(cfg: dict[str, Any], before: dict[str, SnapshotEntry], changes: list[FileChange]) -> list[str]:
    restored: list[str] = []
    root = pathlib.Path(cfg["project_root"])
    for change in changes:
        if change.status != "deleted":
            continue
        old = before.get(change.rel_path)
        if old is None:
            continue
        path = root / change.rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(old.text, encoding="utf-8")
        restored.append(change.rel_path)
    return restored


def format_changes(changes: list[FileChange], max_chars_per_file: int) -> str:
    if not changes:
        return "_No tracked text file changes were detected._\n"
    rows: list[str] = []
    for change in changes:
        diff_text = change.diff
        if len(diff_text) > max_chars_per_file:
            diff_text = diff_text[:max_chars_per_file] + "\n... diff truncated by build_repair_diff_max_chars ...\n"
        rows.extend(
            [
                f"### {change.status}: `{change.rel_path}`",
                "",
                "```diff",
                diff_text.rstrip(),
                "```",
                "",
            ]
        )
    return "\n".join(rows)


def extract_paths_from_log(cfg: dict[str, Any], log_text: str) -> list[pathlib.Path]:
    root = pathlib.Path(cfg["project_root"])
    pattern = re.compile(
        r"(?P<path>(?:[A-Za-z0-9_+@%./:-]+/)*(?:CMakeLists\.txt|Android\.mk|Makefile|[A-Za-z0-9_+@%.-]+\.(?:mk|cmake|c|cc|cpp|cxx|h|hh|hpp|hxx)))"
    )
    paths: list[pathlib.Path] = []
    seen: set[pathlib.Path] = set()
    for match in pattern.finditer(log_text):
        raw = match.group("path").strip("`'\"()[]{}:,;")
        if "://" in raw:
            continue
        path = pathlib.Path(raw)
        candidates = [path] if path.is_absolute() else [root / path, pathlib.Path.cwd() / path]
        for candidate in candidates:
            try:
                resolved = candidate.resolve()
            except OSError:
                continue
            if resolved.exists() and resolved not in seen:
                paths.append(resolved)
                seen.add(resolved)
                break
    return paths


def source_dirs_from_command(cfg: dict[str, Any], command: BuildCommand) -> list[pathlib.Path]:
    dirs: list[pathlib.Path] = []
    try:
        parts = shlex.split(command.command)
    except ValueError:
        return dirs
    for index, part in enumerate(parts):
        source_arg = ""
        if part in {"-S", "--source"} and index + 1 < len(parts):
            source_arg = parts[index + 1]
        elif part.startswith("-S") and len(part) > 2:
            source_arg = part[2:]
        if not source_arg:
            continue
        path = pathlib.Path(source_arg)
        if not path.is_absolute():
            path = command.cwd / path
        if path.exists() and path.is_dir():
            dirs.append(path.resolve())
    return dirs


def find_related_mk_files(
    cfg: dict[str, Any],
    log_text: str,
    limit: int,
    extra_dirs: list[pathlib.Path] | None = None,
) -> list[pathlib.Path]:
    root = pathlib.Path(cfg["project_root"]).resolve()
    scan_root = pathlib.Path(cfg["scan_subdir"]).resolve()
    directories: list[pathlib.Path] = []
    for directory in extra_dirs or []:
        resolved = directory.resolve()
        if resolved not in directories:
            directories.append(resolved)
    for path in extract_paths_from_log(cfg, log_text):
        parent = path.parent if path.is_file() else path
        while True:
            if parent not in directories:
                directories.append(parent)
            if parent == scan_root or parent == root or parent.parent == parent:
                break
            parent = parent.parent

    related: list[pathlib.Path] = []
    seen: set[pathlib.Path] = set()

    def add(path: pathlib.Path) -> None:
        try:
            resolved = path.resolve()
        except OSError:
            return
        if resolved.exists() and resolved.is_file() and resolved not in seen:
            related.append(resolved)
            seen.add(resolved)

    for directory in directories:
        add(directory / "Android.mk")
        add(directory / "Makefile")
        for mk in sorted(directory.glob("*.mk")):
            add(mk)
        if len(related) >= limit:
            return related[:limit]

    if not related and scan_root.exists():
        for dirpath, dirnames, filenames in os.walk(scan_root):
            current = pathlib.Path(dirpath)
            dirnames[:] = [name for name in dirnames if not should_ignore_dir(cfg, current / name)]
            for filename in sorted(filenames):
                if filename in TEXT_FILE_NAMES or filename.endswith(".mk"):
                    add(current / filename)
                    if len(related) >= limit:
                        return related[:limit]
    return related[:limit]


def mk_context(cfg: dict[str, Any], paths: list[pathlib.Path]) -> str:
    max_lines = int(cfg.get("build_repair_mk_context_lines", 120))
    blocks: list[str] = []
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        lines = text.splitlines()
        if len(lines) > max_lines:
            text = "\n".join(lines[:max_lines]) + "\n... truncated ..."
        blocks.append(
            "## `%s`\n\n```make\n%s\n```" % (relative_to_project(cfg, path), text)
        )
    return "\n\n".join(blocks) if blocks else "_No nearby mk/Makefile context was found._"


def read_experience_for_prompt(cfg: dict[str, Any]) -> str:
    path = state_file(cfg, "build_experience_file", "build_experience.md")
    if not path.exists():
        return "_No build repair experience has been recorded yet._"
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    prompt_lines = int(cfg.get("build_experience_prompt_lines", 80))
    return "\n".join(lines[-prompt_lines:])


def read_manual_experience_for_prompt(cfg: dict[str, Any]) -> str:
    path = state_file(cfg, "build_manual_experience_file", "build_manual_experience.md")
    if not path.exists():
        return "_No manual build repair experience file exists yet._"
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    prompt_lines = int(cfg.get("build_manual_experience_prompt_lines", cfg.get("build_experience_prompt_lines", 80)))
    return "\n".join(lines[-prompt_lines:])


def compact_experience(cfg: dict[str, Any]) -> None:
    path = state_file(cfg, "build_experience_file", "build_experience.md")
    if not path.exists():
        return
    max_lines = int(cfg.get("build_experience_max_lines", 160))
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    if len(lines) <= max_lines:
        return

    header = [
        "# workflow_v3 Build Repair Experience",
        "",
        "Keep this file compact. New conversion and build repair prompts use the tail of this file.",
        "",
    ]
    bullets = [line for line in lines if line.startswith("- ")]
    deduped_reversed: list[str] = []
    seen: set[str] = set()
    for line in reversed(bullets):
        key = re.sub(r"^\- \[[^\]]+\]\s*", "- ", line)
        if key in seen:
            continue
        seen.add(key)
        deduped_reversed.append(line)
    kept = list(reversed(deduped_reversed))[-max(20, max_lines - len(header)) :]
    path.write_text("\n".join(header + kept).rstrip() + "\n", encoding="utf-8")


def one_line(text: str, limit: int = 320) -> str:
    compact = re.sub(r"\s+", " ", text).strip()
    return compact[:limit] + ("..." if len(compact) > limit else "")


def append_experience(cfg: dict[str, Any], pending_fix: dict[str, Any], outcome: str) -> None:
    path = state_file(cfg, "build_experience_file", "build_experience.md")
    if not path.exists():
        write_text(
            path,
            "# workflow_v3 Build Repair Experience\n\n"
            "Keep this file compact. New conversion and build repair prompts use the tail of this file.\n\n",
        )
    files = ", ".join(pending_fix.get("changed_files") or [])
    summary = one_line(str(pending_fix.get("summary") or ""))
    append_text(
        path,
        f"- [{now()}] signature `{pending_fix.get('signature')}` -> {outcome}; fix: {summary}; files: {files}\n",
    )
    compact_experience(cfg)


def prompt_for(
    cfg: dict[str, Any],
    command: BuildCommand,
    attempt: int,
    signature: str,
    excerpt: str,
    log_path: pathlib.Path,
    related_mks: list[pathlib.Path],
) -> str:
    return """# workflow_v3 Build Repair Task

Fix one failed configure/build attempt for the converted CMake subproject.

Attempt: `%d`
Failed command name: `%s`
Failed command cwd: `%s`
Failed command:
```bash
%s
```

Failure signature: `%s`
Full build log: `%s`
Build repair log: `%s`
Manual handoff file: `%s`
Experience file: `%s`
Manual experience file: `%s`

Latest error excerpt:
```text
%s
```

Manual build repair experience maintained by humans:
```markdown
%s
```

Existing compact build repair experience:
```markdown
%s
```

Nearby Makefile/Android.mk/*.mk context. Use it as the source of truth for
sources, include directories, definitions, compile flags, link libraries, and
conditions:

%s

Allowed edit scope:

- You may only create or modify `CMakeLists.txt` files.
- Do not modify source files, headers, Makefile, Android.mk, or *.mk files.
- The outer workflow only records diffs for `CMakeLists.txt`; edits elsewhere
  are forbidden even if they seem useful.

Requirements:

- Fix the current failure in `CMakeLists.txt`. If the same Makefile-to-CMake
  mistake is common in sibling or related `CMakeLists.txt` files, apply the
  same correction consistently to those `CMakeLists.txt` files too.
- Do not make unrelated rewrites or style-only churn.
- Do not run configure, build, tests, git, or shell commands.
- Do not delete files. If a fix would require deleting a file, stop and write a
  concise note to the manual handoff file instead.
- Only edit generated or existing `CMakeLists.txt` files.
- Keep behavior aligned with the nearby mk/Makefile implementation.
- Use target-scoped modern CMake commands.
- Do not use unexpanded wildcards in `add_library`, `add_executable`, or
  `target_sources`.
- Do not hide the error by removing sources, weakening conditions, or replacing
  target logic with broad global flags.

Return concise Markdown with:

- suspected root cause
- files changed
- why the change should advance the build
- remaining risk
""" % (
        attempt,
        command.name,
        str(command.cwd),
        command.command,
        signature,
        rel(log_path),
        rel(state_file(cfg, "build_repair_log", "build_repair_log.md")),
        rel(state_file(cfg, "manual_required_file", "manual_required.md")),
        rel(state_file(cfg, "build_experience_file", "build_experience.md")),
        rel(state_file(cfg, "build_manual_experience_file", "build_manual_experience.md")),
        excerpt,
        read_manual_experience_for_prompt(cfg),
        read_experience_for_prompt(cfg),
        mk_context(cfg, related_mks),
    )


def run_fixer(
    cfg: dict[str, Any],
    command: BuildCommand,
    attempt: int,
    signature: str,
    excerpt: str,
    build_log: pathlib.Path,
) -> tuple[subprocess.CompletedProcess[str], pathlib.Path, pathlib.Path, pathlib.Path, int]:
    timeout = int(cfg.get("build_fix_opencode_timeout_seconds", cfg.get("opencode_timeout_seconds", 1800)))
    retries = int(cfg.get("build_fix_opencode_retries", 1))
    stem = f"build-fix.{attempt:03d}.{sanitize_name(command.name)}.{signature}"
    prompts_dir = pathlib.Path(cfg["state_dir"]) / "prompts"
    results_dir = pathlib.Path(cfg["state_dir"]) / "opencode_results"
    logs_dir = pathlib.Path(cfg["state_dir"]) / "logs"
    related_mks = find_related_mk_files(
        cfg,
        excerpt,
        int(cfg.get("build_repair_related_mk_limit", 20)),
        source_dirs_from_command(cfg, command),
    )
    prompt_path = prompts_dir / f"{stem}.md"
    write_text(prompt_path, prompt_for(cfg, command, attempt, signature, excerpt, build_log, related_mks))

    last_cp: subprocess.CompletedProcess[str] | None = None
    last_stdout = results_dir / f"{stem}.try-0.out"
    last_stderr = logs_dir / f"{stem}.try-0.err"
    for retry_index in range(retries + 1):
        stdout_path = results_dir / f"{stem}.try-{retry_index}.out"
        stderr_path = logs_dir / f"{stem}.try-{retry_index}.err"
        cmd = [
            cfg["opencode_bin"],
            "run",
            "--agent",
            "v3-build-fixer",
            "--model",
            cfg.get("model", "deepseek/deepseek-v4-pro"),
            "--file",
            str(prompt_path),
            "--",
            "Fix this workflow_v3 build failure by editing only CMakeLists.txt files.",
        ]
        print(
            f"[fixer] handing failure to OpenCode agent=v3-build-fixer "
            f"try={retry_index}/{retries} timeout={timeout}s",
            flush=True,
        )
        print(f"[fixer] prompt: {prompt_path}", flush=True)
        print("[fixer] waiting for OpenCode result...", flush=True)
        cp = run(cmd, cfg, timeout=timeout)
        write_text(stdout_path, cp.stdout)
        write_text(stderr_path, cp.stderr)
        last_cp = cp
        last_stdout = stdout_path
        last_stderr = stderr_path
        print(
            f"[fixer] OpenCode finished returncode={cp.returncode} "
            f"stdout={stdout_path} stderr={stderr_path}",
            flush=True,
        )
        if cp.returncode == TIMEOUT_RETURNCODE and retry_index < retries:
            print(f"[fixer] timeout; retry {retry_index + 1}/{retries}", flush=True)
            continue
        return cp, prompt_path, stdout_path, stderr_path, retry_index

    assert last_cp is not None
    return last_cp, prompt_path, last_stdout, last_stderr, retries


def run_build_sequence(
    cfg: dict[str, Any],
    commands: list[BuildCommand],
    attempt: int,
) -> tuple[BuildCommand, subprocess.CompletedProcess[str], pathlib.Path] | None:
    logs_dir = pathlib.Path(cfg["state_dir"]) / "logs"
    for index, command in enumerate(commands, start=1):
        print(f"[build] attempt={attempt} command={index}/{len(commands)} name={command.name}", flush=True)
        print(f"  cwd: {command.cwd}", flush=True)
        print(f"  command: {command.command}", flush=True)
        print(f"  timeout: {command.timeout}s", flush=True)
        print("  output:", flush=True)
        cp = run_shell_command(command, cfg)
        log_path = logs_dir / f"build.{attempt:03d}.{index:02d}-{sanitize_name(command.name)}.log"
        write_text(log_path, cp.stdout)
        print(f"[build] command finished returncode={cp.returncode} log={log_path}", flush=True)
        if cp.returncode != 0:
            return command, cp, log_path
    return None


def write_manual_handoff(
    cfg: dict[str, Any],
    title: str,
    details: str,
    state: dict[str, Any],
) -> None:
    manual_path = state_file(cfg, "manual_required_file", "manual_required.md")
    write_text(
        manual_path,
        "# workflow_v3 Manual Build Repair Required\n\n"
        f"## {title}\n\n"
        f"- Time: `{now()}`\n"
        f"- State file: `{rel(state_file(cfg, 'build_repair_state_file', 'build_repair_state.json'))}`\n"
        f"- Detailed log: `{rel(state_file(cfg, 'build_repair_log', 'build_repair_log.md'))}`\n\n"
        f"{details.rstrip()}\n\n"
        "After manual edits, rerun:\n\n"
        "```bash\n"
        "workflow_v3/scripts/build_repair_loop.py <config.json>\n"
        "```\n",
    )
    state.update({"status": "manual_required", "manual_required": rel(manual_path), "updated_at": now()})
    write_json(state_file(cfg, "build_repair_state_file", "build_repair_state.json"), state)


def append_build_failure_log(
    cfg: dict[str, Any],
    attempt: int,
    command: BuildCommand,
    cp: subprocess.CompletedProcess[str],
    log_path: pathlib.Path,
    signature: str,
    same_count: int,
    excerpt: str,
) -> None:
    append_text(
        state_file(cfg, "build_repair_log", "build_repair_log.md"),
        "\n".join(
            [
                f"\n## Attempt {attempt}: build failed",
                "",
                f"- Time: `{now()}`",
                f"- Command: `{command.name}`",
                f"- Cwd: `{command.cwd}`",
                f"- Return code: `{cp.returncode}`",
                f"- Failure signature: `{signature}`",
                f"- Same signature count: `{same_count}`",
                f"- Full log: `{rel(log_path)}`",
                "",
                "### Error excerpt",
                "",
                "```text",
                excerpt.rstrip(),
                "```",
                "",
            ]
        ),
    )


def append_fix_log(
    cfg: dict[str, Any],
    attempt: int,
    fixer_cp: subprocess.CompletedProcess[str],
    prompt_path: pathlib.Path,
    stdout_path: pathlib.Path,
    stderr_path: pathlib.Path,
    retry_index: int,
    changes: list[FileChange],
) -> None:
    summary = one_line(fixer_cp.stdout, 1200)
    append_text(
        state_file(cfg, "build_repair_log", "build_repair_log.md"),
        "\n".join(
            [
                f"\n## Attempt {attempt}: fixer result",
                "",
                f"- Time: `{now()}`",
                f"- Return code: `{fixer_cp.returncode}`",
                f"- Retry index: `{retry_index}`",
                f"- Prompt: `{rel(prompt_path)}`",
                f"- Stdout: `{rel(stdout_path)}`",
                f"- Stderr: `{rel(stderr_path)}`",
                "",
                "### Fix summary",
                "",
                "```text",
                summary,
                "```",
                "",
                "### Diff",
                "",
                format_changes(changes, int(cfg.get("build_repair_diff_max_chars", 60_000))).rstrip(),
                "",
            ]
        ),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Run workflow_v3 configure/build commands and repair failures with OpenCode.")
    parser.add_argument("config")
    parser.add_argument("--limit-attempts", type=int, default=0, help="limit repair attempts for this run")
    parser.add_argument("--reset-state", action="store_true", help="reset build repair state before running")
    args = parser.parse_args()

    cfg = load_config(args.config)
    commands = configured_build_commands(cfg)
    state_path = state_file(cfg, "build_repair_state_file", "build_repair_state.json")
    if args.reset_state and state_path.exists():
        state_path.unlink()

    state = read_json(state_path)
    if state.get("status") == "manual_required":
        append_text(
            state_file(cfg, "build_repair_log", "build_repair_log.md"),
            f"\n## Resume after manual handoff\n\n- Time: `{now()}`\n\n",
        )
        state = {}

    max_attempts = int(cfg.get("max_fix_attempts", cfg.get("max_fix_attempts_per_product", 8)))
    if args.limit_attempts > 0:
        max_attempts = min(max_attempts, args.limit_attempts)
    max_same = int(cfg.get("max_same_failure", 3))
    excerpt_lines = int(cfg.get("error_excerpt_lines", 180))
    seen: dict[str, int] = dict(state.get("seen_signatures") or {})
    pending_fix = state.get("pending_fix") if isinstance(state.get("pending_fix"), dict) else None

    for attempt in range(1, max_attempts + 1):
        progress(attempt, max_attempts, "build repair")
        state.update({"status": "running", "attempt": attempt, "updated_at": now(), "seen_signatures": seen})
        write_json(state_path, state)

        failure = run_build_sequence(cfg, commands, attempt)
        if failure is None:
            if pending_fix:
                append_experience(cfg, pending_fix, "all configured build commands passed")
                pending_fix = None
            append_text(
                state_file(cfg, "build_repair_log", "build_repair_log.md"),
                f"\n## Attempt {attempt}: all build commands passed\n\n- Time: `{now()}`\n\n",
            )
            state.update({"status": "done", "updated_at": now(), "pending_fix": None})
            write_json(state_path, state)
            print("[build] all configured build commands passed", flush=True)
            return 0

        command, cp, build_log = failure
        excerpt = error_excerpt(cp.stdout, excerpt_lines)
        signature = failure_signature(cp.stdout, excerpt_lines)
        key = failure_key(cp.stdout, excerpt_lines)
        print(f"[build] failed command={command.name} signature={signature}", flush=True)
        print("[build] latest error excerpt:", flush=True)
        print(excerpt, flush=True)

        if pending_fix and pending_fix.get("signature") != signature:
            append_experience(cfg, pending_fix, f"advanced to new failure signature `{signature}`")
            pending_fix = None

        seen[signature] = int(seen.get(signature, 0)) + 1
        same_count = seen[signature]
        append_build_failure_log(cfg, attempt, command, cp, build_log, signature, same_count, excerpt)
        state.update(
            {
                "status": "failed",
                "attempt": attempt,
                "failed_command": command.name,
                "failure_signature": signature,
                "failure_key": key,
                "same_signature_count": same_count,
                "last_log": rel(build_log),
                "updated_at": now(),
                "seen_signatures": seen,
                "pending_fix": pending_fix,
            }
        )
        write_json(state_path, state)

        if same_count >= max_same:
            write_manual_handoff(
                cfg,
                "build progress stalled",
                "\n".join(
                    [
                        f"- Failed command: `{command.name}`",
                        f"- Failure signature: `{signature}` repeated `{same_count}` time(s).",
                        f"- Last build log: `{rel(build_log)}`",
                        "",
                        "Latest failure key:",
                        "",
                        "```text",
                        key,
                        "```",
                    ]
                ),
                state,
            )
            print(f"[build] stalled on signature {signature}; see {state_file(cfg, 'manual_required_file', 'manual_required.md')}", flush=True)
            return 10

        print("[snapshot] taking before snapshot for CMakeLists.txt files only...", flush=True)
        before = take_snapshot(cfg)
        print(f"[snapshot] tracked before={len(before)} CMakeLists.txt file(s)", flush=True)
        fixer_cp, prompt_path, stdout_path, stderr_path, retry_index = run_fixer(cfg, command, attempt, signature, excerpt, build_log)
        print("[snapshot] taking after snapshot for CMakeLists.txt files only...", flush=True)
        after = take_snapshot(cfg)
        print(f"[snapshot] tracked after={len(after)} CMakeLists.txt file(s)", flush=True)
        changes = diff_snapshots(before, after)
        print(f"[snapshot] detected {len(changes)} changed CMakeLists.txt file(s)", flush=True)

        deleted = [change for change in changes if change.status == "deleted"]
        if deleted:
            restored = restore_deleted_files(cfg, before, changes)
            append_fix_log(cfg, attempt, fixer_cp, prompt_path, stdout_path, stderr_path, retry_index, changes)
            write_manual_handoff(
                cfg,
                "fixer deleted files",
                "\n".join(
                    [
                        "The build fixer deleted files, which is not allowed.",
                        f"- Deleted files: `{', '.join(change.rel_path for change in deleted)}`",
                        f"- Restored files: `{', '.join(restored) if restored else 'none'}`",
                        f"- Fixer stdout: `{rel(stdout_path)}`",
                        f"- Fixer stderr: `{rel(stderr_path)}`",
                    ]
                ),
                state,
            )
            return 12

        append_fix_log(cfg, attempt, fixer_cp, prompt_path, stdout_path, stderr_path, retry_index, changes)
        if fixer_cp.returncode != 0:
            write_manual_handoff(
                cfg,
                "build fixer failed",
                "\n".join(
                    [
                        f"- Fixer return code: `{fixer_cp.returncode}`",
                        f"- Prompt: `{rel(prompt_path)}`",
                        f"- Fixer stdout: `{rel(stdout_path)}`",
                        f"- Fixer stderr: `{rel(stderr_path)}`",
                    ]
                ),
                state,
            )
            return 11

        pending_fix = {
            "signature": signature,
            "summary": one_line(fixer_cp.stdout),
            "changed_files": [change.rel_path for change in changes],
            "attempt": attempt,
            "time": now(),
        }
        state.update({"pending_fix": pending_fix, "updated_at": now()})
        write_json(state_path, state)

    write_manual_handoff(
        cfg,
        "max fix attempts exceeded",
        f"- Max attempts for this run: `{max_attempts}`\n- Last state file: `{rel(state_path)}`",
        state,
    )
    return 13


if __name__ == "__main__":
    raise SystemExit(main())
