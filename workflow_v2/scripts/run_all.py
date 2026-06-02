#!/usr/bin/env python3
import argparse
import pathlib
import subprocess


ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPTS = pathlib.Path(__file__).resolve().parent


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("config")
    parser.add_argument("--limit", type=int, default=0, help="maximum items per stage")
    parser.add_argument("-j", "--jobs", type=int, default=1, help="parallel conversion jobs for convert_targets.py")
    args = parser.parse_args()
    steps = [
        "discover_mk_files.py",
        "analyze_mk_files.py",
        "convert_targets.py",
        "build_repair_loop.py",
    ]
    for step in steps:
        cmd = [str(SCRIPTS / step), args.config]
        if args.limit > 0:
            cmd.extend(["--limit", str(args.limit)])
        if step == "convert_targets.py":
            cmd.extend(["--jobs", str(args.jobs)])
        print("+", " ".join(cmd))
        cp = subprocess.run(cmd, cwd=str(ROOT), check=False)
        if cp.returncode != 0:
            return cp.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
