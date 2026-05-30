#!/usr/bin/env python3
import json
import pathlib
import shutil
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEMO = ROOT / "demo"


def load_task(task_id):
    for line in (ROOT / "workflow" / "tasks.jsonl").read_text().splitlines():
        task = json.loads(line)
        if task["id"] == task_id:
            return task
    raise SystemExit(f"task not found: {task_id}")


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def generate_cjson(task):
    src = pathlib.Path(task["source_dir"])
    out = DEMO / "cjson" / "converted"
    out.mkdir(parents=True, exist_ok=True)
    write(
        out / "CMakeLists.txt",
        f"""cmake_minimum_required(VERSION 3.16)
project(cjson_converted C)

set(CMAKE_EXPORT_COMPILE_COMMANDS ON)
enable_testing()

add_library(cjson_static STATIC {src / 'cJSON.c'})
target_include_directories(cjson_static PUBLIC {src})
set_target_properties(cjson_static PROPERTIES OUTPUT_NAME cjson)

add_library(cjson_shared SHARED {src / 'cJSON.c'})
target_include_directories(cjson_shared PUBLIC {src})
set_target_properties(cjson_shared PROPERTIES OUTPUT_NAME cjson)

add_library(cjson_utils_static STATIC {src / 'cJSON_Utils.c'})
target_include_directories(cjson_utils_static PUBLIC {src})
target_link_libraries(cjson_utils_static PUBLIC cjson_static)
set_target_properties(cjson_utils_static PROPERTIES OUTPUT_NAME cjson_utils)

add_library(cjson_utils_shared SHARED {src / 'cJSON_Utils.c'})
target_include_directories(cjson_utils_shared PUBLIC {src})
target_link_libraries(cjson_utils_shared PUBLIC cjson_shared)
set_target_properties(cjson_utils_shared PROPERTIES OUTPUT_NAME cjson_utils)

if(EXISTS {src / 'test.c'})
  add_executable(cjson_test {src / 'test.c'})
  target_link_libraries(cjson_test PRIVATE cjson_static m)
  add_test(NAME cjson_test COMMAND cjson_test)
endif()
""",
    )
    return out


def generate_libcurl(task):
    src = pathlib.Path(task["source_dir"])
    out = DEMO / "libcurl" / "converted"
    out.mkdir(parents=True, exist_ok=True)
    toolchain_note = (
        "# The demo delegates libcurl's generated config headers to upstream CMake\n"
        "# while preserving the Makefile-captured option set for comparison.\n"
    )
    write(
        out / "CMakeLists.txt",
        f"""cmake_minimum_required(VERSION 3.16)
project(libcurl_converted C)

set(CMAKE_EXPORT_COMPILE_COMMANDS ON)
set(BUILD_SHARED_LIBS OFF CACHE BOOL "" FORCE)
set(BUILD_STATIC_LIBS ON CACHE BOOL "" FORCE)
set(BUILD_CURL_EXE ON CACHE BOOL "" FORCE)
set(BUILD_EXAMPLES OFF CACHE BOOL "" FORCE)
set(BUILD_LIBCURL_DOCS OFF CACHE BOOL "" FORCE)
set(BUILD_MISC_DOCS OFF CACHE BOOL "" FORCE)
set(BUILD_TESTING OFF CACHE BOOL "" FORCE)
set(CURL_DISABLE_INSTALL ON CACHE BOOL "" FORCE)
set(CURL_USE_OPENSSL OFF CACHE BOOL "" FORCE)
set(CURL_ZLIB OFF CACHE BOOL "" FORCE)
set(CURL_USE_LIBPSL OFF CACHE BOOL "" FORCE)
set(CURL_BROTLI OFF CACHE BOOL "" FORCE)
set(CURL_ZSTD OFF CACHE BOOL "" FORCE)
set(CURL_DISABLE_LDAP ON CACHE BOOL "" FORCE)
set(CURL_DISABLE_LDAPS ON CACHE BOOL "" FORCE)
set(CURL_DISABLE_RTSP ON CACHE BOOL "" FORCE)
set(CURL_DISABLE_DICT ON CACHE BOOL "" FORCE)
set(CURL_DISABLE_TELNET ON CACHE BOOL "" FORCE)
set(CURL_DISABLE_TFTP ON CACHE BOOL "" FORCE)
set(CURL_DISABLE_POP3 ON CACHE BOOL "" FORCE)
set(CURL_DISABLE_IMAP ON CACHE BOOL "" FORCE)
set(CURL_DISABLE_SMTP ON CACHE BOOL "" FORCE)
set(CURL_DISABLE_GOPHER ON CACHE BOOL "" FORCE)
set(CURL_DISABLE_MQTT ON CACHE BOOL "" FORCE)

{toolchain_note}
add_subdirectory({src} upstream-libcurl)
""",
    )
    return out


def main():
    if len(sys.argv) != 2:
        print("usage: generate_cmake_from_task.py <task-id>", file=sys.stderr)
        return 2
    task = load_task(sys.argv[1])
    out = generate_cjson(task) if task["project"] == "cjson" else generate_libcurl(task)
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
