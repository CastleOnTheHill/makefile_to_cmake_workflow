#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

task_id="${1:?usage: run_opencode_task.sh <task-id> [agent]}"
agent="${2:-mk-analyzer}"
task_json="$(python3 - "$task_id" "$TASKS_FILE" <<'PY'
import json, sys
task_id, tasks_file = sys.argv[1], sys.argv[2]
for line in open(tasks_file):
    task = json.loads(line)
    if task["id"] == task_id:
        print(json.dumps(task, ensure_ascii=False, indent=2))
        break
else:
    raise SystemExit(f"task not found: {task_id}")
PY
)"

mkdir -p "$WORKFLOW_DIR/opencode"
prompt_file="$WORKFLOW_DIR/opencode/$task_id.$agent.prompt.md"
result_file="$WORKFLOW_DIR/opencode/$task_id.$agent.result.md"

cat > "$prompt_file" <<EOF
# Task

You are running the $agent step for this Makefile/Android.mk to CMake conversion task.

\`\`\`json
$task_json
\`\`\`

Return concise, implementation-oriented Markdown. Do not reveal secrets.
EOF

if ! load_deepseek_key; then
  echo "OpenCode skipped: missing mykey.txt" > "$result_file"
  exit 0
fi

if ! OC_BIN="$(opencode_bin)"; then
  echo "OpenCode skipped: opencode is not installed" > "$result_file"
  exit 0
fi

log "Running OpenCode agent=$agent task=$task_id"
"$OC_BIN" run --agent "$agent" --model "${OPENCODE_MODEL:-deepseek/deepseek-v4-pro}" --file "$prompt_file" -- "Analyze this conversion task." > "$result_file" 2>"$LOG_DIR/$task_id.$agent.stderr.log" || {
  echo "OpenCode failed; see $LOG_DIR/$task_id.$agent.stderr.log" >> "$result_file"
  exit 0
}

if [[ ! -s "$result_file" ]]; then
  {
    echo "# OpenCode Run"
    echo
    echo "OpenCode completed without stdout. Tool trace is in:"
    echo
    echo "- \`$LOG_DIR/$task_id.$agent.stderr.log\`"
    echo
    echo "## Tool Trace Excerpt"
    echo
    sed -r 's/\x1B\[[0-9;]*[mK]//g' "$LOG_DIR/$task_id.$agent.stderr.log" | head -n 120
  } > "$result_file"
fi
