# Phase 3: Audit Hardening - Context

**Source:** ponytail audit of the whole repo, 2026-11-23 (17 findings; numbers below = audit item numbers)
**Core value at stake:** nothing silently missed, nothing silently lost.

## Locked decisions

- **D-01 (audit #3):** A file is a deletion candidate only if `not (folder/name).exists()`. Files that exist but were not captured this scan (hash error, `.backupignore` match) keep their existing `.milo_backup.info` entry and their S3 object. Ignoring a file never deletes it from S3.
- **D-02 (audit #5):** `soft_delete_object` returns True when the S3 source is already absent.
- **D-03 (audit #2):** After a failed move, call `get_object_info(old_key)`. Only when it returns None (source confirmed gone) fall back to upload in the same scan. Transient errors keep the stale entry and retry later (no re-upload of big files over a flaky link).
- **D-04 (audit #1):** `move_object(..., delete_source=True)`. FileListener passes `delete_source=False` when the old key maps back to a local file that still exists. Ambiguous reverse mapping (two watch roots with the same S3 name) means "unknown" -> `delete_source=False`.
- **D-05 (audit #4, #9):** Mtime comes from the `stat()` taken before hashing. The scan's MD5 and mtime travel with the file into upload; no second hash, no second stat.
- **D-06 (audit #12):** Drop the post-put `check_exists`. For single-part uploads compare the `put_object` ETag to the MD5; multipart is not compared.
- **D-07 (audit #6):** Atomic write = `.milo_backup.info.tmp` + `os.replace`. **Gate:** the NAS (UGreen) enforces a non-standard permission check on bind mounts (see memory `nas-container-permissions`). Plan 03-05 has a blocking human checkpoint to prove replace works there. If it does not, fall back to: keep in-place write, add corrupt-file recovery on load.
- **D-08 (audit #10):** Needs the user's choice (LAN dashboard in use or not). Plan 03-06 has a decision checkpoint. Default if no answer: leave compose unchanged.

## Out of scope

- Real-time watcher, new features, S3 multipart copy for >5GB objects.
- The optional "audit existing bucket for holes" read-only script (audit follow-up) - separate quick task if the user wants it.

## Execution rules for every plan

- Tests first (tdd): write the failing test that reproduces the audit case, then the fix.
- Keep style: `Optional[...]`/`Dict`/`List` from typing, Google-style docstrings, f-string logging, requirement tag in comments (e.g. `# SAFE-01:`).
- After each plan: `uv run pytest -q` and `uv run ruff check . && uv run ruff format --check .` must pass.
- Plans touch the same files, so they run strictly one wave at a time.
- Final step of the phase (per CLAUDE.md): push branch, open PR to `main` titled `feat(phase3): audit-hardening`.
