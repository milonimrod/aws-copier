---
status: partial
phase: 02-performance-polish
source: [02-01-SUMMARY.md, 02-02-SUMMARY.md, 02-03-SUMMARY.md, 02-04-SUMMARY.md, 02-05-SUMMARY.md]
started: 2026-04-26T06:10:00Z
updated: 2026-04-26T06:11:00Z
---

## Current Test

<!-- OVERWRITE each test - shows where we are -->

[testing paused — 3 items skipped by user request]

## Tests

### 1. Cold Start Smoke Test
expected: Kill any running aws-copier instance. Start the app from scratch with `uv run python main.py`. The app boots without errors, the initial scan completes, and a credential source log line appears (e.g. "AWS credentials loaded from: config.yaml" or "provider chain (...)"). No crash on startup.
result: pass

### 2. Startup credential source log (D-10)
expected: In the startup logs (before any S3 upload activity), there is a line that says either "AWS credentials loaded from: config.yaml" (when explicit keys are configured) or "AWS credentials loaded from: provider chain (env / ~/.aws/credentials / IAM)" (when keys are absent). The log appears between the S3 initialize step and the first scan.
result: pass

### 3. Lifecycle rule log at startup (CONFIG-07)
expected: In the startup logs, there is a line about the S3 lifecycle rule — either "S3 lifecycle rule set" (first time / no existing lifecycle config), "already present (DaysAfterInitiation=N)" (rule exists), or a warning like "Could not verify..." (permission denied). The daemon starts successfully regardless of which branch is taken.
result: pass

### 4. mtime-skip on unchanged files (PERF-01/02)
expected: After the initial scan completes with files uploaded, wait a moment and trigger a second scan (or restart the app). The log shows skipped_files incrementing for files that haven't changed — no re-upload occurs. The .milo_backup.info file in the watched folder now contains dict entries with both md5 and mtime fields (e.g. `{"file.txt": {"md5": "abc123", "mtime": 1234567890.0}}`).
result: skipped
reason: user chose to skip remaining tests and open PR

### 5. .backupignore filtering (CONFIG-06)
expected: Create a file named `.backupignore` in a watched folder containing the line `*.tmp`. Add a file called `test.tmp` to that folder. On the next scan, the `.tmp` file is NOT uploaded (no log entry for it, or a skipped/ignored count increments). A non-matching file like `test.txt` in the same folder IS uploaded normally.
result: skipped
reason: user chose to skip remaining tests and open PR

### 6. Event debounce on rapid saves (PERF-04)
expected: While the watcher is running, rapidly save the same file 3 times within 1 second (e.g. `echo a > watched/test.txt; echo b > watched/test.txt; echo c > watched/test.txt`). Only ONE upload is triggered after a ~2-second pause. The log shows one _process_changed_file call, not three.
result: skipped
reason: user chose to skip remaining tests and open PR

## Summary

total: 6
passed: 3
issues: 0
pending: 0
skipped: 3
blocked: 0

## Gaps

[none yet]
