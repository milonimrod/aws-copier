---
phase: 01-core-reliability
plan: "01"
subsystem: core/ignore-rules
tags: [ignore-rules, fnmatch, frozen-dataclass, security, tdd]
dependency_graph:
  requires: []
  provides:
    - aws_copier.core.ignore_rules.IGNORE_RULES
    - aws_copier.core.ignore_rules.IgnoreRules
    - aws_copier.core.ignore_rules.GLOB_PATTERNS
    - aws_copier.core.ignore_rules.SENSITIVE_DENY
    - aws_copier.core.ignore_rules.IGNORE_DIRS
  affects:
    - aws_copier/core/file_listener.py (Plans 03/04 — will replace inline ignore logic)
    - aws_copier/core/folder_watcher.py (Plans 03/04 — will replace inline ignore logic)
tech_stack:
  added: []
  patterns:
    - frozen dataclass as immutable config object
    - module-level singleton with default_factory for frozenset fields
    - fnmatch.fnmatch for glob pattern evaluation
key_files:
  created:
    - aws_copier/core/ignore_rules.py
    - tests/unit/test_ignore_rules.py
  modified: []
decisions:
  - "Dot-prefix files blocked unconditionally (startswith('.')) so future credential tools are caught by default without requiring explicit deny list entries"
  - "Module-level FrozenSet constants (not inline in dataclass) per plan spec — allows Plans 03/04 to import just the constants if needed"
  - "FrozenInstanceError on mutation confirmed via test — shared singleton cannot be corrupted at runtime"
metrics:
  duration_minutes: 2
  completed_date: "2026-04-25"
  tasks_completed: 2
  files_created: 2
  files_modified: 0
requirements:
  - IGNORE-01
  - IGNORE-02
  - IGNORE-03
---

# Phase 1 Plan 01: IgnoreRules Module Summary

**One-liner:** Frozen `IgnoreRules` dataclass with `fnmatch`-based glob matching, dot-file blocking, and credential deny list as a module-level singleton.

## What Was Built

New module `aws_copier/core/ignore_rules.py` — the single source of truth for all file/directory ignore logic. Replaces the diverging sets maintained separately in `FileListener` (file_listener.py:40-93) and `FileChangeHandler` (folder_watcher.py:38-77). Integration into those classes happens in Plans 03 and 04.

## API Surface

### Module import path (downstream consumers must use this)

```python
from aws_copier.core.ignore_rules import IGNORE_RULES
```

### IgnoreRules class

```python
@dataclass(frozen=True)
class IgnoreRules:
    glob_patterns: FrozenSet[str]    # fnmatch patterns, e.g. "*.pyc", "*.bak", "*~"
    sensitive_deny: FrozenSet[str]   # credential patterns, e.g. "*.pem", "*.key", "id_rsa"
    ignore_dirs: FrozenSet[str]      # directory names, e.g. ".git", "__pycache__"

    def should_ignore_file(self, path: Path) -> bool: ...
    def should_ignore_dir(self, path: Path) -> bool: ...

IGNORE_RULES: IgnoreRules = IgnoreRules()
```

### should_ignore_file logic (order matters)

1. `path.name.startswith(".")` → `True` (IGNORE-02: dot-prefix always blocked)
2. `fnmatch.fnmatch(name, pattern)` over `sensitive_deny` → `True` if matched
3. `fnmatch.fnmatch(name, pattern)` over `glob_patterns` → `True` if matched
4. Default → `False`

### should_ignore_dir logic (order matters)

1. `path.name in ignore_dirs` → `True`
2. `path.name.startswith(".")` → `True`
3. `path.is_symlink()` → `True` (symlink cycle prevention; exception = fail-closed → `True`)
4. Default → `False`

## Constants (for Plans 03/04 consumers)

### GLOB_PATTERNS (IGNORE-01)

`.DS_Store`, `Thumbs.db`, `desktop.ini`, `hiberfil.sys`, `pagefile.sys`, `swapfile.sys`,
`*.tmp`, `*.temp`, `*.swp`, `*.swo`, `*.pyc`, `*.pyo`, `*.bak`, `*.backup`, `*~`,
`.coverage`, `.milo_backup.info`

### SENSITIVE_DENY (IGNORE-02)

`.env`, `.env.*`, `*.pem`, `*.key`, `id_rsa`, `id_dsa`, `id_ecdsa`, `id_ed25519`,
`*.p12`, `*.pfx`, `.npmrc`, `.pypirc`, `.netrc`, `*.secret`

### IGNORE_DIRS (IGNORE-03)

`.git`, `.svn`, `.hg`, `__pycache__`, `.pytest_cache`, `node_modules`, `.venv`, `venv`,
`.aws-copier`, `$RECYCLE.BIN`, `System Volume Information`, `.Trashes`, `.Spotlight-V100`,
`.fseventsd`, `.vscode`, `.idea`

## Test Results

- **24 tests** in `tests/unit/test_ignore_rules.py` — all pass
- **127 total** in test suite — all pass (no regressions)
- Test classes: `TestIgnoreRulesGlobPatterns`, `TestIgnoreRulesSensitiveDeny`, `TestIgnoreRulesDirs`, `TestIgnoreRulesImmutability`, `TestIgnoreRulesSingleton`

## TDD Gate Compliance

| Gate | Commit | Status |
|------|--------|--------|
| RED (failing tests) | b9a6b21 | PASS — ImportError confirmed before implementation |
| GREEN (implementation) | 95bf51b | PASS — 24 tests pass |
| REFACTOR | n/a | No refactor needed — code clean on first pass |

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Removed unused `tempfile` import from test file**
- **Found during:** Post-implementation ruff check
- **Issue:** `import tempfile` was included in the plan's test template but the test file uses `tmp_path` pytest fixture instead of `tempfile` directly
- **Fix:** Removed the unused import to satisfy `ruff check` (F401)
- **Files modified:** `tests/unit/test_ignore_rules.py`
- **Commit:** 95bf51b

## Known Stubs

None — both methods are fully implemented and tested. No placeholder values.

## Threat Flags

No new threat surface introduced beyond what is covered in the plan's threat model. This module is purely in-process; it performs no I/O, no network access, and no filesystem writes during import or method calls (only `path.is_symlink()` reads, which is a safe stdlib call).

## Self-Check: PASSED

| Item | Status |
|------|--------|
| `aws_copier/core/ignore_rules.py` exists | FOUND |
| `tests/unit/test_ignore_rules.py` exists | FOUND |
| `.planning/phases/01-core-reliability/01-01-SUMMARY.md` exists | FOUND |
| Commit b9a6b21 (RED phase) | FOUND |
| Commit 95bf51b (GREEN phase) | FOUND |
