#!/usr/bin/env python3
import argparse
import pathlib

from common import load_config, now, rel
from task_board import get_item, load_board, save_board, upsert_item


def read_comment(args: argparse.Namespace) -> str:
    parts = []
    if args.comment:
        parts.append(args.comment)
    if args.comment_file:
        parts.append(pathlib.Path(args.comment_file).read_text(errors="replace").strip())
    return "\n\n".join(part for part in parts if part).strip()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("config")
    parser.add_argument("target_id")
    parser.add_argument("-m", "--comment", default="", help="manual review comment for the next conversion retry")
    parser.add_argument("--comment-file", help="file containing a manual review comment")
    args = parser.parse_args()

    cfg = load_config(args.config)
    board = load_board(cfg)
    existing = get_item(board, "conversion", args.target_id) or {}
    comment = read_comment(args) or existing.get("manual_comment", "")
    upsert_item(
        board,
        "conversion",
        args.target_id,
        status="manual_issue",
        manual_comment=comment,
        manual_issue_marked_at=now(),
    )
    board_path = save_board(cfg, board)
    print(f"marked conversion target for retry: {args.target_id}")
    print(f"board: {rel(board_path)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
