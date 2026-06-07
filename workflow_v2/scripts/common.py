#!/usr/bin/env python3
import hashlib
import json
import os
import pathlib
import re
import signal
import subprocess
import sys
import time
from typing import Any


ROOT = pathlib.Path(__file__).resolve().parents[2]


def load_config(path: str) -> dict[str, Any]:
    config_path = pathlib.Path(path)
    if not config_path.is_absolute():
        config_path = ROOT / config_path
    with config_path.open() as f:
        cfg = json.load(f)
    cfg["_config_path"] = str(config_path)
    cfg["project_root"] = str(resolve_path(cfg.get("project_root", ".")))
    cfg["scan_subdir"] = str(resolve_path(cfg["scan_subdir"]))
    cfg["cmake_output_dir"] = str(resolve_path(cfg["cmake_output_dir"]))
    cfg["state_dir"] = str(resolve_path(cfg.get("state_dir", "workflow_v2/state")))
    cfg["opencode_bin"] = str(resolve_path(cfg.get("opencode_bin", ".tools/node_modules/.bin/opencode")))
    cfg["api_key_file"] = str(resolve_path(cfg.get("api_key_file", "mykey.txt")))
    pathlib.Path(cfg["state_dir"]).mkdir(parents=True, exist_ok=True)
    return cfg


def resolve_path(path: str) -> pathlib.Path:
    p = pathlib.Path(path)
    return p if p.is_absolute() else ROOT / p


def rel(path: pathlib.Path | str, base: pathlib.Path | None = None) -> str:
    p = pathlib.Path(path)
    base = base or ROOT
    try:
        return str(p.resolve().relative_to(base.resolve()))
    except ValueError:
        return str(p)


def read_jsonl(path: pathlib.Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def write_jsonl(path: pathlib.Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def append_jsonl(path: pathlib.Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def write_text(path: pathlib.Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def progress(index: int, total: int, label: str) -> None:
    total = max(total, 1)
    width = 28
    filled = min(width, int(width * index / total))
    bar = "#" * filled + "-" * (width - filled)
    print(f"[{index:>3}/{total:<3}] [{bar}] {label}", flush=True)


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def stable_id(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def shell_env(cfg: dict[str, Any]) -> dict[str, str]:
    env = os.environ.copy()
    key_path = pathlib.Path(cfg["api_key_file"])
    if key_path.exists():
        env["DEEPSEEK_API_KEY"] = key_path.read_text().strip()
    return env


TIMEOUT_RETURNCODE = 124


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
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            stdout, stderr = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            stdout, stderr = proc.communicate()
        timeout_msg = f"\n[TIMEOUT] command exceeded timeout_seconds={timeout}; process group was terminated.\n"
        return subprocess.CompletedProcess(
            cmd,
            TIMEOUT_RETURNCODE,
            stdout or "",
            (stderr or "") + timeout_msg,
        )


def extract_json_objects(text: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
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
    return rows


def failure_signature(text: str, lines: int) -> str:
    error_lines = []
    patterns = ("error:", "undefined reference", "No such file", "fatal error", "FAILED:", "ninja: build stopped")
    for line in text.splitlines():
        if any(p in line for p in patterns):
            error_lines.append(line.strip())
    excerpt = "\n".join(error_lines[-lines:]) or "\n".join(text.splitlines()[-lines:])
    return hashlib.sha256(excerpt.encode(errors="replace")).hexdigest()[:16]


def error_excerpt(text: str, lines: int) -> str:
    selected = []
    patterns = ("error:", "undefined reference", "No such file", "fatal error", "FAILED:", "ninja: build stopped")
    all_lines = text.splitlines()
    for idx, line in enumerate(all_lines):
        if any(p in line for p in patterns):
            start = max(0, idx - 8)
            end = min(len(all_lines), idx + 20)
            selected.extend(all_lines[start:end])
            selected.append("---")
    if selected:
        return "\n".join(selected[-lines:])
    return "\n".join(all_lines[-lines:])


def require_args(argv: list[str], usage: str) -> str:
    if len(argv) != 2:
        print(usage, file=sys.stderr)
        raise SystemExit(2)
    return argv[1]
