# Project Research Summary

**Project:** aws-copier
**Domain:** Async Python S3 backup daemon
**Researched:** 2026-04-24
**Confidence:** HIGH

## Executive Summary

aws-copier is a working personal backup daemon with a sound architecture — async Python 3.11, aiobotocore for non-blocking S3 I/O, watchdog for filesystem events — but it contains a cluster of correctness bugs that collectively mean the tool silently fails to deliver its core value: reliable, complete file sync. The most critical failure is the thread bridge between watchdog and asyncio: `call_soon_threadsafe(asyncio.create_task, coro)` is the canonical wrong-loop anti-pattern. On Python 3.10+ it raises `RuntimeError` in the watchdog thread and drops all real-time events silently. The second critical failure is the serial upload loop, where tasks are `await`ed one-by-one despite being created concurrently, giving effective concurrency of 1 regardless of semaphore or config settings.

Three additional correctness holes compound the reliability gap: glob patterns in `ignore_patterns` are never evaluated as globs (exact string membership only), so `*.pyc` and `*.bak` files are uploaded. Hidden dot-prefix files — including `.env`, `.npmrc`, and SSH keys — are not filtered, so credentials in watched folders go to S3. And `_setup_signal_handlers()` is commented out, meaning SIGTERM or SIGINT kills the process mid-multipart-upload, leaving orphaned parts in S3 that accumulate storage costs indefinitely.

The recommended approach is a single focused milestone that ships all six critical reliability fixes together, bundles the trivial low-effort improvements (counter fix, CLI entrypoint, semaphore wiring, dead code removal) into the same PR, and defers the performance and polish work (debouncing, credential chain, `.backupignore`, in-memory state cache) to a follow-on milestone. All six critical fixes are low complexity, well-researched, and have clear canonical patterns from the Python 3.11 official docs.

---

## Key Findings

### Stack Analysis

The existing stack choices are correct. aiobotocore over boto3 is the right call for an async daemon — boto3 would block the event loop on every S3 call. The `AsyncExitStack` pattern for the long-lived S3 client is the recommended aiobotocore maintainer pattern ("keep as few clients as possible for as long as possible"). aiobotocore 3.0 introduced a hard restriction against using a client after its context exits; the current code's `AsyncExitStack` approach correctly sidesteps this.

The bugs are not architecture problems — they are Python 3.11 asyncio API misuses that have existed since the codebase was written on an earlier Python version:

**Core technologies:**
- `asyncio.run_coroutine_threadsafe(coro, loop)` — required API for thread-to-loop coroutine scheduling; `call_soon_threadsafe(create_task, coro)` is the documented anti-pattern
- `asyncio.gather(*tasks, return_exceptions=True)` — correct primitive for parallel best-effort batch uploads; `TaskGroup` is wrong here because one failure should not cancel the rest
- `aiofiles.open(...)` — already a project dependency, already used in `_calculate_md5`; two methods still use blocking `open()` by oversight
- `asyncio.get_running_loop()` — replaces deprecated `asyncio.get_event_loop()` in Python 3.10+; or use `asyncio.to_thread()` to avoid needing the loop object at all
- `loop.add_signal_handler` (Unix) / `loop.call_soon_threadsafe` (Windows) — the only asyncio-safe signal APIs; `signal.signal` with `asyncio.create_task` is not safe
- `asyncio.AbstractEventLoop` — correct type annotation; `asyncio.BaseEventLoop` is deprecated since Python 3.10
- aiobotocore `get_session` import path `from aiobotocore.session import get_session` — remains valid in aiobotocore 3.5; the removed name was the top-level `aiobotocore.get_session`

**Version note:** `pytest-asyncio>=1.1.0` in `pyproject.toml` references a non-existent version; the effective constraint is the `>=0.21.0` line. Not a runtime issue. `asyncio_mode = "auto"` is the correct configuration for this codebase.

### Feature Priority

Research confirms a clean two-tier split. The six critical reliability fixes are not feature additions — they are correctness gaps where the tool appears to succeed but silently does the wrong thing.

