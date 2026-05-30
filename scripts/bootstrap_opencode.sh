#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

log "Checking OpenCode"

if opencode_bin >/dev/null 2>&1; then
  "$(opencode_bin)" --version >/dev/null 2>&1 || true
fi

if ! opencode_bin >/dev/null 2>&1; then
  log "Installing opencode-ai locally under .tools"
  npm install --prefix "$TOOLS_DIR" opencode-ai @ai-sdk/openai-compatible || \
    NPM_CONFIG_REGISTRY=https://registry.npmmirror.com npm install --prefix "$TOOLS_DIR" opencode-ai @ai-sdk/openai-compatible
fi

OC_BIN="$(opencode_bin)"
VERSION="$("$OC_BIN" --version 2>/dev/null || true)"
log "OpenCode: ${VERSION:-unknown version} at $OC_BIN"

if [[ -f "$ROOT_DIR/mykey.txt" ]]; then
  log "DeepSeek key file found"
else
  log "DeepSeek key file missing; model-driven steps will be skipped"
fi
