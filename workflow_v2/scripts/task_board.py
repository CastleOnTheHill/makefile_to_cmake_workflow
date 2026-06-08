#!/usr/bin/env python3
import json
import pathlib
import re
from collections import Counter
from typing import Any

from common import now, resolve_path


BOARD_MARKER = "workflow_v2:task_board_json"
BOARD_RE = re.compile(r"<!--\s*workflow_v2:task_board_json\s*(.*?)\s*-->", re.S)


def task_board_path(cfg: dict[str, Any]) -> pathlib.Path:
    configured = cfg.get("task_board_path")
    if configured:
        path = pathlib.Path(configured)
        return path if path.is_absolute() else resolve_path(str(path))
    return pathlib.Path(cfg["state_dir"]) / "task_board.md"


def empty_board() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "items": {},
    }


def load_board(cfg: dict[str, Any]) -> dict[str, Any]:
    path = task_board_path(cfg)
    if not path.exists():
        return empty_board()
    text = path.read_text(errors="replace")
    match = BOARD_RE.search(text)
    if not match:
        return empty_board()
    try:
        board = json.loads(match.group(1).strip())
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid task board JSON in {path}: {exc}") from exc
    if not isinstance(board, dict):
        raise SystemExit(f"invalid task board root in {path}: expected object")
    board.setdefault("schema_version", 1)
    board.setdefault("items", {})
    return board


def item_key(kind: str, item_id: str) -> str:
    return f"{kind}:{item_id}"


def get_item(board: dict[str, Any], kind: str, item_id: str) -> dict[str, Any] | None:
    item = board.setdefault("items", {}).get(item_key(kind, item_id))
    return item if isinstance(item, dict) else None


def upsert_item(
    board: dict[str, Any],
    kind: str,
    item_id: str,
    *,
    touch: bool = True,
    **fields: Any,
) -> dict[str, Any]:
    items = board.setdefault("items", {})
    key = item_key(kind, item_id)
    item = items.get(key)
    if not isinstance(item, dict):
        item = {
            "kind": kind,
            "id": item_id,
            "status": "pending",
            "created_at": now(),
        }
        items[key] = item
    item.update({name: value for name, value in fields.items() if value is not None})
    if touch:
        item["updated_at"] = now()
    return item


def markdown_cell(value: Any, *, limit: int = 240) -> str:
    if value is None:
        return ""
    text = str(value).replace("\r", "").replace("\n", "<br>")
    if len(text) > limit:
        text = text[: limit - 3] + "..."
    return text.replace("|", "\\|")


def sorted_items(board: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    rows = [
        item
        for item in board.get("items", {}).values()
        if isinstance(item, dict) and item.get("kind") == kind
    ]
    return sorted(rows, key=lambda item: (item.get("order", 10**12), str(item.get("id", ""))))


def render_summary(board: dict[str, Any]) -> str:
    lines = [
        "## Summary",
        "",
        "| Kind | Total | Pending | Done | Empty | Failed | Timeout | Manual Issue | Running |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for kind in ("analysis", "conversion"):
        counter = Counter(item.get("status", "pending") for item in sorted_items(board, kind))
        total = sum(counter.values())
        lines.append(
            "| %s | %d | %d | %d | %d | %d | %d | %d | %d |"
            % (
                kind,
                total,
                counter["pending"],
                counter["done"],
                counter["empty"],
                counter["failed"],
                counter["timeout"],
                counter["manual_issue"],
                counter["running"],
            )
        )
    return "\n".join(lines)


def render_analysis(board: dict[str, Any]) -> str:
    lines = [
        "## Analysis",
        "",
        "| Status | MK ID | Path | Rows | Return | Updated | Result | Comment |",
        "| --- | --- | --- | ---: | ---: | --- | --- | --- |",
    ]
    for item in sorted_items(board, "analysis"):
        lines.append(
            "| %s | `%s` | `%s` | %s | %s | %s | `%s` | %s |"
            % (
                markdown_cell(item.get("status")),
                markdown_cell(item.get("id")),
                markdown_cell(item.get("path"), limit=360),
                markdown_cell(item.get("rows")),
                markdown_cell(item.get("returncode")),
                markdown_cell(item.get("updated_at")),
                markdown_cell(item.get("stdout_log"), limit=280),
                markdown_cell(item.get("comment") or item.get("note")),
            )
        )
    return "\n".join(lines)


def render_conversion(board: dict[str, Any]) -> str:
    lines = [
        "## Conversion",
        "",
        "| Status | Target ID | Module | Source MK | CMake Dir | Return | Updated | Manual Comment |",
        "| --- | --- | --- | --- | --- | ---: | --- | --- |",
    ]
    for item in sorted_items(board, "conversion"):
        lines.append(
            "| %s | `%s` | `%s` | `%s` | `%s` | %s | %s | %s |"
            % (
                markdown_cell(item.get("status")),
                markdown_cell(item.get("id")),
                markdown_cell(item.get("module")),
                markdown_cell(item.get("source_mk"), limit=320),
                markdown_cell(item.get("cmake_dir"), limit=320),
                markdown_cell(item.get("returncode")),
                markdown_cell(item.get("updated_at")),
                markdown_cell(item.get("manual_comment") or item.get("comment"), limit=360),
            )
        )
    return "\n".join(lines)


def render_board(board: dict[str, Any]) -> str:
    board_json = json.dumps(board, ensure_ascii=False, indent=2, sort_keys=True)
    return "\n\n".join(
        [
            "# Workflow V2 Task Board",
            f"Last updated: {now()}",
            (
                "This file is generated by workflow_v2. The tables are for review; "
                "scripts resume from the embedded JSON block at the bottom. To mark a "
                "conversion result for rerun, use `workflow_v2/scripts/mark_conversion_issue.py` "
                "or edit that JSON block by setting the conversion item status to "
                "`manual_issue` and adding `manual_comment`."
            ),
            render_summary(board),
            render_analysis(board),
            render_conversion(board),
            f"<!-- {BOARD_MARKER}\n{board_json}\n-->",
        ]
    ) + "\n"


def save_board(cfg: dict[str, Any], board: dict[str, Any]) -> pathlib.Path:
    path = task_board_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(render_board(board))
    tmp.replace(path)
    return path
