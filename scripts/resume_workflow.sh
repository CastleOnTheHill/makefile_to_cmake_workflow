#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

if [[ ! -f "$TASKS_FILE" ]]; then
  echo "No workflow/tasks.jsonl found. Run scripts/run_demo.sh first." >&2
  exit 2
fi

mapfile -t task_ids < <(python3 - "$TASKS_FILE" <<'PY'
import json, sys
for line in open(sys.argv[1]):
    task = json.loads(line)
    print(task["id"])
PY
)

for task_id in "${task_ids[@]}"; do
  project="${task_id%%-*}"
  if [[ "$task_id" == libcurl-* ]]; then
    project=libcurl
  fi
  log "Resuming task $task_id"
  scripts/run_opencode_task.sh "$task_id" mk-analyzer || true
  scripts/generate_cmake_from_task.py "$task_id" >/dev/null
  scripts/build_step.sh "$project" || {
    record_manual_required "$task_id" "build failed; inspect workflow/logs/$project.cmake.build.log"
    continue
  }
  scripts/verify_outputs.py "$project" || {
    record_manual_required "$task_id" "verification failed; inspect workflow/verify_$project.md"
    continue
  }
done

append_status "Resume pass completed."

