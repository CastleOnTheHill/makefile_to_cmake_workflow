#!/usr/bin/env python3
import argparse
import collections
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
import sys
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
    terminate_process_tree,
    write_text,
)


TEXT_FILE_NAMES = {"Android.mk", "CMakeLists.txt", "Makefile"}
SNAPSHOT_FILE_NAMES = {"CMakeLists.txt"}
EXPERIENCE_HEADER = [
    "# workflow_v3 Build Repair Experience",
    "",
    "One concise lesson per line. Keep only the bug pattern and the CMake fix.",
    "",
]


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
        f.flush()
        os.fsync(f.fileno())


def read_json(path: pathlib.Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open(encoding="utf-8") as f:
            value = json.load(f)
    except json.JSONDecodeError:
        corrupt_path = path.with_name(f"{path.name}.corrupt.{time.strftime('%Y%m%d%H%M%S')}")
        try:
            path.replace(corrupt_path)
            print(f"[state] ignored corrupt state file; moved to {corrupt_path}", flush=True)
        except OSError:
            print(f"[state] ignored corrupt state file: {path}", flush=True)
        return {}
    return value if isinstance(value, dict) else {}


def write_json(path: pathlib.Path, value: dict[str, Any]) -> None:
    write_text(path, json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def process_cmdline(pid: int) -> str:
    try:
        data = pathlib.Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return ""
    return data.replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()


def process_looks_like_build_repair(pid: int) -> bool:
    cmdline = process_cmdline(pid)
    if not cmdline:
        return True
    return "build_repair_loop.py" in cmdline


class BuildRepairLock:
    def __init__(self, path: pathlib.Path):
        self.path = path
        self.acquired = False

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {"pid": os.getpid(), "started_at": now(), "cmdline": " ".join(sys.argv)},
            ensure_ascii=False,
            sort_keys=True,
        ) + "\n"
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                lock = read_json(self.path)
                pid = int(lock.get("pid") or 0)
                if pid and pid != os.getpid() and process_alive(pid) and process_looks_like_build_repair(pid):
                    raise SystemExit(f"another build repair loop is running: pid={pid}; lock={self.path}")
                stale_path = self.path.with_name(f"{self.path.name}.stale.{time.strftime('%Y%m%d%H%M%S')}")
                try:
                    self.path.replace(stale_path)
                    print(f"[lock] moved stale lock to {stale_path}", flush=True)
                except OSError:
                    self.path.unlink(missing_ok=True)
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            self.acquired = True
            print(f"[lock] acquired {self.path}", flush=True)
            return

    def release(self) -> None:
        if not self.acquired:
            return
        try:
            current = read_json(self.path)
            if int(current.get("pid") or 0) == os.getpid():
                self.path.unlink(missing_ok=True)
        finally:
            self.acquired = False

    def __enter__(self) -> "BuildRepairLock":
        self.acquire()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.release()


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


def run_shell_command(
    command: BuildCommand,
    cfg: dict[str, Any],
    log_path: pathlib.Path,
    heartbeat: Any | None = None,
) -> subprocess.CompletedProcess[str]:
    echo_output = bool(cfg.get("build_repair_echo_output", True))
    log_path.parent.mkdir(parents=True, exist_ok=True)
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
    tail_limit = max(100, int(cfg.get("build_repair_output_tail_lines", max(2000, int(cfg.get("error_excerpt_lines", 180)) * 8))))
    tail: collections.deque[str] = collections.deque(maxlen=tail_limit)
    flush_seconds = max(1, int(cfg.get("build_repair_log_flush_seconds", 5)))
    heartbeat_seconds = max(5, int(cfg.get("build_repair_heartbeat_seconds", 30)))
    start_time = time.monotonic()
    next_flush = start_time + flush_seconds
    next_heartbeat = start_time + heartbeat_seconds

    def read_output() -> None:
        assert proc.stdout is not None
        try:
            for line in proc.stdout:
                output_queue.put(line)
        except OSError:
            pass

    reader = threading.Thread(target=read_output, daemon=True)
    reader.start()
    known_descendants: list[int] = []

    def flush_log(log_file: Any) -> None:
        log_file.flush()

    def drain_output(log_file: Any) -> None:
        while True:
            try:
                line = output_queue.get_nowait()
            except queue.Empty:
                return
            tail.append(line)
            log_file.write(line)
            if echo_output:
                print(line, end="", flush=True)

    def terminate_process(sig: int) -> None:
        nonlocal known_descendants
        known_descendants = terminate_process_tree(proc, sig, known_descendants)

    def finish_after_termination(log_file: Any, timeout_msg: str, returncode: int) -> subprocess.CompletedProcess[str]:
        stop_deadline = time.monotonic() + 5
        while proc.poll() is None and time.monotonic() < stop_deadline:
            drain_output(log_file)
            time.sleep(0.1)
        if proc.poll() is None:
            terminate_process(signal.SIGKILL)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                if proc.stdout is not None:
                    try:
                        proc.stdout.close()
                    except OSError:
                        pass
        reader.join(timeout=5)
        drain_output(log_file)
        tail.append(timeout_msg)
        log_file.write(timeout_msg)
        flush_log(log_file)
        if echo_output:
            print(timeout_msg, end="", flush=True)
        return subprocess.CompletedProcess(command.command, returncode, "".join(tail), "")

    deadline = time.monotonic() + command.timeout
    with log_path.open("w", encoding="utf-8", errors="replace") as log_file:
        try:
            while proc.poll() is None:
                drain_output(log_file)
                current = time.monotonic()
                if current >= next_flush:
                    flush_log(log_file)
                    next_flush = current + flush_seconds
                if current >= next_heartbeat:
                    elapsed = int(current - start_time)
                    print(f"[build] still running name={command.name} elapsed={elapsed}s log={log_path}", flush=True)
                    if heartbeat:
                        heartbeat(elapsed)
                    next_heartbeat = current + heartbeat_seconds
                if current > deadline:
                    terminate_process(signal.SIGTERM)
                    timeout_msg = (
                        f"\n[TIMEOUT] build command exceeded timeout_seconds={command.timeout}; "
                        "process group was terminated.\n"
                    )
                    return finish_after_termination(log_file, timeout_msg, TIMEOUT_RETURNCODE)
                time.sleep(0.1)
        except BaseException:
            terminate_process(signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                terminate_process(signal.SIGKILL)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
            if proc.stdout is not None:
                try:
                    proc.stdout.close()
                except OSError:
                    pass
            reader.join(timeout=5)
            drain_output(log_file)
            flush_log(log_file)
            raise

        reader.join(timeout=5)
        drain_output(log_file)
        flush_log(log_file)
    return subprocess.CompletedProcess(command.command, proc.returncode, "".join(tail), "")


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

    if not related and scan_root.exists() and bool(cfg.get("build_repair_fallback_scan_mk", False)):
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
    lines = [line for line in path.read_text(encoding="utf-8", errors="replace").splitlines() if line.startswith("- ")]
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
    bullets = normalize_experience_lines(path.read_text(encoding="utf-8", errors="replace").splitlines())
    merged: list[str] = []
    for bullet in bullets:
        merge_experience_line(merged, bullet)
    kept = merged[-max(20, max_lines - len(EXPERIENCE_HEADER)) :]
    write_text(path, "\n".join(EXPERIENCE_HEADER + kept).rstrip() + "\n")


def one_line(text: str, limit: int = 320) -> str:
    compact = re.sub(r"\s+", " ", text).strip()
    return compact[:limit] + ("..." if len(compact) > limit else "")


def clean_lesson_text(text: str, limit: int = 180) -> str:
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"\[[^\]]+\]\s*", "", text)
    text = re.sub(r"\bsignature\s+[0-9a-f]{8,}\b", "", text, flags=re.I)
    text = re.sub(r"\bsignature\s*`?[0-9a-f]{8,}`?", "", text, flags=re.I)
    text = re.sub(r"\bfiles?:\s*[^;。]*", "", text, flags=re.I)
    text = re.sub(r"\bremaining risk:\s*.*", "", text, flags=re.I)
    text = re.sub(r"\s+", " ", text).strip(" -;,.。")
    return text[:limit].rstrip(" -;,.。") + ("..." if len(text) > limit else "")


def labeled_value(text: str, labels: list[str]) -> str:
    label_pattern = "|".join(re.escape(label) for label in labels)
    all_labels = (
        "suspected root cause|root cause|cause|fix|solution|why this advances the build|"
        "files changed|remaining risk|问题|原因|修复|方案|风险"
    )
    boundary = rf"(?:\n\s*(?:[-*]\s*)?|\s+[-*]\s+|\s*[;。]\s*)(?:{all_labels})\s*[:：]"
    pattern = re.compile(rf"(?:^|[-*\n]\s*)({label_pattern})\s*[:：]\s*(.*?)(?={boundary}|\Z)", re.I | re.S)
    match = pattern.search(text)
    return clean_lesson_text(match.group(2)) if match else ""


def experience_lesson(pending_fix: dict[str, Any], outcome: str) -> str:
    summary = str(pending_fix.get("summary") or "")
    cause = labeled_value(summary, ["suspected root cause", "root cause", "cause", "问题", "原因"])
    fix = labeled_value(summary, ["fix", "solution", "why this advances the build", "修复", "方案"])
    if not cause:
        cause = clean_lesson_text(summary)
    if not fix and outcome.startswith("advanced to new failure signature"):
        fix = "按该修复后构建推进到下一类错误"
    if not fix and "all configured build commands passed" in outcome:
        fix = "按该修复后配置的构建命令通过"
    if fix:
        lesson = f"{cause}; fix: {fix}"
    else:
        lesson = cause
    lesson = clean_lesson_text(lesson, 260)
    return f"- {lesson}" if lesson else ""


def normalize_experience_lines(lines: list[str]) -> list[str]:
    bullets: list[str] = []
    for raw in lines:
        line = raw.strip()
        if not line.startswith("- "):
            continue
        if "signature `" in line or line.startswith("- ["):
            match = re.search(r"fix:\s*(.*?)(?:;\s*files:|$)", line)
            line = f"- {clean_lesson_text(match.group(1) if match else line)}"
        else:
            line = f"- {clean_lesson_text(line[2:])}"
        if len(line) > 3:
            bullets.append(line)
    return bullets


def experience_tokens(line: str) -> set[str]:
    normalized = re.sub(r"[^A-Za-z0-9_\u4e00-\u9fff]+", " ", line.lower())
    tokens = {token for token in normalized.split() if len(token) > 1}
    return tokens - {"fix", "cmake", "cmakelists", "txt", "the", "and", "for", "with", "that", "this"}


def experience_similarity(left: str, right: str) -> float:
    left_tokens = experience_tokens(left)
    right_tokens = experience_tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


def similar_experience(left: str, right: str) -> bool:
    left_tokens = experience_tokens(left)
    right_tokens = experience_tokens(right)
    if not left_tokens or not right_tokens:
        return False
    return experience_similarity(left, right) >= 0.35 or len(left_tokens & right_tokens) >= 3


def better_experience_line(old: str, new: str) -> str:
    if len(new) < len(old) * 0.75:
        return new
    old_has_fix = "; fix:" in old
    new_has_fix = "; fix:" in new
    if new_has_fix and not old_has_fix:
        return new
    return old if len(old) <= len(new) else new


def merge_experience_line(lines: list[str], new_line: str) -> None:
    for index, old_line in enumerate(lines):
        if similar_experience(old_line, new_line):
            lines[index] = better_experience_line(old_line, new_line)
            return
    lines.append(new_line)


def append_experience(cfg: dict[str, Any], pending_fix: dict[str, Any], outcome: str) -> None:
    path = state_file(cfg, "build_experience_file", "build_experience.md")
    lines = normalize_experience_lines(path.read_text(encoding="utf-8", errors="replace").splitlines()) if path.exists() else []
    lesson = experience_lesson(pending_fix, outcome)
    if len(lesson) > 3:
        merge_experience_line(lines, lesson)
    write_text(path, "\n".join(EXPERIENCE_HEADER + lines).rstrip() + "\n")
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
    state: dict[str, Any],
    state_path: pathlib.Path,
    run_id: str,
) -> tuple[BuildCommand, subprocess.CompletedProcess[str], pathlib.Path] | None:
    logs_dir = pathlib.Path(cfg["state_dir"]) / "logs"
    for index, command in enumerate(commands, start=1):
        log_path = logs_dir / f"build.{attempt:03d}.{index:02d}-{sanitize_name(command.name)}.log"
        state.update(
            {
                "status": "running",
                "phase": "build_command",
                "run_id": run_id,
                "attempt": attempt,
                "command_index": index,
                "command_count": len(commands),
                "current_command": command.name,
                "current_log": rel(log_path),
                "updated_at": now(),
            }
        )
        write_json(state_path, state)
        print(f"[build] attempt={attempt} command={index}/{len(commands)} name={command.name}", flush=True)
        print(f"  cwd: {command.cwd}", flush=True)
        print(f"  command: {command.command}", flush=True)
        print(f"  timeout: {command.timeout}s", flush=True)
        print(f"  log: {log_path}", flush=True)
        print("  output:", flush=True)

        def heartbeat(elapsed: int) -> None:
            state.update({"heartbeat_at": now(), "elapsed_seconds": elapsed, "updated_at": now()})
            write_json(state_path, state)

        cp = run_shell_command(command, cfg, log_path, heartbeat)
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


