#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

project="${1:?usage: build_step.sh <cjson|libcurl>}"
converted="$DEMO_DIR/$project/converted"
build="$DEMO_DIR/$project/cmake-build"

if [[ ! -f "$converted/CMakeLists.txt" ]]; then
  echo "missing converted CMakeLists.txt: $converted" >&2
  exit 2
fi

log "Configuring $project converted CMake project"
cmake -S "$converted" -B "$build" -G Ninja -DCMAKE_BUILD_TYPE=Release 2>&1 | tee "$LOG_DIR/$project.cmake.configure.log"

log "Building $project converted CMake project"
cmake --build "$build" -j"$(nproc)" 2>&1 | tee "$LOG_DIR/$project.cmake.build.log"

if [[ "$project" == "cjson" ]]; then
  ctest --test-dir "$build" --output-on-failure 2>&1 | tee "$LOG_DIR/$project.ctest.log"
fi

find "$build" -maxdepth 5 \( -name '*.a' -o -name '*.so*' -o -perm -111 \) -type f | sort > "$DEMO_DIR/$project/converted_artifacts.txt"
append_status "CMake build completed for \`$project\`."

