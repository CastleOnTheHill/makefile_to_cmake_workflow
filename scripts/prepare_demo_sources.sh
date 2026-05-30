#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

CJSON_TAG="${CJSON_TAG:-v1.7.19}"
CURL_TAG="${CURL_TAG:-curl-8_15_0}"

clone_or_update() {
  local name="$1"
  local url="$2"
  local tag="$3"
  local dst="$DEMO_DIR/$name/src"

  if [[ ! -d "$dst/.git" ]]; then
    log "Cloning $name $tag"
    mkdir -p "$(dirname "$dst")"
    git clone --depth 1 --branch "$tag" "$url" "$dst"
  else
    log "$name source already exists"
  fi

  git -C "$dst" rev-parse HEAD > "$DEMO_DIR/$name/source.rev"
  printf '%s\n' "$tag" > "$DEMO_DIR/$name/source.tag"
}

case "${1:-all}" in
  cjson)
    clone_or_update cjson https://github.com/DaveGamble/cJSON.git "$CJSON_TAG"
    ;;
  libcurl)
    clone_or_update libcurl https://github.com/curl/curl.git "$CURL_TAG"
    ;;
  all)
    clone_or_update cjson https://github.com/DaveGamble/cJSON.git "$CJSON_TAG"
    clone_or_update libcurl https://github.com/curl/curl.git "$CURL_TAG"
    ;;
  *)
    echo "usage: $0 [cjson|libcurl|all]" >&2
    exit 2
    ;;
esac

