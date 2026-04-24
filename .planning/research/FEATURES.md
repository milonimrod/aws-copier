# Feature Landscape

**Domain:** Personal async Python S3 backup daemon
**Researched:** 2026-04-24
**Overall confidence:** HIGH (grounded in codebase analysis + CONCERNS.md + verified external patterns)

---

## Context

This is a subsequent-milestone research pass. The tool exists and runs. The research question
is: given the known bugs and the gap between what the tool does today and what personal backup
tools need to do reliably, what should be built next and in what order?

The CONCERNS.md analysis already identified every concrete code-level bug. This document maps
those bugs plus ecosystem expectations onto four categories:

1. **Critical reliability fixes** — the tool silently fails without these
2. **UX improvements** — the tool works, but daily use is rough without these
3. **Configuration improvements** — makes the tool more flexible
4. **Out of scope / anti-features** — not worth building for a personal tool

---

## Category 1: Critical Reliability Fixes

These are bugs where the tool appears to succeed but silently does the wrong thing.
Skipping any of these means the backup guarantee ("files are reliably synced") is not met.

### Fix 1.1 — Thread-to-asyncio handoff in real-time watcher

**What goes wrong:** `call_soon_threadsafe(asyncio.create_task, coro)` schedules
`asyncio.create_task` on the wrong loop. Any file change event triggers a
`RuntimeError: no running event loop` or silently creates a task on the default loop
(which is not running), so real-time uploads never happen.

**Consequence:** The watcher appears active but file changes are dropped entirely.
**Complexity:** Low. One-line fix: replace with `asyncio.run_coroutine_threadsafe(coro, loop)`.
**Source:** CONCERNS.md — "call_soon_threadsafe anti-pattern"

---

### Fix 1.2 — Serial upload loop defeating concurrency

**What goes wrong:** Tasks are created for all files, but then awaited one-by-one in a
`for` loop. Effective concurrency is 1 regardless of semaphore size or config value.

**Consequence:** Large batch uploads are dramatically slower than designed. The 5-minute
scan loop may not complete before the next one starts, causing backlog buildup.
**Complexity:** Low. Replace the serial loop with `asyncio.gather(*tasks, return_exceptions=True)`.
**Source:** CONCERNS.md — "Upload proceeds serially"

---

### Fix 1.3 — Glob patterns never matched as globs

**What goes wrong:** `*.pyc`, `*.bak`, `*.backup`, `*~` in `ignore_patterns` are tested
with exact set membership, not `fnmatch`. Files like `cache.pyc` or `notes.bak` are uploaded.

**Consequence:** Files explicitly marked for exclusion are silently backed up to S3.
**Complexity:** Low. Add `fnmatch.fnmatch(filename, pattern)` for patterns containing `*`.
Use `any(fnmatch.fnmatch(name, p) for p in glob_patterns)`.
**Source:** CONCERNS.md — "Glob patterns never matched as globs"

---

### Fix 1.4 — Hidden files (`.env`, `.npmrc`, SSH keys) uploaded to S3

**What goes wrong:** `_should_ignore_file` does not ignore dot-prefix files, while
`_should_ignore_directory` does skip dot-prefix dirs. The asymmetry means `.env`, `.npmrc`,
`id_rsa`, `.aws/credentials` inside a watched folder are backed up to S3.

**Consequence:** Credential files are exfiltrated to S3. This is both a security risk and
the kind of silent failure that erodes trust in the tool permanently once discovered.
**Complexity:** Low for basic fix (add dot-file check). Medium if adding a configurable
sensitive-file deny-list.

Default deny-list to add (HIGH value, zero config required):
```
.env  .env.*  *.pem  *.key  id_rsa  id_ed25519  .npmrc  .netrc
.aws/credentials  .aws/config  *.p12  *.pfx  *.keystore
```

**Source:** CONCERNS.md — "Sensitive file paths silently uploaded"

---

### Fix 1.5 — Re-enable signal handling in headless mode

**What goes wrong:** `_setup_signal_handlers()` is defined but the call is commented out in
`main.py`. SIGTERM and SIGINT kill the process immediately with no cleanup.

**Consequence:** Any in-progress multipart upload is abandoned mid-flight. The incomplete
upload parts stay in S3 and incur storage costs indefinitely (AWS confirmed: incomplete
multipart parts are billed until explicitly aborted or a lifecycle rule cleans them up).

