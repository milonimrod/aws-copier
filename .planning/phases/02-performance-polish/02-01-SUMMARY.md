---
phase: 02-performance-polish
plan: "01"
subsystem: config
tags: [pathspec, credentials, aws, config, provider-chain]

# Dependency graph
requires: []
provides:
  - pathspec>=0.9.0 runtime dependency (enables .backupignore in Plan 05)
  - SimpleConfig.use_credential_chain bool attribute (consumed by S3Manager in Plan 03)
  - SimpleConfig.credential_source str attribute (audit label for S3Manager.initialize())
affects: [02-03, 02-05]

# Tech tracking
tech-stack:
  added:
    - pathspec 0.12.1 (runtime dep, gitignore-style pattern matching)
  patterns:
    - "Derived attributes pattern: use_credential_chain/credential_source computed in __init__ but excluded from to_dict()/save_to_yaml() to prevent serialization round-trip pollution"
    - "Presence-based credential detection: not raw_key or not raw_secret (D-09)"

key-files:
  created: []
  modified:
    - pyproject.toml
    - uv.lock
    - aws_copier/models/simple_config.py
    - tests/unit/test_simple_config.py

key-decisions:
  - "D-09 applied: use_credential_chain is True when EITHER key or secret is absent or empty — both must be non-empty for config.yaml path"
  - "D-10 applied: credential_source label set deterministically at init for S3Manager audit logging (not computed at S3 call time)"
  - "Placeholder defaults ('YOUR_ACCESS_KEY_ID') replaced with empty string to make truthiness-based detection work correctly"
  - "Derived fields deliberately excluded from to_dict() and save_to_yaml() — T-02-01 information disclosure mitigation"

patterns-established:
  - "Derived-attribute exclusion from serialization: compute in __init__, omit from to_dict/save_to_yaml"
  - "TDD RED/GREEN for config attributes: write failing attribute-access tests before implementing"

requirements-completed:
  - CONFIG-05

# Metrics
duration: 3min
completed: "2026-04-26"
---

# Phase 02 Plan 01: Pathspec Dependency + Credential Chain Detection Summary

**pathspec 0.12.1 added as runtime dep and SimpleConfig gains use_credential_chain/credential_source derived attributes for AWS provider-chain fallback detection**

## Performance

- **Duration:** 3 min
- **Started:** 2026-04-26T05:58:42Z
- **Completed:** 2026-04-26T06:01:00Z
- **Tasks:** 2
- **Files modified:** 4

## Accomplishments

- pathspec 0.12.1 declared in `[project] dependencies` (not dev), locked in uv.lock, importable; enables .backupignore pattern matching for Plan 05
- Replaced placeholder credential defaults ("YOUR_ACCESS_KEY_ID" / "YOUR_SECRET_ACCESS_KEY") with empty string defaults so truthiness-based chain detection works correctly
- Added `use_credential_chain: bool` and `credential_source: str` derived attributes to SimpleConfig — computed from raw kwargs, never serialized
- 8 new tests in `TestCredentialChainDetection` class; all pass alongside existing 14 tests (22 total)

## Task Commits

Each task was committed atomically:

1. **Task 1: Add pathspec runtime dependency** - `e588182` (chore)
2. **Task 2: TDD RED — failing tests for CONFIG-05** - `722fdb1` (test)
3. **Task 2: TDD GREEN — implement credential chain detection** - `411a3d0` (feat)

_Note: Task 2 used TDD RED/GREEN cycle with separate commits per gate._

## Files Created/Modified

- `pyproject.toml` - Added `pathspec>=0.9.0` with inline comment to `[project] dependencies`
- `uv.lock` - Updated with resolved pathspec 0.12.1 entry
- `aws_copier/models/simple_config.py` - Replaced placeholder defaults; added `use_credential_chain` and `credential_source` derived attributes after `s3_prefix`
- `tests/unit/test_simple_config.py` - Added `TestCredentialChainDetection` class with 8 behaviour-proving tests

## Decisions Made

- Replaced `"YOUR_ACCESS_KEY_ID"` placeholder defaults with `""` (empty string) because the D-09 truthiness check (`not raw_key`) must treat the missing/unset case the same as empty — placeholders would falsely trigger the explicit-creds path.
- Used `kwargs.get("aws_access_key_id")` (returns `None` when absent) as `raw_key` rather than `self.aws_access_key_id` (already defaulted to `""`) to correctly distinguish "user passed nothing" from "user passed empty string". Both produce `use_credential_chain = True` per D-09.
- `credential_source` and `use_credential_chain` are NOT added to `to_dict()` or `save_to_yaml()` — they are derived from the YAML fields on load, not stored fields. This prevents write-back of meta-labels into the config file.

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered

None. `uv add 'pathspec>=0.9.0'` resolved and installed cleanly. All existing tests continued to pass after removing placeholder string defaults (no prior test asserted the placeholder values).

## TDD Gate Compliance

- RED gate: `722fdb1` — `test(02-01)` commit with 8 failing tests (AttributeError on missing attribute)
- GREEN gate: `411a3d0` — `feat(02-01)` commit; all 22 tests pass

## Known Stubs

None.

## Threat Flags

None — no new network endpoints, auth paths, file access patterns, or schema changes introduced. Threat mitigations T-02-01 and T-02-02 verified by tests.

## Next Phase Readiness

- Plan 03 (S3Manager credential chain): `config.use_credential_chain` and `config.credential_source` are ready to consume; S3Manager can switch between explicit credentials and botocore provider chain at session creation time.
- Plan 05 (.backupignore): pathspec 0.12.1 is installed and importable; `PathSpec.from_lines('gitignore', patterns)` is confirmed working.
- Plans 02 and 04 have no dependency on this plan — they can execute independently.

## Self-Check

- [x] `aws_copier/models/simple_config.py` exists and contains `use_credential_chain`
- [x] `tests/unit/test_simple_config.py` exists and contains `TestCredentialChainDetection`
- [x] `pyproject.toml` contains `pathspec>=0.9.0` in `[project] dependencies`
- [x] Commits e588182, 722fdb1, 411a3d0 exist in git log

---
*Phase: 02-performance-polish*
*Completed: 2026-04-26*
