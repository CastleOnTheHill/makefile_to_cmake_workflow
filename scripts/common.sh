#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TOOLS_DIR="$ROOT_DIR/.tools"
WORKFLOW_DIR="$ROOT_DIR/workflow"
DEMO_DIR="$ROOT_DIR/demo"
LOG_DIR="$WORKFLOW_DIR/logs"
TASKS_FILE="$WORKFLOW_DIR/tasks.jsonl"
STATUS_FILE="$WORKFLOW_DIR/status.md"
MANUAL_FILE="$WORKFLOW_DIR/manual_required.md"

mkdir -p "$TOOLS_DIR" "$WORKFLOW_DIR" "$LOG_DIR" "$DEMO_DIR"

log() {
  printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

opencode_bin() {
  if [[ -x "$TOOLS_DIR/node_modules/.bin/opencode" ]]; then
    printf '%s\n' "$TOOLS_DIR/node_modules/.bin/opencode"
  elif command -v opencode >/dev/null 2>&1; then
    command -v opencode
  else
    return 1
  fi
}

load_deepseek_key() {
  if [[ ! -f "$ROOT_DIR/mykey.txt" ]]; then
    log "mykey.txt not found; OpenCode calls will be skipped"
    return 1
  fi
  DEEPSEEK_API_KEY="$(tr -d '\r\n' < "$ROOT_DIR/mykey.txt")"
  export DEEPSEEK_API_KEY
}

append_status() {
  mkdir -p "$(dirname "$STATUS_FILE")"
  printf '\n## %s\n\n%s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$STATUS_FILE"
}

record_manual_required() {
  local task="$1"
  local reason="$2"
  mkdir -p "$(dirname "$MANUAL_FILE")"
  {
    printf '\n## %s\n\n' "$task"
    printf '- Time: %s\n' "$(date '+%Y-%m-%d %H:%M:%S')"
    printf '- Reason: %s\n' "$reason"
  } >> "$MANUAL_FILE"
}

