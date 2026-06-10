#!/usr/bin/env python3
import argparse
import json
import pathlib
import subprocess
import tempfile


ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPTS = pathlib.Path(__file__).resolve().parent


def load_config(path: str) -> dict:
    config_path = pathlib.Path(path)
    if not config_path.is_absolute():
        config_path = ROOT / config_path
    with config_path.open(encoding="utf-8") as f:
        cfg = json.load(f)
    cfg["_source_config"] = str(config_path)
    return cfg


def write_fake_config(cfg: dict) -> pathlib.Path:
    fake_cfg = dict(cfg)
    fake_cfg.pop("_source_config", None)
    fake_cfg["opencode_bin"] = str(SCRIPTS / "fake_opencode.py")
    state_dir = pathlib.Path(fake_cfg.get("state_dir", "workflow_v3/state"))
    if not state_dir.is_absolute():
        state_dir = ROOT / state_dir
    state_dir.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        delete=False,
        dir=str(state_dir),
        prefix="fake_config.",
        suffix=".json",
    )
    with tmp:
        json.dump(fake_cfg, tmp, ensure_ascii=False, indent=2)
        tmp.write("\n")
    return pathlib.Path(tmp.name)


def run_step(step: str, config: pathlib.Path, args: list[str]) -> int:
    cmd = [str(SCRIPTS / step), str(config)] + args
    print("+", " ".join(cmd), flush=True)
    cp = subprocess.run(cmd, cwd=str(ROOT), check=False)
    return cp.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description="Run workflow_v3 with fake OpenCode for smoke tests.")
    parser.add_argument("config")
    parser.add_argument("--limit", type=int, default=0, help="maximum rows per stage")
    parser.add_argument("-j", "--jobs", type=int, default=1, help="parallel jobs for fake analyze/convert")
    parser.add_argument("--timeout", type=int, default=60, help="per-fake-OpenCode timeout seconds")
    parser.add_argument(
        "--stage",
        choices=["all", "discover", "analyze", "convert", "build"],
        default="all",
        help="run only one stage or the discover/analyze/convert fake workflow",
    )
    parser.add_argument("--force-analyze", action="store_true", help="force rerun fake analysis")
    parser.add_argument("--force-convert", action="store_true", help="force rerun fake conversion")
    parser.add_argument("--mk-file", action="append", default=[], help="limit analyze/convert to one mk/makefile path; repeatable")
    parser.add_argument("--file-list", "--mk-list", action="append", default=[], help="file list for analyze/convert; repeatable")
    args = parser.parse_args()
    if args.jobs < 1:
        raise SystemExit("--jobs must be >= 1")

    fake_config = write_fake_config(load_config(args.config))
    try:
        common = []
        if args.limit > 0:
            common.extend(["--limit", str(args.limit)])

        analyze_extra = ["--jobs", str(args.jobs), "--timeout", str(args.timeout)]
        convert_extra = ["--jobs", str(args.jobs), "--timeout", str(args.timeout)]
        if args.force_analyze:
            analyze_extra.append("--force")
        if args.force_convert:
            convert_extra.append("--force")
        for mk_file in args.mk_file:
            analyze_extra.extend(["--mk-file", mk_file])
            convert_extra.extend(["--mk-file", mk_file])
        for file_list in args.file_list:
            analyze_extra.extend(["--file-list", file_list])
            convert_extra.extend(["--file-list", file_list])

        steps: list[tuple[str, list[str]]] = []
        if args.stage in {"all", "discover"}:
            steps.append(("discover_mk_files.py", common))
        if args.stage in {"all", "analyze"}:
            steps.append(("analyze_mk_files.py", common + analyze_extra))
        if args.stage in {"all", "convert"}:
            steps.append(("convert_targets.py", common + convert_extra))
        if args.stage == "build":
            steps.append(("build_repair_loop.py", []))

        for step, step_args in steps:
            code = run_step(step, fake_config, step_args)
            if code != 0:
                return code
        return 0
    finally:
        fake_config.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
