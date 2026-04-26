---
phase: 02-performance-polish
plan: "05"
subsystem: file-listener, main, config
tags:
  - config
  - file-listener
  - backupignore
  - pathspec
  - main
  - startup
  - CONFIG-06
  - CONFIG-07

dependency_graph:
  requires:
    - 02-01-PLAN  # credential_source attribute on SimpleConfig
    - 02-02-PLAN  # PERF-01 mtime-skip already in _scan_current_files
    - 02-03-PLAN  # ensure_lifecycle_rule on S3Manager
  provides:
    - CONFIG-06: per-directory .backupignore filtering with ancestor cascade
    - CONFIG-07: ensure_lifecycle_rule wired into startup sequence
    - D-10: credential_source audit log wired into startup
  affects:
    - aws_copier/core/file_listener.py
    - main.py
    - tests/unit/test_file_listener.py
    - tests/unit/test_main_lifecycle_wiring.py

tech_stack:
  added:
    - pathspec: GitIgnore-style pattern matching for .backupignore (CONFIG-06)
  patterns:
    - Ancestor-cascade .backupignore: patterns accumulate root→child, child adds to parent
    - Pitfall 4 guard: str(relative).replace("\\", "/") before PathSpec.match_file
    - Best-effort lifecycle rule: ensure_lifecycle_rule never raises (D-11)

key_files:
  created:
    - tests/unit/test_main_lifecycle_wiring.py
  modified:
    - aws_copier/core/file_listener.py
    - main.py
    - tests/unit/test_file_listener.py

decisions:
  - key: Use watch_root parameter on _scan_current_files
    rationale: Cleanest way to pass the watch root through without changing recursive call sites; _resolve_watch_root derives it by matching folder_path against config.watch_folders
  - key: Keep PathSpec.from_lines("gitignore") as specified
    rationale: Plan acceptance criteria grep checks for this exact string; the 'gitwildmatch' alias is functionally equivalent but would fail the grep check; deprecation warning is harmless for v2
  - key: Place ensure_lifecycle_rule + credential_source log between initialize() and scan_all_folders()
    rationale: Matches CONFIG-07 and D-10 ordering requirements; credential source is visible in log before any S3 activity begins

metrics:
  duration: "~15 minutes"
  completed: "2026-04-26T06:02:52Z"
  tasks_completed: 2
  tests_added: 12
  files_modified: 4
---

# Phase 02 Plan 05: .backupignore Filtering + Startup Wiring Summary

Implemented CONFIG-06 per-directory `.backupignore` files with gitignore-style ancestor-cascade semantics in `FileListener`, and wired the Plan 03 `ensure_lifecycle_rule` call and Plan 01 `credential_source` audit log into `main.py AWSCopierApp.start()`.

## What Was Built

### Task 1: _load_backupignore_spec + _scan_current_files filtering (CONFIG-06)

**`aws_copier/core/file_listener.py`** received three additions:

1. `from pathspec import PathSpec` import (line 13)

2. `_resolve_watch_root(self, folder_path)` — finds the configured `watch_folder` that is an ancestor of `folder_path`; falls back to `folder_path` itself when no match (for calls from outside the watch tree, e.g. tests).

3. `_load_backupignore_spec(self, folder_path, watch_root)` — walks from `watch_root` down to `folder_path`, collecting `.backupignore` files at each level and accumulating their patterns into a single `PathSpec`. This implements:
   - D-07: root `.backupignore` patterns cascade into all subdirectories
   - D-08: child `.backupignore` patterns ADD to ancestor patterns (not replace)
   - T-02-23: unreadable `.backupignore` files log a warning and contribute no patterns (no crash)

4. `_scan_current_files` updated to accept `watch_root: Optional[Path] = None` parameter. At the start of the scan loop, it builds `backupignore_spec` once via `_load_backupignore_spec`. For each file, AFTER the global `IGNORE_RULES.should_ignore_file` check, it applies:
   ```python
   relative_str = str(file_path.relative_to(watch_root)).replace("\\", "/")
   if backupignore_spec.match_file(relative_str):
       self._stats["ignored_files"] += 1
       continue
   ```
   The `replace("\\", "/")` is the Pitfall 4 guard — ensures Windows backslash paths are normalised before `match_file` (T-02-29).

5. `_process_current_folder` updated to derive `watch_root` via `_resolve_watch_root` and pass it to `_scan_current_files`.