def max_recorded_attempt(cfg: dict[str, Any]) -> int:
    pattern = re.compile(r"(?:build|build-fix)\.(\d+)\.")
    max_attempt = 0
    for directory_name in ("logs", "prompts", "opencode_results"):
        directory = pathlib.Path(cfg["state_dir"]) / directory_name
        if not directory.exists():
            continue
        for path in directory.iterdir():
            match = pattern.search(path.name)
            if match:
                max_attempt = max(max_attempt, int(match.group(1)))
    return max_attempt


def install_shutdown_signal_handlers() -> None:
    def request_shutdown(signum: int, _frame: Any) -> None:
        raise KeyboardInterrupt(f"received signal {signum}")

    signal.signal(signal.SIGINT, request_shutdown)
    signal.signal(signal.SIGTERM, request_shutdown)


def append_resume_log(cfg: dict[str, Any], title: str, state: dict[str, Any]) -> None:
    append_text(
        state_file(cfg, "build_repair_log", "build_repair_log.md"),
        "\n".join(
            [
                f"\n## {title}",
                "",
                f"- Time: `{now()}`",
                f"- Previous status: `{state.get('status', 'unknown')}`",
                f"- Previous phase: `{state.get('phase', 'unknown')}`",
                f"- Previous attempt: `{state.get('attempt', 'unknown')}`",
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

    install_shutdown_signal_handlers()
    cfg = load_config(args.config)
    lock_path = state_file(cfg, "build_repair_lock_file", "build_repair.lock")
    state_path = state_file(cfg, "build_repair_state_file", "build_repair_state.json")
    run_id = f"{int(time.time())}-{os.getpid()}"
    state: dict[str, Any] = {}

    with BuildRepairLock(lock_path):
        commands = configured_build_commands(cfg)
        if args.reset_state and state_path.exists():
            state_path.unlink()

        state = read_json(state_path)
        previous_attempt = int(state.get("attempt") or 0)
        if state.get("status") == "manual_required":
            append_resume_log(cfg, "Resume after manual handoff", state)
            state = {"attempt": previous_attempt}
        elif state.get("status") in {"running", "failed", "fixing", "interrupted"}:
            append_resume_log(cfg, "Resume after interrupted build repair run", state)
        elif state.get("status") == "done":
            append_resume_log(cfg, "Recheck after completed build repair run", state)
            state = {"attempt": previous_attempt}

        max_attempts = int(cfg.get("max_fix_attempts", cfg.get("max_fix_attempts_per_product", 8)))
        run_budget = args.limit_attempts if args.limit_attempts > 0 else max_attempts
        max_same = int(cfg.get("max_same_failure", 3))
        excerpt_lines = int(cfg.get("error_excerpt_lines", 180))
        recorded_attempt = max_recorded_attempt(cfg)
        start_attempt = max(int(state.get("attempt") or 0), recorded_attempt) + 1
        seen: dict[str, int] = dict(state.get("seen_signatures") or {})
        pending_fix = state.get("pending_fix") if isinstance(state.get("pending_fix"), dict) else None

        try:
            for run_index, attempt in enumerate(range(start_attempt, start_attempt + run_budget), start=1):
                progress(run_index, run_budget, f"build repair attempt={attempt}")
                state.update(
                    {
                        "status": "running",
                        "phase": "attempt_start",
                        "run_id": run_id,
                        "pid": os.getpid(),
                        "attempt": attempt,
                        "run_attempt_index": run_index,
                        "run_attempt_budget": run_budget,
                        "updated_at": now(),
                        "seen_signatures": seen,
                    }
                )
                write_json(state_path, state)

                failure = run_build_sequence(cfg, commands, attempt, state, state_path, run_id)
                if failure is None:
                    if pending_fix:
                        append_experience(cfg, pending_fix, "all configured build commands passed")
                        pending_fix = None
                    append_text(
                        state_file(cfg, "build_repair_log", "build_repair_log.md"),
                        f"\n## Attempt {attempt}: all build commands passed\n\n- Time: `{now()}`\n\n",
                    )
                    state.update(
                        {
                            "status": "done",
                            "phase": "done",
                            "updated_at": now(),
                            "pending_fix": None,
                            "current_command": None,
                        }
                    )
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
                        "phase": "build_failed",
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
                    print(
                        f"[build] stalled on signature {signature}; see {state_file(cfg, 'manual_required_file', 'manual_required.md')}",
                        flush=True,
                    )
                    return 10

                print("[snapshot] taking before snapshot for CMakeLists.txt files only...", flush=True)
                state.update({"status": "running", "phase": "snapshot_before", "updated_at": now()})
                write_json(state_path, state)
                before = take_snapshot(cfg)
                print(f"[snapshot] tracked before={len(before)} CMakeLists.txt file(s)", flush=True)
                state.update({"status": "fixing", "phase": "opencode_fix", "updated_at": now()})
                write_json(state_path, state)
                fixer_cp, prompt_path, stdout_path, stderr_path, retry_index = run_fixer(
                    cfg, command, attempt, signature, excerpt, build_log
                )
                print("[snapshot] taking after snapshot for CMakeLists.txt files only...", flush=True)
                state.update(
                    {
                        "status": "running",
                        "phase": "snapshot_after",
                        "fixer_returncode": fixer_cp.returncode,
                        "fixer_stdout": rel(stdout_path),
                        "fixer_stderr": rel(stderr_path),
                        "updated_at": now(),
                    }
                )
                write_json(state_path, state)
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
                state.update(
                    {
                        "status": "running",
                        "phase": "fix_applied",
                        "pending_fix": pending_fix,
                        "updated_at": now(),
                    }
                )
                write_json(state_path, state)

            write_manual_handoff(
                cfg,
                "max fix attempts exceeded",
                f"- Max attempts for this run: `{run_budget}`\n- Last state file: `{rel(state_path)}`",
                state,
            )
            return 13
        except KeyboardInterrupt as exc:
            state.update(
                {
                    "status": "interrupted",
                    "phase": state.get("phase") or "unknown",
                    "run_id": run_id,
                    "pid": os.getpid(),
                    "updated_at": now(),
                    "interrupt_reason": str(exc) or "KeyboardInterrupt",
                }
            )
            write_json(state_path, state)
            append_text(
                state_file(cfg, "build_repair_log", "build_repair_log.md"),
                f"\n## Interrupted\n\n- Time: `{now()}`\n- Attempt: `{state.get('attempt')}`\n- Phase: `{state.get('phase')}`\n\n",
            )
            print(f"[build] interrupted; state saved to {state_path}", flush=True)
            return 130


if __name__ == "__main__":
    raise SystemExit(main())