The correct pattern (HIGH confidence, from asyncio documentation and community posts):
```python
for sig in (signal.SIGTERM, signal.SIGINT):
    loop.add_signal_handler(sig, lambda s=sig: asyncio.create_task(shutdown(s, loop)))

async def shutdown(sig, loop):
    # Wait for in-progress uploads to finish (or timeout)
    tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    [task.cancel() for task in tasks]
    await asyncio.gather(*tasks, return_exceptions=True)
    loop.stop()
```

`loop.add_signal_handler` is the asyncio-safe variant; `signal.signal` is not safe to
call from a running event loop on all platforms.

**Complexity:** Low-medium. The method exists; it needs to be un-commented and the
implementation switched from `signal.signal` to `loop.add_signal_handler`.
**Source:** CONCERNS.md; roguelynn.com asyncio graceful shutdown article

---

### Fix 1.6 — Duplicate, divergent ignore patterns across classes

**What goes wrong:** `FileListener` and `FileChangeHandler` each maintain their own
`ignore_patterns` sets that have already diverged. A file may be ignored by the batch
scanner but uploaded by the real-time watcher (or vice versa).

**Consequence:** Inconsistent backup behavior depending on whether a file was there at
startup or changed after — a fundamental correctness gap.
**Complexity:** Low. Extract into a shared `IgnoreConfig` dataclass or module-level
constant consumed by both classes.
**Source:** CONCERNS.md — "Duplicate ignore pattern sets"

---

### Fix 1.7 — Sync file I/O inside async methods blocks the event loop

**What goes wrong:** `_load_backup_info` and `_update_backup_info` are declared `async`
but use synchronous `open()`. On any non-SSD path (network mount, slow HDD), this blocks
the entire event loop during every scan cycle, stalling all concurrent uploads.

**Consequence:** The async model provides no benefit during state persistence; uploads
stall for the duration of every metadata read/write.
**Complexity:** Low. `aiofiles` is already a dependency — replace `open()` with `async with aiofiles.open(...)`.
**Source:** CONCERNS.md — "sync file I/O in async methods"

---

## Category 2: UX Improvements

These improve the daily-use experience. The tool works without them, but they matter for
trust and usability over months of continuous use.

### UX 2.1 — Debounce real-time file change events

**What goes wrong:** Applications that do atomic-save (write to temp file, then rename)
generate 2-3 filesystem events per save. A rapidly-edited file triggers full folder scans
repeatedly within seconds.

**Why it matters:** Most editors (VS Code, JetBrains, vim, nano) use atomic-save. Without
debouncing, every keystroke-save combination generates redundant S3 `HeadObject` calls
and potentially redundant uploads.

**Standard pattern:** Queue events with a short debounce window (2-5 seconds) and process
only the most recent event per path.
**Complexity:** Medium. Requires a per-path timer dict and task cancellation/restart on new events.
**Source:** CONCERNS.md — "No debouncing on real-time file change events"

---

### UX 2.2 — Upload progress for large files

**What goes wrong:** Multipart uploads of files >100MB run silently. There is no progress
feedback — the daemon logs show the upload started but nothing until it completes (or fails).

**Why it matters:** Users with large files (video, database dumps) have no idea if the
tool is stuck or working. This is a common complaint with backup tools that do silent
large-file transfers.

**Standard pattern:** Use the `Callback` parameter in `upload_fileobj` / `put_object` to
report bytes transferred. Log progress at 25%/50%/75%/100% for files over a configurable
threshold (e.g., 50MB).
**Complexity:** Low-medium. aiobotocore supports progress callbacks on uploads.

---

### UX 2.3 — Actionable status output (fix `ignored_files` counter)

**What goes wrong:** `_stats["ignored_files"]` is initialized but never incremented. The
5-minute status loop always reports `ignored_files: 0`, making it useless for diagnosing
whether the ignore rules are working.

**Why it matters:** A user who adds `.env` to their watch folder cannot tell from the
status output whether it is being ignored or uploaded.
**Complexity:** Trivial. Increment the counter in `_scan_current_files` when `_should_ignore_file` returns True.
**Source:** CONCERNS.md — "`ignored_files` counter never incremented"

---

