#!/usr/bin/env python3
import argparse
import hashlib
import json
import pathlib
import re
import sys


def stable_id(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def extract_json_block(text: str, marker: str) -> dict:
    marker_index = text.find(marker)
    search_from = marker_index if marker_index >= 0 else 0
    fence = text.find("```json", search_from)
    if fence < 0:
        return {}
    start = text.find("\n", fence)
    end = text.find("```", start + 1)
    if start < 0 or end < 0:
        return {}
    try:
        value = json.loads(text[start:end].strip())
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def parse_agent(argv: list[str]) -> str:
    if "--agent" not in argv:
        return ""
    index = argv.index("--agent")
    return argv[index + 1] if index + 1 < len(argv) else ""


def parse_prompt_path(argv: list[str]) -> pathlib.Path:
    if "--file" not in argv:
        raise SystemExit("fake_opencode requires --file <prompt>")
    index = argv.index("--file")
    if index + 1 >= len(argv):
        raise SystemExit("fake_opencode requires --file <prompt>")
    return pathlib.Path(argv[index + 1])


def fake_analysis(prompt: str) -> int:
    task = extract_json_block(prompt, "Primary task:")
    mk_path = task.get("原始mk/makefile路径", "unknown.mk")
    target = {
        "schema_version": 1,
        "source_mk": mk_path,
        "included_mk": [],
        "target_id": f"fake-{stable_id(mk_path)}",
        "module": pathlib.PurePosixPath(mk_path.replace("\\", "/")).stem or "fake_module",
        "target_type": "include_aggregator",
        "conditions": [{"expression": "workflow_v3_fake", "effect": "unknown", "value": "fake analysis placeholder"}],
        "conditional_sources": [],
        "conditional_include_dirs": [],
        "conditional_defines": [],
        "conditional_compile_options": [],
        "conditional_link_libraries": [],
        "conditional_link_options": [],
        "sources": [],
        "generated_sources": [],
        "include_dirs": [],
        "export_include_dirs": [],
        "defines": [],
        "compile_options": [],
        "link_libraries": [],
        "link_options": [],
        "cmake_includes": [],
        "c_standard": "",
        "cxx_standard": "",
        "artifacts": [],
        "risks": ["fake opencode result; no real Makefile analysis was performed"],
        "confidence": "low",
    }
    print(json.dumps(target, ensure_ascii=False, sort_keys=True))
    return 0


def extract_backtick_value(prompt: str, label: str) -> str:
    pattern = re.compile(re.escape(label) + r"\s*`([^`]+)`")
    match = pattern.search(prompt)
    return match.group(1).strip() if match else ""


def fake_conversion(prompt: str) -> int:
    cmake_path = extract_backtick_value(prompt, "CMake output file:")
    if not cmake_path:
        raise SystemExit("fake conversion could not find CMake output file in prompt")
    trace_match = re.search(r"workflow_v3:mk_task_id=([^\s`]+)\s+source_mk=([^\s`]+)", prompt)
    task_id = trace_match.group(1) if trace_match else "unknown"
    source_mk = trace_match.group(2) if trace_match else "unknown"

    cmake_file = pathlib.Path(cmake_path)
    cmake_file.parent.mkdir(parents=True, exist_ok=True)
    cmake_file.write_text(
        "# workflow_v3 fake CMakeLists.txt\n"
        f"# workflow_v3:mk_task_id={task_id} source_mk={source_mk}\n",
        encoding="utf-8",
    )
    print(f"fake conversion wrote {cmake_file}")
    return 0


def fake_build_fix(prompt: str) -> int:
    signature = extract_backtick_value(prompt, "Failure signature:")
    print("# fake v3 build fixer")
    print()
    print(f"- suspected root cause: fake mode did not inspect or edit files for signature `{signature or 'unknown'}`")
    print("- files changed: none")
    print("- why this advances the build: it does not; this only validates workflow plumbing")
    print("- remaining risk: rerun with a real opencode binary for actual repair")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("command", nargs="?")
    args, _ = parser.parse_known_args(argv[1:])
    if args.command != "run":
        raise SystemExit("fake_opencode only supports: opencode run ...")

    agent = parse_agent(argv)
    prompt = parse_prompt_path(argv).read_text(encoding="utf-8", errors="replace")
    if agent == "v3-mk-analyzer":
        return fake_analysis(prompt)
    if agent == "v3-cmake-converter":
        return fake_conversion(prompt)
    if agent == "v3-build-fixer":
        return fake_build_fix(prompt)
    raise SystemExit(f"fake_opencode does not support agent: {agent}")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
