#!/usr/bin/env python3
import argparse
import fnmatch
import pathlib

from common import cmake_path_for_mk, load_config, progress, rel, stable_id
from excel_board import (
    COL_ANALYSIS_DONE,
    COL_ANALYSIS_OK,
    COL_ANALYSIS_STATUS,
    COL_CMAKE_PATH,
    COL_CONVERSION_DONE,
    COL_CONVERSION_OK,
    COL_CONVERSION_STATUS,
    COL_MK_PATH,
    COL_TASK_ID,
    DEFAULT_COLUMNS,
    mutate_board,
    row_by_path,
)


def ignored(path: pathlib.Path, root: pathlib.Path, patterns: list[str]) -> bool:
    rel_path = rel(path, root)
    return any(fnmatch.fnmatch(rel_path, pattern) or fnmatch.fnmatch(path.name, pattern) for pattern in patterns)


def matches_file(path: pathlib.Path, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(path.name, pattern) for pattern in patterns)


def discover_rows(cfg: dict, limit: int) -> list[dict[str, str]]:
    root = pathlib.Path(cfg["project_root"])
    scan = pathlib.Path(cfg["scan_subdir"])
    patterns = cfg.get("mk_file_patterns", ["Android.mk", "*.mk", "Makefile"])
    ignore_dirs = cfg.get("ignore_dirs", [])
    candidates = sorted(scan.rglob("*"))
    rows: list[dict[str, str]] = []

    for index, path in enumerate(candidates, start=1):
        if index == 1 or index == len(candidates) or index % 200 == 0:
            progress(index, len(candidates), f"scan {rel(path, root)}")
        if not path.is_file():
            continue
        if any(ignored(parent, root, ignore_dirs) for parent in path.parents):
            continue
        if not matches_file(path, patterns):
            continue

        mk_rel = rel(path, root)
        rows.append(
            {
                COL_TASK_ID: stable_id(mk_rel),
                COL_MK_PATH: mk_rel,
                COL_CMAKE_PATH: rel(cmake_path_for_mk(cfg, mk_rel), root),
            }
        )
        if limit > 0 and len(rows) >= limit:
            print(f"limit reached: {limit}", flush=True)
            break
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("config")
    parser.add_argument("--limit", type=int, default=0, help="maximum number of mk/makefile rows to discover")
    args = parser.parse_args()

    cfg = load_config(args.config)
    board_path = pathlib.Path(cfg["board_path"])
    discovered = discover_rows(cfg, args.limit)

    def update_board(headers: list[str], rows: list[dict[str, str]]):
        existing = row_by_path(rows)
        next_rows = []
        for discovered_row in discovered:
            mk_path = discovered_row[COL_MK_PATH]
            row = dict(existing.get(mk_path, {}))
            row.update(discovered_row)
            if not row.get(COL_ANALYSIS_DONE):
                row[COL_ANALYSIS_DONE] = "否"
            if not row.get(COL_ANALYSIS_OK):
                row[COL_ANALYSIS_OK] = ""
            if not row.get(COL_CONVERSION_DONE):
                row[COL_CONVERSION_DONE] = "否"
            if not row.get(COL_CONVERSION_OK):
                row[COL_CONVERSION_OK] = ""
            if not row.get(COL_ANALYSIS_STATUS):
                row[COL_ANALYSIS_STATUS] = "pending"
            if not row.get(COL_CONVERSION_STATUS):
                row[COL_CONVERSION_STATUS] = "pending"
            next_rows.append(row)
        return headers or DEFAULT_COLUMNS[:], next_rows

    mutate_board(board_path, update_board)
    print(f"discovered {len(discovered)} mk/makefile file(s); board={rel(board_path)}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
