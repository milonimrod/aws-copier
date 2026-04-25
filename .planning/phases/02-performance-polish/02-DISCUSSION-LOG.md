# Phase 2: Performance & Polish - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-04-25
**Phase:** 02-performance-polish
**Areas discussed:** Backup info migration, .backupignore scope, Credential fallback trigger, Lifecycle rule conflicts

---

## Backup Info Migration

| Option | Description | Selected |
|--------|-------------|----------|
| Migrate silently | Treat old string entries as {md5: value, mtime: 0}. mtime=0 forces one re-stat per file on first run, then new format takes over. No data loss, no re-upload. | ✓ |
| Wipe and re-scan | Delete old entries and rebuild from scratch. All files get re-MD5'd and re-checked against S3. | |
| Keep old format until file changes | Leave old entries as-is; only write new format when a file is updated. Mixed format persists until all files cycle through. | |

**User's choice:** Migrate silently (Recommended)
**Notes:** No additional notes.

---

## Mtime Storage Timing

| Option | Description | Selected |
|--------|-------------|----------|
| mtime at upload time | Store os.stat(file).st_mtime captured just before upload. Accurate — changes during/after upload detected on next scan. | ✓ |
| mtime at scan start | Capture mtime when the folder scan begins. Simpler, but misses files modified during upload. | |

**User's choice:** mtime at upload time (Recommended)

---

## .backupignore Scope

| Option | Description | Selected |
|--------|-------------|----------|
| Cascades into subdirs | .backupignore in /photos/ also filters /photos/2024/, /photos/raw/, etc. Gitignore mental model. | ✓ |
| Applies to its directory only | Only affects files directly in that folder. Subdirectories ignore it. | |

**User's choice:** Cascades into subdirs (Recommended)

---

## .backupignore Inheritance

| Option | Description | Selected |
|--------|-------------|----------|
| Yes, parent rules inherit down | Rules accumulate as you descend the tree — child rules add to parent rules. | ✓ |
| Child only — no inheritance | Each directory's .backupignore stands alone, replaces parent rules. | |

**User's choice:** Yes, parent rules inherit down (Recommended)

---

## Credential Fallback Trigger

| Option | Description | Selected |
|--------|-------------|----------|
| When fields are absent from config | Only fall back when aws_access_key_id / aws_secret_access_key are not present in YAML. Explicit config always wins. | ✓ |
| When fields look like placeholders | Also fall back when values match 'YOUR_ACCESS_KEY_ID' etc. Friendlier for new users. | |
| Always try provider chain first | AWS SDK behavior — provider chain first, config.yaml as fallback. | |

**User's choice:** When fields are absent from config (Recommended)

---

## Startup Credential Logging

| Option | Description | Selected |
|--------|-------------|----------|
| Log the source | Emit logger.info('AWS credentials loaded from: config.yaml / env vars / ~/.aws/credentials'). | ✓ |
| Silent — no credential log | Don't log credential provenance. | |

**User's choice:** Log the source (Recommended)

---

## Lifecycle Rule Failure

| Option | Description | Selected |
|--------|-------------|----------|
| Warn and continue | Log warning and proceed. Daemon is usable without this protection. | ✓ |
| Abort startup | Treat missing lifecycle rule as fatal error. Hard guarantee, but breaks read-only IAM users. | |

**User's choice:** Warn and continue (Recommended)

---

## Lifecycle Rule Conflict

| Option | Description | Selected |
|--------|-------------|----------|
| Leave it and log | Detect existing rule, log it at INFO level, leave untouched. | ✓ |
| Overwrite with 1-day rule | Always enforce exactly 1 day — overwrite any existing rule. | |

**User's choice:** Leave it and log (Recommended)

---

## Claude's Discretion

- Exact asyncio mechanism for debounce timer (asyncio.create_task + asyncio.sleep vs TimerHandle)
- Internal data structure for per-path debounce state in FolderWatcher
- Whether in-memory backup info cache is class-level or instance dict in FileListener
- pathspec version constraint and import pattern
- S3 API calls for reading/writing lifecycle rules

## Deferred Ideas

None — discussion stayed within phase scope.
