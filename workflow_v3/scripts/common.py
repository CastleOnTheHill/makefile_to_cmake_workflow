#!/usr/bin/env python3
import hashlib
import json
import os
import pathlib
import re
import signal
import subprocess
import time
from typing import Any


ROOT = pathlib.Path(__file__).resolve().parents[2]
TIMEOUT_RETURNCODE = 124


def resolve_path(path: str | pathlib.Path) -> pathlib.Path:
    p = pathlib.Path(path)
    return p if p.is_absolute() else ROOT / p


def load_config(path: str) -> dict[str, Any]:
    config_path = resolve_path(path)
    with config_path.open() as f:
        cfg = json.load(f)

    cfg["_config_path"] = str(config_path)
    cfg["project_root"] = str(resolve_path(cfg.get("project_root", ".")))
    cfg["scan_subdir"] = str(resolve_path(cfg["scan_subdir"]))
    cfg["state_dir"] = str(resolve_path(cfg.get("state_dir", "workflow_v3/state")))
    cfg["board_path"] = str(resolve_path(cfg.get("board_path", "workflow_v3/state/tasks.xlsx")))
    cfg["opencode_bin"] = str(resolve_path(cfg.get("opencode_bin", ".tools/node_modules/.bin/opencode")))
    cfg["api_key_file"] = str(resolve_path(cfg.get("api_key_file", "mykey.txt")))
    cfg.setdefault("model", "deepseek/deepseek-v4-pro")
    cfg.setdefault("mk_file_patterns", ["Android.mk", "*.mk", "Makefile"])
    cfg.setdefault("ignore_dirs", [".git", ".tools", ".opencode/node_modules"])
    pathlib.Path(cfg["state_dir"]).mkdir(parents=True, exist_ok=True)
    pathlib.Path(cfg["board_path"]).parent.mkdir(parents=True, exist_ok=True)
    return cfg


