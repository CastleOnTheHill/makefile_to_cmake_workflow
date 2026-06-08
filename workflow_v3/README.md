# Workflow V3

`workflow_v3` is a smaller Excel-driven Makefile/Android.mk/*.mk to CMake
workflow. The Excel file is the task source, status board, resume state, and
manual review channel.

## Install

```bash
python3 -m pip install --user -r workflow_v3/requirements.txt
```

## Configure

Copy `workflow_v3/config.example.json` to a local config and update:

- `project_root`: repository root of the original project.
- `scan_subdir`: subdirectory to convert.
- `board_path`: Excel task board path.
- `state_dir`: minimal runtime logs and analysis JSONL.
- `opencode_bin`, `model`, `api_key_file`.

CMake output is not configurable in v3. Each row writes to `CMakeLists.txt`
beside the original mk/makefile.

## Run

```bash
workflow_v3/scripts/discover_mk_files.py workflow_v3/config.local.json --limit 20
workflow_v3/scripts/analyze_mk_files.py workflow_v3/config.local.json -j 3 --limit 20
workflow_v3/scripts/convert_targets.py workflow_v3/config.local.json -j 3 --limit 20
```

Or run the full flow:

```bash
workflow_v3/scripts/run_all.py workflow_v3/config.local.json -j 3 --limit 20
```

## Excel Board

Required columns:

- `原始mk/makefile路径`
- `转换后cmake文件路径`
- `是否完成分析`
- `分析是否成功`
- `是否完成转换`
- `转换是否成功`
- `人工意见`

Extra columns are preserved. Scripts update one completed row at a time and save
the Excel file after each OpenCode task finishes.

Manual review loop:

1. Open the generated `CMakeLists.txt`.
2. If it is wrong, write a comment in the row's `人工意见` cell.
3. Run `convert_targets.py` again.
4. The converter reruns that row, passes the comment to OpenCode, and on
   success changes the cell to `已完成：<original comment>`.

Resume behavior:

- Successful analysis is reused.
- Failed or timed-out analysis reruns next time.
- Successful conversion is reused if the target trace exists in CMake.
- Rows with active `人工意见` rerun conversion even if conversion succeeded.
- Use `--force` to rerun selected rows manually.

Single-file rerun:

```bash
workflow_v3/scripts/analyze_mk_files.py workflow_v3/config.local.json --mk-file path/to/package.mk --force
workflow_v3/scripts/convert_targets.py workflow_v3/config.local.json --mk-file path/to/package.mk --force
```

## Fake OpenCode

Use fake mode to smoke-test a real project without calling a model:

```bash
workflow_v3/scripts/run_fake.py workflow_v3/config.local.json -j 8 --limit 100
```

It runs discover/analyze/convert with `workflow_v3/scripts/fake_opencode.py`.
The generated `CMakeLists.txt` files contain only workflow trace comments, not
real targets.
