#!/usr/bin/env python3
import pathlib
import subprocess
import sys


ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPTS = pathlib.Path(__file__).resolve().parent


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: run_all.py <config.json>", file=sys.stderr)
        return 2
    config = sys.argv[1]
    steps = [
        "discover_mk_files.py",
        "analyze_mk_files.py",
        "convert_targets.py",
        "build_repair_loop.py",
    ]
    for step in steps:
        cmd = [str(SCRIPTS / step), config]
        print("+", " ".join(cmd))
        cp = subprocess.run(cmd, cwd=str(ROOT), check=False)
        if cp.returncode != 0:
            return cp.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

