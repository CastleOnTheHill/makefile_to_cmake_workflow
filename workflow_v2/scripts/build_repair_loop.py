#!/usr/bin/env python3
import json
import pathlib
import subprocess
import sys

from common import append_jsonl, error_excerpt, failure_signature, load_config, now, rel, require_args, run, write_text


def run_shell(command: str, cwd: pathlib.Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        shell=True,
        cwd=str(cwd),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
        executable="/bin/bash",
    )


def prompt_for(cfg: dict, product: dict, attempt: int, signature: str, excerpt: str, log_path: pathlib.Path) -> str:
    state = pathlib.Path(cfg["state_dir"])
    layout = cfg.get("cmake_output_layout", "centralized")
    return """# Build Repair Task

Product: `%s`
Attempt: `%d`
Failure signature: `%s`
Build command:
```bash
%s
```

CMake output layout: `%s`
CMake output directory: `%s`
Analyzed target JSONL: `%s`
Manual handoff file: `%s`
Full build log: `%s`

Latest error excerpt:
```text
%s
```

Make the smallest CMake edit that should advance the build. Do not run builds.
""" % (
        product["name"],
        attempt,
        signature,
        product["build_command"],
        layout,
        cfg["cmake_output_dir"],
        rel(state / "targets.jsonl"),
        rel(state / "manual_required.md"),
        rel(log_path),
        excerpt,
    )


def main() -> int:
    cfg = load_config(require_args(sys.argv, "usage: build_repair_loop.py <config.json>"))
    state = pathlib.Path(cfg["state_dir"])
    logs_dir = state / "logs"
    prompts_dir = state / "prompts"
    results_dir = state / "opencode_results"
    manual = state / "manual_required.md"
    max_attempts = int(cfg.get("max_fix_attempts_per_product", 8))
    max_same = int(cfg.get("max_same_failure", 3))
    excerpt_lines = int(cfg.get("error_excerpt_lines", 160))
    root = pathlib.Path(cfg["project_root"])

    for product in cfg.get("products", []):
        seen: dict[str, int] = {}
        for attempt in range(1, max_attempts + 1):
            print(f"[{product['name']}] build attempt {attempt}/{max_attempts}")
            cp = run_shell(product["build_command"], root)
            log_path = logs_dir / f"build.{product['name']}.attempt-{attempt}.log"
            write_text(log_path, cp.stdout)
            if cp.returncode == 0:
                append_jsonl(
                    state / "build_state.jsonl",
                    {
                        "product": product["name"],
                        "status": "done",
                        "attempt": attempt,
                        "time": now(),
                        "log": rel(log_path),
                    },
                )
                print(f"[{product['name']}] build passed")
                break

            sig = failure_signature(cp.stdout, excerpt_lines)
            seen[sig] = seen.get(sig, 0) + 1
            append_jsonl(
                state / "build_state.jsonl",
                {
                    "product": product["name"],
                    "status": "failed",
                    "attempt": attempt,
                    "signature": sig,
                    "same_signature_count": seen[sig],
                    "time": now(),
                    "log": rel(log_path),
                },
            )

            if seen[sig] >= max_same:
                with manual.open("a") as f:
                    f.write(
                        f"\n## {product['name']} stalled\n\n"
                        f"- Time: {now()}\n"
                        f"- Signature: `{sig}` repeated {seen[sig]} time(s)\n"
                        f"- Last log: `{rel(log_path)}`\n"
                        "- Action: manually fix CMake/source inputs, then rerun "
                        "`workflow_v2/scripts/build_repair_loop.py <config>`.\n"
                    )
                print(f"[{product['name']}] stalled; see {manual}")
                return 10

            prompt_path = prompts_dir / f"build-fix.{product['name']}.attempt-{attempt}.md"
            result_path = results_dir / f"build-fix.{product['name']}.attempt-{attempt}.out"
            stderr_path = logs_dir / f"build-fix.{product['name']}.attempt-{attempt}.err"
            excerpt = error_excerpt(cp.stdout, excerpt_lines)
            write_text(prompt_path, prompt_for(cfg, product, attempt, sig, excerpt, log_path))
            cmd = [
                cfg["opencode_bin"],
                "run",
                "--agent",
                "v2-build-fixer",
                "--model",
                cfg.get("model", "deepseek/deepseek-v4-pro"),
                "--file",
                str(prompt_path),
                "--",
                "Fix this build failure with the smallest edit.",
            ]
            fix = run(cmd, cfg)
            write_text(result_path, fix.stdout)
            write_text(stderr_path, fix.stderr)
            print(f"[{product['name']}] fixer returncode={fix.returncode} result={rel(result_path)}")
        else:
            with manual.open("a") as f:
                f.write(
                    f"\n## {product['name']} exceeded max attempts\n\n"
                    f"- Time: {now()}\n"
                    f"- Max attempts: {max_attempts}\n"
                    "- Action: manually fix and rerun build repair loop.\n"
                )
            return 11
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