**Critical reliability fixes (tool is broken without these):**
- Fix 1.1 — thread-to-async handoff: `run_coroutine_threadsafe` replaces wrong-loop anti-pattern
- Fix 1.2 — serial upload loop: `asyncio.gather` with `return_exceptions=True`
- Fix 1.3 — glob pattern matching: `fnmatch.fnmatch` for patterns containing `*`
- Fix 1.4 — hidden file / sensitive file deny-list: dot-prefix files blocked; hardcoded deny-list for `.env`, `*.pem`, `id_rsa`, etc.
- Fix 1.5 — signal handling: `loop.add_signal_handler` (Unix) with Windows fallback via `call_soon_threadsafe`
- Fix 1.6 — unified ignore patterns: shared `IgnoreRules` frozen dataclass consumed by both `FileListener` and `FileChangeHandler`

**High value, low effort (bundle into same milestone):**
- UX 2.3 — `ignored_files` counter: increment in `_scan_current_files` when file is ignored
- UX 2.4 — CLI entrypoint: fix `pyproject.toml` to point to `main:sync_main`
- Config 3.1 — wire semaphore to config: `asyncio.Semaphore(config.max_concurrent_uploads)`
- Config 3.5 — remove `discovered_files_folder` dead code
- Config 3.6 — move `ruff` and `python-dotenv` to dev dependency group
- Fix 1.7 — `aiofiles` for `_load_backup_info` / `_update_backup_info`

**Defer to milestone 2:**
- Config 3.2 — credential chain (support `~/.aws/credentials`; no correctness impact)
- Config 3.3 — `.backupignore` file support via `pathspec` library
- UX 2.1 — event debouncing (requires Fix 1.1 to be meaningful)
- UX 2.2 — upload progress for large files
- UX 2.5 — in-memory backup state cache between scan cycles
- UX 2.6 — deduplicate MD5 computation (computed twice per upload currently)
- Config 3.4 — multipart lifecycle rule check at startup

**Out of scope (confirmed):**
Versioning, deduplication, client-side encryption, restore functionality, web UI, multi-user, multi-cloud, scheduling/cron, upload resume with checkpoints. S3 versioning and SSE handle the first two at the bucket level with no code changes needed.

### Architecture Approach

All five architectural concerns have clear, low-complexity solutions. None require structural redesign — they are targeted fixes to specific methods and one new shared module. The key architectural addition is extracting ignore logic into a single `aws_copier/core/ignore_rules.py` module with a frozen `IgnoreRules` dataclass; this is the foundation that fixes Fix 1.3, Fix 1.4, Fix 1.6, and UX 2.3 simultaneously.

**Major components and their changes:**
1. `aws_copier/core/ignore_rules.py` (new) — frozen `IgnoreRules` dataclass with `should_ignore_file` / `should_ignore_directory` using `fnmatch`; `DEFAULT_IGNORE_RULES` constant shared by both classes
2. `FileChangeHandler.on_any_event` — one-line fix: `run_coroutine_threadsafe(coro, self.event_loop)`; fix type annotation to `AbstractEventLoop`
3. `FileListener._upload_files` — replace serial `for` loop with `asyncio.gather(*tasks, return_exceptions=True)`; inspect results per-item after gather
4. `FileListener._load_backup_info` / `_update_backup_info` — replace `open()` with `async with aiofiles.open(...)`; note `json.load(f)` does not work with aiofiles — must `await f.read()` then `json.loads()`
5. `AWSCopierApp._setup_signal_handlers` — implement `loop.add_signal_handler` (Unix) with `signal.signal` + `call_soon_threadsafe` Windows fallback; uncomment the call in `start()`

**Pitfall-driven addition:** Per-folder `asyncio.Lock` is required when switching to `aiofiles` for info file writes. Two concurrent coroutines (real-time watchdog event + scheduled 5-minute scan) can race on `_update_backup_info` for the same folder. Without the lock, the second write overwrites the first. Combined with atomic write-then-rename (`tmp.replace(target)`) this prevents both race corruption and truncation-on-crash.

### Critical Pitfalls

