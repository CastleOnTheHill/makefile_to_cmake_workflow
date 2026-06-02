#!/usr/bin/env python3
import argparse
import fnmatch
import pathlib

from common import load_config, progress, rel, stable_id, write_jsonl, write_text


def ignored(path: pathlib.Path, root: pathlib.Path, patterns: list[str]) -> bool:
    r = rel(path, root)
    return any(fnmatch.fnmatch(r, pattern) or fnmatch.fnmatch(path.name, pattern) for pattern in patterns)


def matches_file(path: pathlib.Path, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(path.name, pattern) for pattern in patterns)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("config")
    parser.add_argument("--limit", type=int, default=0, help="maximum number of mk files to discover")
    args = parser.parse_args()
    cfg = load_config(args.config)
    root = pathlib.Path(cfg["project_root"])
    scan = pathlib.Path(cfg["scan_subdir"])
    state = pathlib.Path(cfg["state_dir"])
    patterns = cfg.get("mk_file_patterns", ["Android.mk", "*.mk", "Makefile"])
    ignore_dirs = cfg.get("ignore_dirs", [])

    rows = []
    candidates = sorted(scan.rglob("*"))
    for idx, path in enumerate(candidates, start=1):
        if idx == 1 or idx == len(candidates) or idx % 100 == 0:
            progress(idx, len(candidates), f"scan {rel(path, root)}")
        if not path.is_file():
            continue
        if any(ignored(parent, root, ignore_dirs) for parent in path.parents):
            continue
        if not matches_file(path, patterns):
            continue
        rows.append(
            {
                "schema_version": 1,
                "mk_id": stable_id(rel(path, root)),
                "path": rel(path, root),
                "status": "pending",
            }
        )
        if args.limit > 0 and len(rows) >= args.limit:
            print(f"limit reached: {args.limit}")
            break

    write_jsonl(state / "mk_files.jsonl", rows)
    write_text(
        state / "status.md",
        "# Workflow V2 Status\n\n"
        f"Discovered {len(rows)} Makefile/Android.mk/*.mk file(s).\n\n"
        + "\n".join(f"- [ ] `{row['path']}`" for row in rows)
        + "\n",
    )
    print(f"discovered {len(rows)} mk file(s): {state / 'mk_files.jsonl'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
