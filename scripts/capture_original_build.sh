#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

project="${1:?usage: capture_original_build.sh <cjson|libcurl>}"
src="$DEMO_DIR/$project/src"
out="$DEMO_DIR/$project/original"
mkdir -p "$out"

if [[ ! -d "$src" ]]; then
  echo "missing source directory: $src" >&2
  exit 2
fi

log "Capturing original build for $project"

case "$project" in
  cjson)
    make -C "$src" clean >/dev/null 2>&1 || true
    make -C "$src" all V=1 2>&1 | tee "$out/build.log"
    find "$src" -maxdepth 2 \( -name '*.a' -o -name '*.so*' -o -perm -111 \) -type f | sort > "$out/artifacts.txt"
    ;;
  libcurl)
    build="$out/build"
    mkdir -p "$build"
    if [[ ! -x "$src/configure" ]]; then
      (cd "$src" && ./buildconf) 2>&1 | tee "$out/buildconf.log"
    fi
    (cd "$build" && "$src/configure" --disable-shared --enable-static --without-ssl --without-zlib --without-libpsl --without-brotli --without-zstd --disable-ldap --disable-rtsp --disable-dict --disable-telnet --disable-tftp --disable-pop3 --disable-imap --disable-smtp --disable-gopher --disable-mqtt --disable-manual) 2>&1 | tee "$out/configure.log"
    make -C "$build" V=1 -j"$(nproc)" 2>&1 | tee "$out/build.log"
    find "$build" -maxdepth 4 \( -name '*.a' -o -name '*.so*' -o -perm -111 \) -type f | sort > "$out/artifacts.txt"
    ;;
  *)
    echo "unknown project: $project" >&2
    exit 2
    ;;
esac

append_status "Captured original build for \`$project\`."
