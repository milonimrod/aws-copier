---
phase: 01-core-reliability
plan: 02
subsystem: config
tags: [pyproject, dependencies, simple-config, cleanup]
dependency_graph:
  requires: []
  provides: [CONFIG-02, CONFIG-03, CONFIG-04]
  affects: [aws_copier/models/simple_config.py, pyproject.toml, tests/unit/test_simple_config.py]
tech_stack:
  added: []
  patterns: [TDD red-green, uv dependency-groups]
key_files:
  created: []
  modified:
    - pyproject.toml
    - aws_copier/models/simple_config.py
    - tests/unit/test_simple_config.py
    - uv.lock
decisions:
  - "Move ruff and python-dotenv to [dependency-groups].dev (not [project.optional-dependencies].dev) per uv conventions"
  - "Keep [project.optional-dependencies].dev block untouched — it is a separate PEP 621 extras mechanism"
  - "test_config_create_directories fully removed since it exercised the removed create_directories method"
metrics:
  duration_minutes: 15
  completed_date: "2026-04-25"
  tasks_completed: 2
  files_modified: 4
requirements:
  - CONFIG-02
  - CONFIG-03
  - CONFIG-04
---

# Phase 1 Plan 02: Config Defect Cleanup Summary

**One-liner:** Fixed broken CLI entrypoint (`simple_main:main` → `main:sync_main`), removed dead `discovered_files_folder` field and `create_directories()` method from `SimpleConfig`, and moved `ruff`/`python-dotenv` from runtime to dev dependencies.

## Tasks Completed

| Task | Name | Commit | Files |
|------|------|--------|-------|
| 1 | Fix pyproject.toml entrypoint and move ruff/python-dotenv to dev | 2d05a3e | pyproject.toml, uv.lock |
| 2 (RED) | Add failing tests for discovered_files_folder removal | 836ed04 | tests/unit/test_simple_config.py |
| 2 (GREEN) | Remove discovered_files_folder field and create_directories method | 09738e6 | aws_copier/models/simple_config.py, tests/unit/test_simple_config.py |

## Changes Applied

### pyproject.toml

**Entrypoint fix (CONFIG-02):**
```
Before: aws-copier = "simple_main:main"
After:  aws-copier = "main:sync_main"
```

**Runtime deps (CONFIG-04) — removed:**
```toml
# Removed from [project].dependencies:
"python-dotenv>=1.1.1",
"ruff>=0.12.11",
```

**Dev group (CONFIG-04) — added:**
```toml
# Added to [dependency-groups].dev:
"python-dotenv>=1.1.1",
"ruff>=0.12.11",
```

### aws_copier/models/simple_config.py (CONFIG-03)

Four removals:
1. `__init__`: 5-line `discovered_files_folder` field assignment block (lines 41-45)
2. `create_directories()` method: 3 lines (lines 79-81)
3. `save_to_yaml()`: 1 line — `"discovered_files_folder": str(self.discovered_files_folder),`
4. `to_dict()`: 1 line — `"discovered_files_folder": str(self.discovered_files_folder),`

Backward compatibility: `SimpleConfig.__init__(**kwargs)` accepts arbitrary kwargs, so old YAML files containing `discovered_files_folder:` are silently ignored after loading — no user config breaks.

### tests/unit/test_simple_config.py

- Removed: `test_config_create_directories` (exercised removed method)
- Updated: `test_simple_config_creation` — removed assertion `config.discovered_files_folder is not None`
- Added: `test_discovered_files_folder_removed` — asserts field and method are absent
- Added: `test_legacy_config_with_discovered_files_folder_ignored` — asserts old YAML with the field loads cleanly

**Test count delta:** 14 tests total (was 15; removed 1, added 2, net +1)

## Verification Results

```
aws-copier = "main:sync_main"          ✓ entrypoint correct
[project].dependencies                  ✓ no ruff, no python-dotenv
[dependency-groups].dev                 ✓ python-dotenv>=1.1.1, ruff>=0.12.11
grep discovered_files_folder simple_config.py  → 0 hits ✓
grep create_directories simple_config.py       → 0 hits ✓
uv run pytest tests/unit/test_simple_config.py --no-cov  → 14 passed ✓
uv lock --check                         → consistent ✓
uv run aws-copier                       → starts normally, no ModuleNotFoundError ✓
```

## New Dependency Layout

**Runtime (`[project].dependencies`):**
- aiobotocore>=2.24.1
- watchdog>=3.0.0
- pyyaml>=6.0.2
- aiofiles>=24.1.0

**Dev (`[dependency-groups].dev`):**
- moto[s3]>=4.0.0
- pytest>=8.4.1
- pytest-asyncio>=1.1.0
- pytest-cov>=6.2.1
- python-dotenv>=1.1.1 (moved from runtime)
- ruff>=0.12.11 (moved from runtime)

## Deviations from Plan

None — plan executed exactly as written.

## TDD Gate Compliance

| Gate | Commit | Status |
|------|--------|--------|
| RED (test) | 836ed04 | Both new tests failed as expected before implementation |
| GREEN (feat) | 09738e6 | All 14 tests pass after implementation |
| REFACTOR | N/A | No cleanup needed; code is already minimal |

## Threat Flags

None — no new network endpoints, auth paths, file access patterns, or schema changes introduced.

## Self-Check: PASSED

All files confirmed present: pyproject.toml, simple_config.py, test_simple_config.py, uv.lock, 01-02-SUMMARY.md

All commits confirmed: 2d05a3e (Task 1), 836ed04 (Task 2 RED), 09738e6 (Task 2 GREEN)