1. **Silent gather exception swallowing** — `asyncio.gather(return_exceptions=True)` collects exceptions as return values; if the result list is not inspected per-item, all upload failures are swallowed silently. Always pair with a result-inspection loop that logs per-file failures and updates stats.

2. **Windows crash on signal handler re-enable** — `loop.add_signal_handler` raises `NotImplementedError` on Windows (ProactorEventLoop does not implement it). Enabling signal handling without a platform guard (`sys.platform == "win32"` or try/except `NotImplementedError`) makes the daemon unlaunchable on Windows.

3. **Sensitive file leakage via dot-file asymmetry** — `_should_ignore_directory` blocks dot-prefix dirs but `_should_ignore_file` does not block dot-prefix files. `.env`, SSH keys, and AWS credential files in watched folders are uploaded to S3. Fix requires explicit dot-file deny in `should_ignore_file` plus a hardcoded sensitive-file deny-list that cannot be overridden by config.

4. **Race condition on backup info file write** — with async I/O, two coroutines can interleave at `await` points inside `_update_backup_info`. A per-folder `asyncio.Lock` and atomic write-then-rename are both required when introducing `aiofiles`. Omitting either creates a window for data corruption or silent state loss.

5. **Orphaned S3 multipart parts** — process kill during multipart upload leaves parts in S3 at full storage cost indefinitely. Two defenses required: `abort_multipart_upload` in a `finally` block in code (already present), and an `AbortIncompleteMultipartUpload` lifecycle rule on the bucket (not yet set). The lifecycle rule is the only protection against SIGKILL.

---

## Implications for Roadmap

### Phase 1: Core Reliability Fixes

**Rationale:** Six active requirements are correctness gaps where the tool silently fails its core promise. They share the same theme (async correctness), are mutually independent at the code level, and are all low complexity. Shipping them together closes the reliability gap in a single milestone.

**Delivers:** A backup daemon that actually uploads files in real time, actually runs uploads concurrently, actually respects ignore patterns, does not leak credentials to S3, and shuts down cleanly.

**Addresses:** Fix 1.1, 1.2, 1.3, 1.4, 1.5, 1.6 from FEATURES.md

**Avoids:** Pitfalls 1, 3, 4, 8 from PITFALLS.md

**Implementation sequence within the phase:**
1. Create `ignore_rules.py` (IgnoreRules dataclass) — unblocks 1.3, 1.4, 1.6, and the counter fix
2. Fix `run_coroutine_threadsafe` thread bridge — active crash risk, one-line change
3. Fix `asyncio.gather` serial loop — requires gather + result-inspection loop
4. Fix `aiofiles` for backup info — add per-folder lock + atomic write at the same time
5. Re-enable signal handling with platform guard — do last so shutdown path exercises the fixed gather

**Bundle with (same PR):** UX 2.3, UX 2.4, Config 3.1, Config 3.5, Config 3.6, Fix 1.7, `asyncio.get_event_loop()` to `get_running_loop()` / `to_thread()` fix, `BaseEventLoop` to `AbstractEventLoop` annotation fix

### Phase 2: Polish and Performance

**Rationale:** With reliability established, this phase improves daily-use quality and performance. All items here have the Phase 1 fixes as a prerequisite (debounce requires the thread bridge fix; in-memory cache is only meaningful after async I/O is correct; credential chain is lower-risk once the tool is proven reliable).

**Delivers:** Faster scans, better operator visibility, credential hygiene, and per-directory ignore customization.

**Addresses:** UX 2.1, 2.2, 2.5, 2.6; Config 3.2, 3.3, 3.4

**Avoids:** Pitfall 7 (multipart lifecycle rule check — Config 3.4)

**Key dependency:** Config 3.3 (`.backupignore`) requires Fix 1.3 (glob matching) and Fix 1.6 (unified ignore rules) from Phase 1 before it is correct. Do not attempt `.backupignore` without the `IgnoreRules` module in place.

### Phase Ordering Rationale

