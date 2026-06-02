#!/usr/bin/env python3
import json
import pathlib
import sys

from common import append_jsonl, load_config, now, require_args


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: mark_manual_fixed.py <config.json> <product-or-task-id>", file=sys.stderr)
        return 2
    cfg = load_config(sys.argv[1])
    item = sys.argv[2]
    state = pathlib.Path(cfg["state_dir"])
    append_jsonl(
        state / "manual_fixes.jsonl",
        {
            "item": item,
            "status": "manual_fixed",
            "time": now(),
            "note": "Human marked this item ready for retry.",
        },
    )
    print(f"marked manual fix for {item}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

