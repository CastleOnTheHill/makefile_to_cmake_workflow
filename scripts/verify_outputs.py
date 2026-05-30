#!/usr/bin/env python3
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEMO = ROOT / "demo"
WORKFLOW = ROOT / "workflow"


def basenames(path):
    if not path.exists():
        return set()
    return {pathlib.Path(line.strip()).name for line in path.read_text().splitlines() if line.strip()}


def compile_count(path):
    if not path.exists():
        return 0
    try:
        return len(json.loads(path.read_text()))
    except Exception:
        return 0


def main():
    if len(sys.argv) != 2:
        print("usage: verify_outputs.py <cjson|libcurl>", file=sys.stderr)
        return 2
    project = sys.argv[1]
    original = basenames(DEMO / project / "original" / "artifacts.txt")
    converted = basenames(DEMO / project / "converted_artifacts.txt")
    compdb = compile_count(DEMO / project / "cmake-build" / "compile_commands.json")

    report = [f"# Verification: {project}", ""]
    report.append(f"- Original artifact count: {len(original)}")
    report.append(f"- Converted artifact count: {len(converted)}")
    report.append(f"- Converted compile command count: {compdb}")
    report.append("")

    expected = {
        "cjson": {"libcjson.a", "libcjson.so", "libcjson_utils.a", "libcjson_utils.so", "cjson_test"},
        "libcurl": {"libcurl.a", "curl"},
    }
    missing = sorted(expected.get(project, set()) - converted)
    if missing:
        report.append(f"- Missing expected converted artifacts: {', '.join(missing)}")
    else:
        report.append("- Expected converted artifacts are present.")

    overlap = sorted(original & converted)
    report.append(f"- Artifact basename overlap: {', '.join(overlap) if overlap else '(none)'}")

    out = WORKFLOW / f"verify_{project}.md"
    out.write_text("\n".join(report) + "\n")
    print(out)

    return 1 if missing or compdb == 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