### UX 2.4 — Fix the `aws-copier` CLI entrypoint

**What goes wrong:** `pyproject.toml` declares `aws-copier = "simple_main:main"` but
`simple_main.py` does not exist. `uv run aws-copier` fails with `ModuleNotFoundError`.

**Why it matters:** This is the primary install-time entry point. A broken entrypoint
means new installs cannot run the tool at all via the advertised command.
**Complexity:** Trivial. Change to `main:sync_main` (headless) or add both headless and GUI entries.
**Source:** CONCERNS.md — "Broken script entrypoint"

---

### UX 2.5 — In-memory cache of backup state between scan cycles

**What goes wrong:** Every 5-minute scan reads the entire `.milo_backup.info` JSON for
every watched folder, recomputes all MD5s, and rewrites the entire JSON. For folders
with thousands of files, this is O(n) disk I/O per cycle.

**Why it matters:** Tools like rclone and restic are known for being fast because they
avoid redundant state reads. This pattern will become noticeably slow on large watched
folders and wastes battery on laptops.
**Complexity:** Medium. Keep `existing_backup_info` dicts in memory between scans; only
flush to disk when entries actually change.
**Source:** CONCERNS.md — "Backup info file read and rewritten every scan"

---

### UX 2.6 — Eliminate duplicate MD5 computation per upload

**What goes wrong:** `_upload_single_file` calls `_calculate_md5`, then `s3_manager.upload_file`
calls `_calculate_md5` again internally. Every uploaded file is hashed twice.

**Why it matters:** For large files, MD5 computation is non-trivial. Doubling it is pure waste.
**Complexity:** Low. Pass the already-computed MD5 to `upload_file` as a parameter.
**Source:** CONCERNS.md — "MD5 computed twice per upload"

---

## Category 3: Configuration Improvements

These make the tool more flexible without adding operational complexity for a personal tool.

### Config 3.1 — Wire `max_concurrent_uploads` config to the semaphore

**What goes wrong:** `SimpleConfig.max_concurrent_uploads` defaults to 100 and appears
in `config.yaml`, but `FileListener` hardcodes both semaphores to 50. The config field
has no effect and gives false confidence.

**Complexity:** Trivial. `upload_semaphore = asyncio.Semaphore(config.max_concurrent_uploads)`.
**Source:** CONCERNS.md — "Semaphore limits hardcoded"

---

### Config 3.2 — AWS credential chain support (env vars + `~/.aws/credentials`)

**What goes wrong:** Credentials are stored in plaintext `config.yaml` and serialized by
`SimpleConfig.save_to_yaml`. The credential chain (env vars, `~/.aws/credentials`) is not
consulted.

**Why it matters:** boto3/aiobotocore support the full AWS credential provider chain natively
(env vars → `~/.aws/credentials` → `~/.aws/config` → IAM role). Supporting this requires
zero code for the common case — just stop requiring credentials in the YAML and let aiobotocore
fall through its chain. Users with `~/.aws/credentials` already configured (common for anyone
using the AWS CLI) would then need no credential config at all.

**Recommended approach:**
1. Make `aws_access_key_id` and `aws_secret_access_key` optional in `SimpleConfig`
2. When absent, do not pass them to the aiobotocore session — it will find credentials automatically
3. Document that `config.yaml` with credentials must have `chmod 600`

**Complexity:** Low-medium. Conditional credential passing to the aiobotocore session.

---

### Config 3.3 — `.backupignore` file support (gitignore-style per-directory rules)

**What goes wrong:** The current ignore rules are global (set in code/config), with no
per-directory override. Users cannot say "ignore `node_modules/` in this project but not
in others."

**Why users expect this:** Every mature file-sync and backup tool (rclone, rsync, restic,
git) supports per-directory ignore files. Users who already have `.gitignore` files in
projects reasonably expect their backup tool to respect them.

**Standard library:** `pathspec` (PyPI) is the de facto Python library for gitignore-style
matching. It implements Git's wildmatch spec including `**`, negation patterns, and
directory-specific matching — things `fnmatch` does not handle. Used by pip, black, and
dozens of other major Python tools.

```python
from pathspec import PathSpec
spec = PathSpec.from_lines("gitwildmatch", open(".backupignore").readlines())
if spec.match_file(relative_path):
    # ignore
```

