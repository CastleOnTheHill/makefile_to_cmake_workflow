#!/usr/bin/env python3
import fnmatch
import pathlib
import sys

from common import load_config, rel, stable_id, write_jsonl, write_text, require_args


def ignored(path: pathlib.Path, root: pathlib.Path, patterns: list[str]) -> bool:
    r = rel(path, root)
    return any(fnmatch.fnmatch(r, pattern) or fnmatch.fnmatch(path.name, pattern) for pattern in patterns)


def matches_file(path: pathlib.Path, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(path.name, pattern) for pattern in patterns)


def main() -> int:
    cfg = load_config(require_args(sys.argv, "usage: discover_mk_files.py <config.json>"))
    root = pathlib.Path(cfg["project_root"])
    scan = pathlib.Path(cfg["scan_subdir"])
    state = pathlib.Path(cfg["state_dir"])
    patterns = cfg.get("mk_file_patterns", ["Android.mk", "*.mk", "Makefile"])
    ignore_dirs = cfg.get("ignore_dirs", [])

    rows = []
    for path in sorted(scan.rglob("*")):
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

    write_jsonl(state / "mk_files.jsonl", rows)
    write_text(
        state / "status.md",
        "# Workflow V2 Status\n\n"
        f"Discovered {len(rows)} Makefile/Android.mk/*.mk file(s).\n\n"
        + "\n".join(f"- [ ] `{row['path']}`" for row in rows)
        + "\n",
    )
    print(state / "mk_files.jsonl")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

