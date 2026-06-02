# Makefile/Android.mk Subproject to CMake Workflow

This workspace contains a resumable demo workflow for converting a Makefile or
Android.mk subproject into a standalone CMake project with AI-assisted task
splitting and repair.

The demo runs in two stages:

1. `cjson`: small project, used to validate the full loop quickly.
2. `libcurl`: larger project, used to exercise target/options/verification flow.

`workflow_v2/` contains a newer multi-product workflow skeleton for complex
Makefile/Android.mk projects. It splits analysis, CMake generation, and build
repair into three OpenCode agents.

The workflow keeps intermediate state under `workflow/` so it can be interrupted
and resumed.

## Quick Start

中文操作说明见：`docs/操作说明.md`

```bash
scripts/run_demo.sh cjson
scripts/run_demo.sh libcurl
```

To run both:

```bash
scripts/run_demo.sh all
```

## OpenCode / DeepSeek

`opencode.json` configures a DeepSeek OpenAI-compatible provider using
`DEEPSEEK_API_KEY` from the environment. The scripts read `mykey.txt` and export
the key only for the subprocess that needs it. The key is not written to logs.

OpenCode is installed locally into `.tools/` by `scripts/bootstrap_opencode.sh`.

## Resume

```bash
scripts/resume_workflow.sh
```

If a task repeatedly fails, it is recorded in `workflow/manual_required.md`.
After manually fixing the generated CMake files, rerun `scripts/resume_workflow.sh`.