**Recommended scope for personal tool:** Support a `.backupignore` file in each watched
root directory. Reading a `.gitignore` file directly is an option but makes the semantics
less explicit for backup-specific rules (users may want to back up files that git ignores).

**Complexity:** Medium. Requires loading the ignore file per-directory at scan time and
caching it. Add `pathspec` as a dependency.

---

### Config 3.4 — S3 lifecycle rule documentation / auto-setup for multipart cleanup

**What goes wrong:** Interrupted multipart uploads accumulate in S3 and incur storage
costs with no cleanup. AWS confirmed: parts are billed until explicitly aborted or a
lifecycle rule is applied.

**Why it matters:** A personal user may not know about this. The tool's own behavior
(calling `abort_multipart_upload` on failure) is not sufficient if the process is killed
mid-upload (which happens because signal handling is disabled — see Fix 1.5).

**Recommended approach:** At startup, optionally check whether the target bucket has an
`AbortIncompleteMultipartUploads` lifecycle rule, and log a warning if not. Do not auto-create
rules without user consent. Document the one-time AWS CLI command to add it:

```bash
aws s3api put-bucket-lifecycle-configuration \
  --bucket my-bucket \
  --lifecycle-configuration '{"Rules":[{"ID":"abort-incomplete-mpu","Status":"Enabled","AbortIncompleteMultipartUpload":{"DaysAfterInitiation":1}}]}'
```

**Complexity:** Low. One `GetBucketLifecycleConfiguration` API call at startup.

---

### Config 3.5 — Remove dead `discovered_files_folder` config field

**What goes wrong:** `SimpleConfig` creates and stores a path for a directory that is
never read or written. The directory is created on disk, confusing users.

**Complexity:** Trivial. Remove the field and its serialization; update `config.yaml`.
**Source:** CONCERNS.md — "`discovered_files_folder` is dead code"

---

### Config 3.6 — Move dev dependencies to dev dependency group

**What goes wrong:** `ruff` is a runtime dependency (increases install size). `python-dotenv`
is a runtime dependency used only in a `if __name__ == "__main__"` dev harness.

**Complexity:** Trivial. Move both to `[dependency-groups].dev` in `pyproject.toml`. Extract
the `s3_manager.py` dev harness to a separate script.
**Source:** CONCERNS.md — "ruff listed as runtime dependency"

---

## Category 4: Out of Scope / Anti-Features

These are features that similar tools include but are explicitly not worth building for
a personal backup daemon. Building them adds complexity without commensurate value.

| Feature | Why Not |
|---------|---------|
| **Versioning / snapshot history** | S3 versioning can be enabled at the bucket level with zero code changes. The tool does not need to manage versions itself. Over-engineering for personal use. |
| **Deduplication (block-level or content-addressed)** | Restic and Borg do this. Meaningful for multi-TB archives. For a personal daemon watching source code and documents, S3 storage costs are negligible and dedup adds significant complexity and a format lock-in. |
| **Encryption at rest** | S3 SSE (server-side encryption) handles this with one bucket setting. Client-side encryption requires key management, which is more burden than benefit for a personal tool. |
| **Restore / download functionality** | AWS CLI and S3 console are sufficient for personal restores. A restore path doubles the surface area to maintain and test; restic is purpose-built for restore workflows if needed. |
| **Web UI or remote monitoring dashboard** | The project's own out-of-scope list includes this. The tkinter GUI and CLI status loop are sufficient for personal use. Adding a web server introduces a port, authentication, and a new attack surface. |
| **Multi-user or team features** | Explicitly out of scope per PROJECT.md. |
| **Multi-cloud or provider abstraction** | S3 only by design. rclone already solves multi-provider backup. |
| **Scheduling / cron orchestration** | Always-on daemon is the intended model. Users who want scheduled backups can use cron + a single-run mode if added, but this is not a priority. |
| **Retry/resume of interrupted uploads** | For personal use on stable home connections, exponential backoff on transient errors is sufficient. Full upload resume (checkpoint and continue) is complex and adds a persistent state store. The lifecycle rule (Config 3.4) is the right mitigation for the cost concern. |
| **SHA-256 as change-detection hash** | MD5 is cryptographically broken but collision attacks require intentional crafting. For a personal backup daemon with no adversarial write access to watched folders, MD5 is acceptable. The performance cost of switching to SHA-256 (slower, no hardware acceleration on many systems) is not justified. |