- Phase 1 before Phase 2: reliability before polish is the only defensible order for a backup tool. A fast daemon that leaks credentials is worse than a slow daemon that does not.
- `IgnoreRules` module is the first task in Phase 1 because it is a prerequisite for correctly implementing and testing three other fixes in the same phase.
- Signal handling is the last task in Phase 1 because verifying clean shutdown requires the gather fix to be correct first (SIGTERM should drain in-flight gather tasks, not cancel them mid-flight).
- Per-folder `asyncio.Lock` must be introduced simultaneously with the `aiofiles` change — they address the same race, and introducing async I/O without the lock makes the race more likely, not less.

### Research Flags

Phases with well-documented patterns (research already complete, skip phase research):
- **Phase 1:** All fixes have authoritative Python 3.11 doc references and verified code patterns. Implementation can proceed directly.
- **Phase 2 (UX 2.1 debounce):** Standard per-path timer dict pattern; no external research needed.
- **Phase 2 (Config 3.2 credential chain):** boto3 credential provider chain is fully documented; straightforward conditional credential passing.

Phases that may benefit from light research before planning:
- **Phase 2 (Config 3.3 `.backupignore`):** `pathspec` library API should be reviewed at implementation time; gitignore semantics (negation, `**`) have edge cases worth checking against the library's test suite.

---

## Confidence Assessment

| Area | Confidence | Notes |
|------|------------|-------|
| Stack | HIGH | All patterns verified against Python 3.11 official docs; aiobotocore patterns from maintainer discussions |
| Features | HIGH | Directly grounded in codebase analysis via CONCERNS.md; no speculation |
| Architecture | HIGH | All five concerns have code-level solutions verified against official docs |
| Pitfalls | HIGH | Confirmed by official docs + AWS billing docs; Windows signal pitfall is documented behavior |

**Overall confidence:** HIGH

### Gaps to Address

- **Credential chain (Config 3.2):** Current design stores credentials in plaintext YAML. The improvement is clear but not urgent — it has no correctness impact and the personal-use threat model is low. Flag for Phase 2 validation: confirm aiobotocore passes through the full provider chain when credentials are omitted from session kwargs.
- **In-memory cache invalidation (UX 2.5):** The mtime-based cache approach is straightforward, but the interaction with `_update_backup_info` writes must be tested carefully — the cache entry must be updated (not just invalidated) after a write to avoid a re-read on the next cycle. Handle at implementation time, not during planning.
- **GUI mode signal handling:** `main_gui.py` line 160 has a separate signal handler that runs before the background loop starts. This is a fragile area noted in CONCERNS.md but is not in the active requirements list. It can be addressed in Phase 1 as a low-risk addition or deferred — the GUI signal path is less critical than the headless daemon path.

---

## Sources

### Primary (HIGH confidence)
- Python 3.11 asyncio docs — `run_coroutine_threadsafe`, `gather`, `get_running_loop`, `add_signal_handler`: https://docs.python.org/3.11/library/asyncio-task.html
- Python 3.11 asyncio dev guide — thread safety: https://docs.python.org/3.11/library/asyncio-dev.html
- aiobotocore tutorial — session and client lifecycle: https://aiobotocore.aio-libs.org/en/latest/tutorial.html
- aiobotocore maintainer discussion — "keep as few clients as possible": https://github.com/aio-libs/aiobotocore/discussions/1105
- AWS docs — AbortIncompleteMultipartUpload lifecycle: https://docs.aws.amazon.com/AmazonS3/latest/userguide/mpu-abort-incomplete-mpu-lifecycle-config.html
- AWS blog — multipart upload cost from orphaned parts: https://aws.amazon.com/blogs/aws-cloud-financial-management/discovering-and-deleting-incomplete-multipart-uploads-to-lower-amazon-s3-costs/
- aiofiles library: https://github.com/Tinche/aiofiles

### Secondary (MEDIUM confidence)
- roguelynn.com — asyncio graceful shutdown patterns: https://roguelynn.com/words/asyncio-graceful-shutdowns/
- pathspec on PyPI — gitignore-style matching: https://pypi.org/project/pathspec/
- boto3 credentials guide — provider chain: https://boto3.amazonaws.com/v1/documentation/api/latest/guide/credentials.html

---
*Research completed: 2026-04-24*
*Ready for roadmap: yes*
