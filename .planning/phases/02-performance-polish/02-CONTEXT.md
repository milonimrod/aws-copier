# Phase 2: Performance & Polish - Context

**Gathered:** 2026-04-25
**Status:** Ready for planning

<domain>
## Phase Boundary

Add scan-time optimisations (mtime skip, in-memory cache, deduplicated MD5), credential chain support, per-directory `.backupignore` files, and an S3 lifecycle rule for multipart cleanup. All work is on the daemon backend — no UI changes, no new external capabilities.

</domain>

<decisions>
## Implementation Decisions

### Backup Info Format Migration (PERF-01)

- **D-01:** Migrate `.milo_backup.info` silently in-place. Old string entries (`{filename: "md5hash"}`) are read as `{md5: value, mtime: 0.0}`. `mtime: 0.0` forces one re-stat per file on the first run after migration, then the new format takes over. No data loss, no re-upload.
- **D-02:** After a successful upload, store `st_mtime` captured just before the upload call (not at scan start). This ensures that a file modified during upload is detected on the next cycle.
- **D-03:** New format: `{filename: {md5: "...", mtime: <float>}}`. Backward-compatible read: if a value is a plain string, treat it as the old format and apply D-01 migration on read; write always uses the new dict format.

### In-Memory Backup Info Cache (PERF-02)

- **D-04:** Cache `existing_backup_info` per folder in `FileListener` instance memory between scan cycles. Re-read `.milo_backup.info` from disk only when the file's `st_mtime` changes (check via `os.stat` before loading). This avoids O(n) disk reads per 5-minute cycle for unchanged folders.

### MD5 Deduplication (PERF-03)

- **D-05:** Compute MD5 once in `FileListener._upload_single_file`; pass the pre-computed hash to `S3Manager.upload_file` as a new optional parameter `precomputed_md5: Optional[str] = None`. When provided, `upload_file` skips its internal `_calculate_md5` call.

### Event Debounce (PERF-04)

- **D-06:** Debounce per-path with a 2-second timer using `asyncio.create_task` + `asyncio.sleep(2)`. If a new event arrives for the same path before the timer fires, cancel the pending task and start a fresh one. This is Claude's discretion to implement — the 2-second window and per-path keying are fixed by the requirement.

### .backupignore Scope (CONFIG-06)

- **D-07:** `.backupignore` patterns cascade into subdirectories — a file in `/photos/` applies to `/photos/2024/`, `/photos/raw/`, etc. This matches `.gitignore` semantics and the `pathspec` library's standard usage.
- **D-08:** Parent-directory `.backupignore` rules inherit downward. Rules accumulate as the tree is descended — a child directory's `.backupignore` adds to (not replaces) ancestor rules. Evaluated in path order: root → parent → child.

### Credential Fallback (CONFIG-05)

- **D-09:** Fall back to the AWS provider chain only when `aws_access_key_id` or `aws_secret_access_key` are absent from `config.yaml` entirely. If the keys are present (even as placeholder strings), use them as-is — explicit config always wins.
- **D-10:** At startup, emit `logger.info("AWS credentials loaded from: {source}")` where `{source}` is one of `"config.yaml"`, `"environment variables"`, or `"~/.aws/credentials"`. Clear audit trail for auth failures.

### S3 Lifecycle Rule (CONFIG-07)

- **D-11:** If the lifecycle rule cannot be set or verified at startup (permission denied, API error), log `logger.warning("Could not verify multipart lifecycle rule: {error}. Incomplete uploads may accumulate cost.")` and continue. The daemon is usable without this protection — do not abort startup.
- **D-12:** If an `AbortIncompleteMultipartUpload` rule already exists on the bucket (any `DaysAfterInitiation` value), log `logger.info("S3 lifecycle rule already present (DaysAfterInitiation={N}). Skipping.")` and leave it untouched. Never overwrite externally-set rules.

### Testing Strategy
- **D-13:** Carry forward Phase 1 approach — each requirement gets a behavior-proving test. Key tests: mtime skip reduces MD5 calls on unchanged files; debounce collapses rapid events into one call; `.backupignore` excludes matched files; credential chain fallback is exercised with and without config keys present.

### Claude's Discretion
- Exact asyncio mechanism for debounce timer (asyncio.create_task + asyncio.sleep vs. `asyncio.TimerHandle`)
- Internal data structure for per-path debounce state in `FolderWatcher` (dict keyed by path string)
- Whether the in-memory backup info cache is a class-level dict or instance dict in `FileListener`
- `pathspec` version constraint and import pattern
- S3 API call used to read/write lifecycle rules (`get_bucket_lifecycle_configuration` / `put_bucket_lifecycle_configuration`)

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Phase Requirements
- `.planning/REQUIREMENTS.md` — v2 requirement specs: PERF-01 through PERF-04, CONFIG-05, CONFIG-06, CONFIG-07

### Files Being Modified
- `aws_copier/core/file_listener.py` — backup info load/update, MD5 computation, upload loop (PERF-01, PERF-02, PERF-03)
- `aws_copier/core/folder_watcher.py` — real-time event handler, debounce goes here (PERF-04)
- `aws_copier/core/s3_manager.py` — `upload_file` signature extension, lifecycle rule check (PERF-03, CONFIG-07)
- `aws_copier/models/simple_config.py` — credential loading logic (CONFIG-05)

### Phase 1 Context (patterns to follow)
- `.planning/phases/01-core-reliability/01-CONTEXT.md` — established patterns: IgnoreRules singleton, per-folder asyncio.Lock, Google-style docstrings, try/except Exception wrapping

### Test Infrastructure
- `tests/` — pytest-asyncio (asyncio_mode = "auto") + moto[s3]; new tests go here

No external specs — requirements fully captured in decisions above and REQUIREMENTS.md.

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `FileListener._get_folder_lock(folder_path)` — per-folder asyncio.Lock registry; extend to protect the in-memory cache dict as well (PERF-02)
- `FileListener._load_backup_info` / `_update_backup_info` — already use `aiofiles`; extend these methods to handle the new dict format and migration (PERF-01)
- `FileListener._calculate_md5` — existing async MD5 computation; result flows into `_upload_single_file` which calls `s3_manager.upload_file`; thread the hash through (PERF-03)
- `asyncio.Semaphore` already present for uploads and MD5; debounce dict in `FolderWatcher` needs no semaphore — it's always accessed from the asyncio event loop thread

### Established Patterns
- All async methods: `try/except Exception as e` + `logger.error(f"...")` — maintain throughout
- Private methods: single underscore prefix; new helpers follow this convention
- Google-style docstrings with `Args:` and `Returns:` on all methods
- `asyncio.wait_for(..., timeout=300)` wraps individual file uploads — keep this pattern

### Integration Points
- `S3Manager.upload_file` signature: add `precomputed_md5: Optional[str] = None` parameter (backward-compatible)
- `SimpleConfig.__init__`: add credential chain detection logic after loading YAML fields
- `FolderWatcher.FileChangeHandler.on_any_event` → debounce dict → delayed `_process_current_folder` call
- `main.py` `AWSCopierApp.run()`: add lifecycle rule check call after S3Manager initialises

</code_context>

<specifics>
## Specific Ideas

No specific references — implementation fully defined by REQUIREMENTS.md and decisions above.

</specifics>

<deferred>
## Deferred Ideas

None — discussion stayed within phase scope.

</deferred>

---

*Phase: 02-performance-polish*
*Context gathered: 2026-04-25*
