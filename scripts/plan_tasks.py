#!/usr/bin/env python3
import json
import os
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / "workflow"
DEMO = ROOT / "demo"


def read(path):
    return path.read_text(errors="replace") if path.exists() else ""


def cjson_tasks():
    src = DEMO / "cjson" / "src"
    makefile = read(src / "Makefile")
    sources = sorted(p.name for p in src.glob("*.c"))
    return [
        {
            "id": "cjson-core",
            "project": "cjson",
            "kind": "library",
            "source_dir": str(src),
            "makefile": str(src / "Makefile"),
            "summary": "Convert cJSON library targets from Makefile into standalone CMake.",
            "sources": [s for s in sources if s == "cJSON.c"],
            "outputs": ["cjson_static", "cjson_shared"],
            "hints": {"makefile_excerpt": makefile[:6000]},
            "status": "pending",
            "attempts": 0,
        },
        {
            "id": "cjson-test",
            "project": "cjson",
            "kind": "test",
            "source_dir": str(src),
            "makefile": str(src / "Makefile"),
            "summary": "Convert cJSON tests into CMake/CTest targets.",
            "sources": [s for s in sources if s != "cJSON.c"],
            "outputs": ["cjson_test"],
            "hints": {"makefile_excerpt": makefile[:6000]},
            "status": "pending",
            "attempts": 0,
        },
    ]


def parse_makefile_inc(path):
    text = read(path)
    files = []
    for match in re.finditer(r"([A-Za-z0-9_]+)\s*=\s*((?:.|\n)*?)(?=\n[A-Za-z0-9_]+\s*=|\Z)", text):
        name, value = match.group(1), match.group(2)
        if name in {"LIB_CFILES", "LIB_VTLS_CFILES", "LIB_VQUIC_CFILES", "CSOURCES", "CURL_CFILES", "CURLX_CFILES"}:
            normalized = value.replace("\\\n", " ")
            files.extend(x.replace("../", "") for x in re.split(r"\s+", normalized) if x.endswith(".c"))
    return sorted(dict.fromkeys(files))


def libcurl_tasks():
    src = DEMO / "libcurl" / "src"
    lib_sources = parse_makefile_inc(src / "lib" / "Makefile.inc")
    tool_sources = []
    for item in parse_makefile_inc(src / "src" / "Makefile.inc"):
        tool_sources.append(item if item.startswith("lib/") else f"src/{item}")
    return [
        {
            "id": "libcurl-library",
            "project": "libcurl",
            "kind": "library",
            "source_dir": str(src),
            "makefile": str(src / "lib" / "Makefile.inc"),
            "summary": "Convert libcurl library source list and minimal no-SSL feature set.",
            "sources": [f"lib/{s}" for s in lib_sources],
            "outputs": ["libcurl_static"],
            "hints": {
                "configure": "--disable-shared --enable-static --without-ssl --without-zlib",
                "source_count": len(lib_sources),
            },
            "status": "pending",
            "attempts": 0,
        },
        {
            "id": "libcurl-tool",
            "project": "libcurl",
            "kind": "executable",
            "source_dir": str(src),
            "makefile": str(src / "src" / "Makefile.inc"),
            "summary": "Convert curl command-line tool source list and link to converted libcurl.",
            "sources": tool_sources,
            "outputs": ["curl"],
            "hints": {"source_count": len(tool_sources)},
            "status": "pending",
            "attempts": 0,
        },
    ]


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in {"cjson", "libcurl", "all"}:
        print("usage: plan_tasks.py <cjson|libcurl|all>", file=sys.stderr)
        return 2
    projects = ["cjson", "libcurl"] if sys.argv[1] == "all" else [sys.argv[1]]
    tasks = []
    for project in projects:
        tasks.extend(cjson_tasks() if project == "cjson" else libcurl_tasks())

    WORKFLOW.mkdir(exist_ok=True)
    with (WORKFLOW / "tasks.jsonl").open("w") as f:
        for task in tasks:
            f.write(json.dumps(task, ensure_ascii=False) + "\n")

    status = ["# Workflow Status", "", f"Generated {len(tasks)} task(s).", ""]
    for task in tasks:
        status.append(f"- [ ] `{task['id']}`: {task['summary']}")
    (WORKFLOW / "status.md").write_text("\n".join(status) + "\n")
    print(f"wrote {WORKFLOW / 'tasks.jsonl'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