def rel(path: pathlib.Path | str, base: pathlib.Path | str | None = None) -> str:
    p = pathlib.Path(path)
    b = pathlib.Path(base) if base is not None else ROOT
    try:
        return str(p.resolve().relative_to(b.resolve()))
    except ValueError:
        return str(p)


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def stable_id(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def progress(index: int, total: int, label: str) -> None:
    total = max(total, 1)
    width = 28
    filled = min(width, int(width * index / total))
    bar = "#" * filled + "-" * (width - filled)
    percent = int(index * 100 / total)
    print(f"[{index:>5}/{total:<5}] [{bar}] {percent:>3}% {label}", flush=True)


def write_text(path: pathlib.Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def read_jsonl(path: pathlib.Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    with path.open(encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line:
                continue
            value = json.loads(line)
            if isinstance(value, dict):
                rows.append(value)
    return rows


def write_jsonl(path: pathlib.Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def shell_env(cfg: dict[str, Any]) -> dict[str, str]:
    env = os.environ.copy()
    key_path = pathlib.Path(cfg["api_key_file"])
    if key_path.exists():
        key = key_path.read_text(encoding="utf-8").strip()
        if key:
            env["DEEPSEEK_API_KEY"] = key
    return env


def text_from_timeout(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def timeout_output(exc: subprocess.TimeoutExpired) -> tuple[str, str]:
    return text_from_timeout(getattr(exc, "stdout", None) or getattr(exc, "output", None)), text_from_timeout(
        getattr(exc, "stderr", None)
    )


def terminate_process_group(proc: subprocess.Popen[str], sig: int) -> None:
    try:
        os.killpg(proc.pid, sig)
    except ProcessLookupError:
        pass


def descendant_pids(root_pid: int) -> list[int]:
    children_by_parent: dict[int, list[int]] = {}
    proc_root = pathlib.Path("/proc")
    if not proc_root.exists():
        return []
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            status = (entry / "status").read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        pid = None
        ppid = None
        for line in status.splitlines():
            if line.startswith("Pid:"):
                pid = int(line.split()[1])
            elif line.startswith("PPid:"):
                ppid = int(line.split()[1])
            if pid is not None and ppid is not None:
                break
        if pid is None or ppid is None:
            continue
        children_by_parent.setdefault(ppid, []).append(pid)

    descendants: list[int] = []
    stack = list(children_by_parent.get(root_pid, []))
    while stack:
        pid = stack.pop()
        descendants.append(pid)
        stack.extend(children_by_parent.get(pid, []))
    return descendants


def terminate_process_tree(proc: subprocess.Popen[str], sig: int, known_descendants: list[int] | None = None) -> list[int]:
    descendants = set(known_descendants or [])
    descendants.update(descendant_pids(proc.pid))
    terminate_process_group(proc, sig)
    for pid in sorted(descendants, reverse=True):
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            pass
        except PermissionError:
            pass
    return sorted(descendants)


def close_process_pipes(proc: subprocess.Popen[str]) -> None:
    for pipe in (proc.stdin, proc.stdout, proc.stderr):
        if pipe is None:
            continue
        try:
            pipe.close()
        except OSError:
            pass


def run(
    cmd: list[str],
    cfg: dict[str, Any],
    *,
    cwd: pathlib.Path | None = None,
    stdin: str | None = None,
    timeout: int | float | None = None,
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd or ROOT),
        env=shell_env(cfg),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.PIPE if stdin is not None else None,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(input=stdin, timeout=timeout)
        return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)
    except subprocess.TimeoutExpired as exc:
        stdout, stderr = timeout_output(exc)
        descendants = terminate_process_tree(proc, signal.SIGTERM)
        try:
            stdout, stderr = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired as term_exc:
            term_stdout, term_stderr = timeout_output(term_exc)
            stdout = term_stdout or stdout
            stderr = term_stderr or stderr
            terminate_process_tree(proc, signal.SIGKILL, descendants)
            try:
                stdout, stderr = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired as kill_exc:
                kill_stdout, kill_stderr = timeout_output(kill_exc)
                stdout = kill_stdout or stdout
                stderr = kill_stderr or stderr
                close_process_pipes(proc)
                try:
                    proc.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    pass
        timeout_msg = f"\n[TIMEOUT] command exceeded timeout_seconds={timeout}; process group was terminated.\n"
        return subprocess.CompletedProcess(
            cmd,
            TIMEOUT_RETURNCODE,
            stdout or "",
            (stderr or "") + timeout_msg,
        )


def extract_json_objects(text: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    stripped = text.strip()
    if stripped:
        try:
            value = json.loads(stripped)
            if isinstance(value, dict):
                return [value]
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        except json.JSONDecodeError:
            pass

    for raw in text.splitlines():
        line = raw.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    if rows:
        return rows

    match = re.search(r"```(?:json|jsonl)?\s*(.*?)```", text, flags=re.S)
    if match:
        return extract_json_objects(match.group(1))
    return []


def failure_key(text: str, lines: int = 120) -> str:
    patterns = (
        "FAILED:",
        "error:",
        "fatal error:",
        "undefined reference",
        "No such file",
        "No rule to make target",
        "CMake Error",
        "collect2: error",
        "ld:",
        "ninja: build stopped",
    )
    all_lines = text.splitlines()
    selected: list[str] = []
    for index, line in enumerate(all_lines):
        if any(pattern in line for pattern in patterns):
            end = min(len(all_lines), index + 12)
            selected.extend(item.strip() for item in all_lines[index:end])
            selected.append("---")
    if not selected:
        selected = [line.strip() for line in all_lines[-lines:]]
    return "\n".join(selected[-lines:])


def failure_signature(text: str, lines: int = 120) -> str:
    return hashlib.sha256(failure_key(text, lines).encode(errors="replace")).hexdigest()[:16]


def error_excerpt(text: str, lines: int = 180) -> str:
    patterns = (
        "FAILED:",
        "error:",
        "fatal error:",
        "undefined reference",
        "No such file",
        "No rule to make target",
        "CMake Error",
        "collect2: error",
        "ld:",
        "ninja: build stopped",
    )
    all_lines = text.splitlines()
    selected: list[str] = []
    for index, line in enumerate(all_lines):
        if any(pattern in line for pattern in patterns):
            start = max(0, index - 10)
            end = min(len(all_lines), index + 24)
            selected.extend(all_lines[start:end])
            selected.append("---")
    if selected:
        return "\n".join(selected[-lines:])
    return "\n".join(all_lines[-lines:])


def cmake_path_for_mk(cfg: dict[str, Any], mk_rel_path: str) -> pathlib.Path:
    mk_path = pathlib.Path(mk_rel_path)
    if not mk_path.is_absolute():
        mk_path = pathlib.Path(cfg["project_root"]) / mk_path
    return mk_path.parent / "CMakeLists.txt"


def analysis_result_path(cfg: dict[str, Any], task_id: str) -> pathlib.Path:
    return pathlib.Path(cfg["state_dir"]) / "analysis" / f"{task_id}.jsonl"


def log_path(cfg: dict[str, Any], stem: str, suffix: str) -> pathlib.Path:
    return pathlib.Path(cfg["state_dir"]) / "logs" / f"{stem}.{suffix}"


def prompt_path(cfg: dict[str, Any], stem: str, suffix: str) -> pathlib.Path:
    return pathlib.Path(cfg["state_dir"]) / "prompts" / f"{stem}.{suffix}.md"