---

## Feature Dependencies

```
Fix 1.1 (thread bridge)      ← required before UX 2.1 (debounce) is meaningful
Fix 1.3 (glob matching)      ← prerequisite for Config 3.3 (.backupignore) to be useful
Fix 1.4 (hidden files)       ← prerequisite for Config 3.3 to be trusted
Fix 1.5 (signal handling)    ← prerequisite for Config 3.4 (multipart lifecycle) to matter
Fix 1.6 (duplicate patterns) ← prerequisite for Config 3.3 (single source of truth for ignores)
```

---

## MVP Recommendation for Next Milestone

**Must ship (critical reliability — tool is broken without these):**
1. Fix 1.1 — thread-to-async handoff (`asyncio.run_coroutine_threadsafe`)
2. Fix 1.2 — serial upload loop (`asyncio.gather`)
3. Fix 1.3 — glob pattern matching (`fnmatch`)
4. Fix 1.4 — hidden file / sensitive file deny-list
5. Fix 1.5 — re-enable signal handling (`loop.add_signal_handler`)
6. Fix 1.6 — unified ignore patterns

**High value, low effort (include in same milestone):**
- UX 2.3 — fix `ignored_files` counter (trivial)
- UX 2.4 — fix CLI entrypoint (trivial)
- Config 3.1 — wire semaphore to config (trivial)
- Config 3.5 — remove dead config field (trivial)
- Config 3.6 — dev dependency cleanup (trivial)
- Fix 1.7 — async file I/O with aiofiles (low)

**Defer to subsequent milestone:**
- Config 3.2 — credential chain (low-medium, no correctness impact, security improvement)
- Config 3.3 — `.backupignore` file (medium, adds `pathspec` dependency)
- UX 2.1 — debounce (medium, requires Fix 1.1 first)
- UX 2.2 — upload progress (low-medium, quality of life only)
- UX 2.5 — in-memory backup state cache (medium, performance improvement)
- UX 2.6 — deduplicate MD5 computation (low, performance improvement)
- Config 3.4 — multipart lifecycle guidance (low, requires Fix 1.5 first)

---

## Confidence Assessment

| Area | Confidence | Notes |
|------|------------|-------|
| Critical bug analysis | HIGH | Directly from CONCERNS.md codebase audit |
| Graceful shutdown pattern | HIGH | Verified against asyncio official pattern + roguelynn.com article |
| Glob / ignore-file patterns | HIGH | pathspec library verified on PyPI; fnmatch in stdlib |
| Multipart upload cost risk | HIGH | Confirmed by AWS official docs |
| Credential chain | HIGH | boto3 official docs verify provider chain behavior |
| Feature scope (what to skip) | MEDIUM | Based on rclone/restic comparison; reasonable for personal tool constraints |

---

## Sources

- [pathspec on PyPI](https://pypi.org/project/pathspec/) — gitignore-style pattern matching
- [python-pathspec on GitHub](https://github.com/cpburnz/python-pathspec) — implementation reference
- [Graceful Shutdowns with asyncio — roguelynn.com](https://roguelynn.com/words/asyncio-graceful-shutdowns/) — shutdown pattern
- [Boto3 Credentials guide](https://boto3.amazonaws.com/v1/documentation/api/latest/guide/credentials.html) — credential provider chain
- [AWS: AbortIncompleteMultipartUpload lifecycle config](https://docs.aws.amazon.com/AmazonS3/latest/userguide/mpu-abort-incomplete-mpu-lifecycle-config.html) — multipart cost mitigation
- [AWS: Discovering and deleting incomplete multipart uploads](https://aws.amazon.com/blogs/aws-cloud-financial-management/discovering-and-deleting-incomplete-multipart-uploads-to-lower-amazon-s3-costs/) — cost impact confirmation
- [Restic vs rclone vs rsync comparison](https://dev.to/lovestaco/restic-vs-rclone-vs-rsync-choosing-the-right-tool-for-backups-gn9) — ecosystem feature expectations
- [Personal backup to Amazon S3 — Better Dev Blog](https://betterdev.blog/personal-backup-to-amazon-s3/) — personal tool patterns
