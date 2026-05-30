#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

target="${1:-all}"
case "$target" in
  cjson|libcurl|all) ;;
  *)
    echo "usage: $0 [cjson|libcurl|all]" >&2
    exit 2
    ;;
esac

scripts/bootstrap_opencode.sh
scripts/prepare_demo_sources.sh "$target"

if [[ "$target" == "all" ]]; then
  projects=(cjson libcurl)
else
  projects=("$target")
fi

for project in "${projects[@]}"; do
  scripts/capture_original_build.sh "$project"
done

scripts/plan_tasks.py "$target"

mapfile -t task_ids < <(python3 - "$TASKS_FILE" <<'PY'
import json, sys
for line in open(sys.argv[1]):
    print(json.loads(line)["id"])
PY
)

for task_id in "${task_ids[@]}"; do
  scripts/run_opencode_task.sh "$task_id" mk-analyzer || true
  scripts/generate_cmake_from_task.py "$task_id" >/dev/null
done

for project in "${projects[@]}"; do
  scripts/build_step.sh "$project"
  scripts/verify_outputs.py "$project"
done

append_status "Demo completed for \`$target\`."
log "Done. See workflow/status.md and workflow/verify_*.md"