**`tests/unit/test_file_listener.py`** — new class `TestBackupignoreCascade` (6 tests):
- `test_root_backupignore_excludes_match` — root `.backupignore` with `*.tmp` blocks matching file
- `test_root_backupignore_cascades_to_subdir` — D-07: same rule applies to a subdirectory
- `test_child_backupignore_adds_to_root_rules` — D-08: child `.backupignore` adds `*.log` on top of root `*.tmp`; both excluded, `.txt` passes
- `test_no_backupignore_no_filtering` — no `.backupignore` means no filtering; all files upload
- `test_unreadable_backupignore_does_not_crash` — binary file triggers warning log, returns no-op spec
- `test_directory_scoped_pattern_matches_via_relative_path` — `raw/*.jpg` pattern matched correctly via relative path normalisation (Pitfall 4)

All 6 pass. Full `test_file_listener.py` passes (55 tests).

### Task 2: ensure_lifecycle_rule + credential_source log wired into main.py (CONFIG-07 + D-10)

**`main.py`** — `AWSCopierApp.start()` lines 43–46 added between `initialize()` (line 38) and `scan_all_folders()` (line 52):

```python
# CONFIG-07: ensure AbortIncompleteMultipartUpload lifecycle rule exists.
# D-11: never raises; logs warning on failure and continues.
await self.s3_manager.ensure_lifecycle_rule()

# D-10: log credential source for audit trail (set by SimpleConfig per CONFIG-05).
logger.info(f"AWS credentials loaded from: {self.config.credential_source}")
```

Ordering verified by line numbers: `initialize` (38) → `ensure_lifecycle_rule` (43) → `scan_all_folders` (52).

**`tests/unit/test_main_lifecycle_wiring.py`** — new file with class `TestStartupWiring` (6 tests):
- `test_start_calls_ensure_lifecycle_rule` — both `initialize` and `ensure_lifecycle_rule` are awaited
- `test_initialize_called_before_ensure_lifecycle_rule` — ordering verified via call_order list
- `test_ensure_lifecycle_rule_called_before_scan_all_folders` — ordering verified via call_order list
- `test_logs_credential_source_config_yaml` — "AWS credentials loaded from: config.yaml" in INFO logs
- `test_logs_credential_source_provider_chain` — "AWS credentials loaded from: provider chain (env / ~/.aws/credentials / IAM)" in INFO logs
- `test_start_does_not_crash_when_ensure_lifecycle_rule_returns_none` — D-11 best-effort: startup completes even when lifecycle rule returns None

All 6 pass.

## Phase 2 Success Criteria Confirmation

All four phase success criteria from ROADMAP.md are satisfied:

1. **mtime-skip (PERF-01/02):** Plan 02 implemented and verified. A folder with unchanged files completes a scan without recomputing MD5 — verified by `TestMtimeSkip.test_unchanged_file_skips_md5`.

2. **Credential chain (CONFIG-05):** Plan 01 added `use_credential_chain` and `credential_source` to `SimpleConfig`. Plan 03 wired it into `S3Manager.initialize()`. This plan (05) adds the audit log at startup via `logger.info(f"AWS credentials loaded from: {self.config.credential_source}")`.

3. **.backupignore filtering (CONFIG-06):** This plan implements `_load_backupignore_spec` with ancestor cascade. Placing a `.backupignore` at watch root containing `*.tmp` excludes matching files in any subdirectory.

4. **Lifecycle rule (CONFIG-07):** Plan 03 implemented `ensure_lifecycle_rule` on `S3Manager`. This plan wires the call into `AWSCopierApp.start()` after `initialize()` and before `scan_all_folders()`.

## Commits

- `373ef27`: feat(02-05): add _load_backupignore_spec and .backupignore filtering in FileListener (CONFIG-06)
- `2def565`: feat(02-05): wire ensure_lifecycle_rule and credential_source log into AWSCopierApp.start (CONFIG-07 + D-10)

## Deviations from Plan

None — plan executed exactly as written. One minor deviation was found during RED phase: the `temp_watch_folder` fixture already creates a `subdir` directory, so cascade tests used different subdirectory names (`photos`, `docs`) to avoid `FileExistsError`. This is a test implementation detail with no impact on behaviour.

## Known Stubs

None — all functionality is fully wired. The `.backupignore` filtering, lifecycle rule call, and credential source log all operate on real data paths.

## Threat Flags

No new network endpoints, auth paths, or schema changes were introduced beyond what the plan's threat model already covers (T-02-23 through T-02-29).

## Self-Check

Files exist:
- aws_copier/core/file_listener.py — modified with `_load_backupignore_spec`, `_resolve_watch_root`, updated `_scan_current_files`
- main.py — modified with `ensure_lifecycle_rule` call and credential_source log
- tests/unit/test_file_listener.py — extended with `TestBackupignoreCascade`
- tests/unit/test_main_lifecycle_wiring.py — created with `TestStartupWiring`

## Self-Check: PASSED
