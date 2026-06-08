#!/usr/bin/env python3
import argparse
import pathlib
import subprocess


ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPTS = pathlib.Path(__file__).resolve().parent


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("config")
    parser.add_argument("--limit", type=int, default=0, help="maximum rows per stage")
    parser.add_argument("-j", "--jobs", type=int, default=1, help="parallel jobs for analyze and convert")
    parser.add_argument("--timeout", type=int, default=0, help="per-OpenCode timeout seconds for analyze and convert")
    parser.add_argument("--force-analyze", action="store_true", help="force rerun analysis")
    parser.add_argument("--force-convert", action="store_true", help="force rerun conversion")
    args = parser.parse_args()

    steps = [
        ("discover_mk_files.py", []),
        ("analyze_mk_files.py", ["--jobs", str(args.jobs)]),
        ("convert_targets.py", ["--jobs", str(args.jobs)]),
    ]
    for step, extra in steps:
        cmd = [str(SCRIPTS / step), args.config]
        if args.limit > 0:
            cmd.extend(["--limit", str(args.limit)])
        cmd.extend(extra)
        if step in {"analyze_mk_files.py", "convert_targets.py"} and args.timeout > 0:
            cmd.extend(["--timeout", str(args.timeout)])
        if step == "analyze_mk_files.py" and args.force_analyze:
            cmd.append("--force")
        if step == "convert_targets.py" and args.force_convert:
            cmd.append("--force")
        print("+", " ".join(cmd), flush=True)
        cp = subprocess.run(cmd, cwd=str(ROOT), check=False)
        if cp.returncode != 0:
            return cp.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
