# Phase 1: Core Reliability - Pattern Map

**Mapped:** 2026-04-24
**Files analyzed:** 8 (5 modified, 1 new, 2 new test files + 2 extended test files)
**Analogs found:** 7 / 8 (new `ignore_rules.py` has partial analog in existing ignore logic)

---

## File Classification

| New/Modified File | Role | Data Flow | Closest Analog | Match Quality |
|---|---|---|---|---|
| `aws_copier/core/ignore_rules.py` | utility | transform | `aws_copier/core/file_listener.py` lines 39-93 + 466-515 | partial (logic exists inline, not as module) |
| `aws_copier/core/file_listener.py` | service | batch + file-I/O | self (refactor) | exact |
| `aws_copier/core/folder_watcher.py` | service | event-driven | self (refactor) | exact |
| `aws_copier/models/simple_config.py` | config | transform | self (refactor) | exact |
| `main.py` | utility | request-response | self (refactor) | exact |
| `pyproject.toml` | config | — | self (refactor) | exact |
| `tests/unit/test_ignore_rules.py` | test | — | `tests/unit/test_file_listener.py` | role-match |
| `tests/unit/test_signal_handling.py` | test | — | `tests/unit/test_folder_watcher.py` | role-match |

---

## Pattern Assignments

### `aws_copier/core/ignore_rules.py` (new utility, transform)

**Analog:** `aws_copier/core/file_listener.py` lines 39-93 (ignore_patterns / ignore_dirs sets) and lines 466-515 (`_should_ignore_file` / `_should_ignore_directory` methods), plus `aws_copier/core/folder_watcher.py` lines 38-77 (duplicate set) and lines 142-162 (`_should_ignore_file` variant).

**Module-level docstring pattern** (file_listener.py line 1):
```python
"""File listener for incremental backup with .milo_backup.info tracking."""
```
New file follows same one-liner format:
```python
"""Centralized ignore rules for file and directory filtering."""
```

**Imports pattern** — matches project convention (`typing`, `pathlib`, stdlib only, logger after imports):
```python
import fnmatch
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import FrozenSet

logger = logging.getLogger(__name__)
```

**Constant pattern** (file_listener.py lines 39-93 — the sets being extracted):
```python
# Current inline sets in FileListener.__init__ — these become module-level frozensets
self.ignore_patterns = {
    ".DS_Store", "Thumbs.db", "desktop.ini",
    "hiberfil.sys", "pagefile.sys", "swapfile.sys",
    ".tmp", ".temp", ".swp", ".swo",
    ...
    "*.pyc", "*.bak", "*.backup", "*~",
    ".milo_backup.info",
}
self.ignore_dirs = {
    ".git", ".svn", ".hg", "__pycache__", ...
}
```
New constants use `UPPER_SNAKE_CASE` frozensets per project conventions.

**Core frozen dataclass pattern** — no existing frozen dataclass in codebase; use stdlib `@dataclass(frozen=True)`:
```python
@dataclass(frozen=True)
class IgnoreRules:
    """Centralized, immutable ignore-rule set consumed by FileListener and FileChangeHandler."""
    glob_patterns: FrozenSet[str] = field(default_factory=lambda: GLOB_PATTERNS)
    sensitive_deny: FrozenSet[str] = field(default_factory=lambda: SENSITIVE_DENY)
    ignore_dirs: FrozenSet[str] = field(default_factory=lambda: IGNORE_DIRS)
```

**Method docstring pattern** (file_listener.py lines 466-473):
```python
def _should_ignore_file(self, file_path: Path) -> bool:
    """Check if a file should be ignored.

    Args:
        file_path: Path to file to check

    Returns:
        True if file should be ignored, False otherwise
    """
```
New public methods on `IgnoreRules` use the same Google-style docstring, without underscore prefix (they are the public API per D-05).

**Existing `_should_ignore_directory` logic to copy** (file_listener.py lines 488-515):
```python
def _should_ignore_directory(self, dir_path: Path) -> bool:
    dirname = dir_path.name
    if dirname in self.ignore_dirs:
        return True
    if dirname.startswith("."):
        return True
    try:
        if dir_path.is_symlink():
            return True
        if hasattr(dir_path, "is_dir") and hasattr(dir_path, "exists"):
            if dir_path.exists() and not dir_path.is_dir():
                return True
    except Exception:
        return False
    return False
```

**Module-level singleton pattern** — no existing singleton in codebase; follows `DEFAULT_CONFIG_PATH` constant pattern (simple_config.py line 108-109):
```python
# simple_config.py pattern for module-level constant:
DEFAULT_CONFIG_PATH = Path.home() / "aws-copier-config.yaml"

# New singleton follows same module-level assignment style:
IGNORE_RULES: IgnoreRules = IgnoreRules()
```

---

### `aws_copier/core/file_listener.py` (service, batch + file-I/O — refactor)

**Analog:** self. All patterns already established in the file.

**Import addition** — add `dataclass` import is not needed here; only add the new module import. Match existing import block style (file_listener.py lines 1-16):
```python
"""File listener for incremental backup with .milo_backup.info tracking."""

import asyncio
import hashlib
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import aiofiles

from aws_copier.core.ignore_rules import IGNORE_RULES   # ADD THIS
from aws_copier.core.s3_manager import S3Manager
from aws_copier.models.simple_config import SimpleConfig

logger = logging.getLogger(__name__)
```

**CONFIG-01: Semaphore wired to config** (file_listener.py lines 33-37 — current hardcoded value):
```python
# BEFORE (line 34):
self.upload_semaphore = asyncio.Semaphore(50)

# AFTER — wire to config:
self.upload_semaphore = asyncio.Semaphore(self.config.max_concurrent_uploads)
```

**ASYNC-03: aiofiles + per-folder Lock pattern** (file_listener.py lines 202-220 and 415-429 — both `_load_backup_info` and `_update_backup_info`):

Current blocking pattern to replace:
```python
# _load_backup_info (lines 214-218):
with open(backup_info_file, "r", encoding="utf-8") as f:
    data = json.load(f)
    return data.get("files", {})

# _update_backup_info (lines 424-428):
with open(backup_info_file, "w", encoding="utf-8") as f:
    json.dump(backup_info, f, indent=2)
```

Lock registry to add in `__init__` (follows `_stats` dict pattern, lines 96-103):
```python
# Add after self.md5_semaphore:
self._folder_locks: Dict[Path, asyncio.Lock] = {}
```

New private helper method (follows `_calculate_md5_with_semaphore` pattern, lines 431-441):
```python
def _get_folder_lock(self, folder_path: Path) -> asyncio.Lock:
    """Get or create asyncio.Lock for the given folder path.

    Args:
        folder_path: Directory path needing a lock

    Returns:
        asyncio.Lock for this folder
    """
    if folder_path not in self._folder_locks:
        self._folder_locks[folder_path] = asyncio.Lock()
    return self._folder_locks[folder_path]
```

**ASYNC-02: gather pattern** (file_listener.py lines 342-389 — `_upload_files`):

Current serial loop to replace (lines 364-385):
```python
# BEFORE — serial for loop (lines 364-385):
uploaded_files = []
for filename, task in upload_tasks:
    try:
        result = await asyncio.wait_for(task, timeout=300)
        ...
    except asyncio.TimeoutError:
        ...
    except Exception as e:
        ...
```

The `_scan_current_files` method already uses the correct `gather` pattern (lines 253-263) — copy that structure:
```python
# EXISTING gather pattern in _scan_current_files (lines 253-263) — COPY THIS:
results = await asyncio.gather(*(task for _, task in md5_tasks), return_exceptions=True)
for (filename, _), result in zip(md5_tasks, results):
    if isinstance(result, Exception):
        logger.error(f"Error computing MD5 for {filename}: {result}")
        self._stats["errors"] += 1
    elif result:
        current_files[filename] = result
        self._stats["scanned_files"] += 1
    else:
        self._stats["errors"] += 1
```

New wrapper coroutine (adds per-task timeout before gather, follows `_calculate_md5_with_semaphore` naming):
```python
async def _upload_with_timeout(self, filename: str, folder_path: Path) -> tuple:
    """Upload a single file with per-task timeout wrapper.

    Args:
        filename: Name of file to upload
        folder_path: Path to folder containing the file

    Returns:
        Tuple of (filename, success_bool)
    """
    try:
        result = await asyncio.wait_for(
            self._upload_single_file(filename, folder_path), timeout=300
        )
        return filename, result
    except asyncio.TimeoutError:
        logger.error(f"Upload timeout for {filename} (5 minutes)")
        self._stats["errors"] += 1
        return filename, False
    except Exception as e:
        logger.error(f"Upload task failed for {filename}: {e}")
        self._stats["errors"] += 1
        return filename, False
```

**IGNORE-03/04: Replace `_should_ignore_file` with IGNORE_RULES + counter** (file_listener.py lines 466-486 and lines 236-240):

Current call site (line 238):
```python
if file_path.is_dir() or self._should_ignore_file(file_path):
    continue
```

After IGNORE-03/04:
```python
if file_path.is_dir() or IGNORE_RULES.should_ignore_file(file_path):
    self._stats["ignored_files"] += 1   # IGNORE-04: increment counter
    continue
```

Methods `_should_ignore_file` and `_should_ignore_directory` are removed from `FileListener` entirely; all callers use `IGNORE_RULES` singleton.

**Error handling pattern** (file_listener.py lines 306-339 — `_upload_single_file`) — all new methods must follow:
```python
try:
    ...
except Exception as e:
    logger.error(f"Error doing X for {some_path}: {e}")
    self._stats["errors"] += 1
    return False
```

---

### `aws_copier/core/folder_watcher.py` (service, event-driven — refactor)

**Analog:** self. Targeted line changes.

**ASYNC-05: Type annotation fix** (folder_watcher.py line 22):
```python
# BEFORE (line 22):
event_loop: asyncio.BaseEventLoop

# AFTER:
event_loop: asyncio.AbstractEventLoop
```

**ASYNC-01: Thread bridge fix** (folder_watcher.py lines 100-103):
```python
# BEFORE (lines 101-103):
self.event_loop.call_soon_threadsafe(
    asyncio.create_task, self._process_changed_file(file_path, event.event_type)
)

# AFTER:
asyncio.run_coroutine_threadsafe(
    self._process_changed_file(file_path, event.event_type),
    self.event_loop
)
# Fire-and-forget: do NOT call .result() — that would deadlock the watchdog thread
```

**IGNORE-03: Replace `_should_ignore_file` with IGNORE_RULES** (folder_watcher.py lines 93-94 and 142-162):

Import to add (follows existing import block style, folder_watcher.py lines 1-15):
```python
from aws_copier.core.ignore_rules import IGNORE_RULES
```

Call site (line 93):
```python
# BEFORE:
if self._should_ignore_file(file_path):
    return

# AFTER:
if IGNORE_RULES.should_ignore_file(file_path):
    return
```

Method `_should_ignore_file` (lines 142-162) is removed from `FileChangeHandler`; the diverging duplicate implementation is eliminated.

---

### `aws_copier/models/simple_config.py` (config — refactor)

**Analog:** self. Two targeted removals.

**CONFIG-03: Remove `discovered_files_folder` field** (simple_config.py lines 41-45):
```python
# REMOVE these lines from __init__:
discovered_files_folder_data = kwargs.get(
    "discovered_files_folder", str(Path.home() / ".aws-copier" / "discovered")
)
self.discovered_files_folder: Path = Path(discovered_files_folder_data)
```

**CONFIG-03: Remove `create_directories()`** (simple_config.py lines 79-81):
```python
# REMOVE this method entirely:
def create_directories(self) -> None:
    """Create necessary directories."""
    self.discovered_files_folder.mkdir(parents=True, exist_ok=True)
```

Also remove `discovered_files_folder` from `save_to_yaml` (line 70) and `to_dict` (line 103).

**`__init__` constructor pattern** (simple_config.py lines 12-49) — follow the same `kwargs.get(key, default)` style for any surviving fields:
```python
def __init__(self, **kwargs):
    """Initialize configuration with default values."""
    self.aws_access_key_id: str = kwargs.get("aws_access_key_id", "YOUR_ACCESS_KEY_ID")
    ...
    self.max_concurrent_uploads: int = kwargs.get("max_concurrent_uploads", 100)
```

---

### `main.py` (utility — refactor)

**Analog:** self. Signal handler re-enabled using existing skeleton (lines 102-128) plus new drain logic.

**ASYNC-06: Current broken signal handler skeleton** (main.py lines 102-128):
```python
# EXISTING dead code — uses signal.signal (sync approach, wrong for asyncio):
def _setup_signal_handlers(self):
    def signal_handler(signum, _):
        logger.info(f"Received signal {signum}")
        self.running = False
        if hasattr(self, "shutdown_event"):
            asyncio.create_task(self._set_shutdown_event())  # BUG: no running loop

    if os.name == "nt":
        signal.signal(signal.SIGINT, signal_handler)
        ...
    else:
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)
```

**Call site to uncomment and refactor** (main.py line 47 — currently commented out):
```python
# BEFORE (line 47):
# self._setup_signal_handlers()

# AFTER: call AFTER event loop is running, pass the running loop:
self._setup_signal_handlers()
```

**Shutdown method pattern** (main.py lines 79-100) — drain logic added inside existing `shutdown()`:
```python
async def shutdown(self):
    """Shutdown the application."""
    if not self.running:
        return

    logger.info("Shutting down AWS Copier...")
    self.running = False

    try:
        # ASYNC-06: drain in-flight uploads before stopping watcher
        # ... new drain block here ...
        await self.folder_watcher.stop()
        ...
```

**Error handling pattern** (main.py lines 72-77) — all signal/shutdown code follows existing try/except:
```python
    except KeyboardInterrupt:
        logger.info("Received interrupt signal")
    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
    finally:
        await self.shutdown()
```

**Platform guard pattern** (main.py lines 112-124 — existing `os.name == "nt"` guard):
```python
# Existing guard style to follow for ASYNC-06:
if os.name == "nt":  # Windows
    ...
else:  # Unix-like (macOS, Linux)
    ...
```
New signal handler uses `sys.platform != "win32"` (equivalent, matches RESEARCH.md recommendation) with `loop.add_signal_handler` on Unix and `signal.signal` + `loop.call_soon_threadsafe` on Windows.

---

### `pyproject.toml` (config — line edits)

**CONFIG-02: Entrypoint fix** (pyproject.toml line 32):
```toml
# BEFORE:
aws-copier = "simple_main:main"

# AFTER:
aws-copier = "main:sync_main"
```

**CONFIG-04: Dependency group move** (pyproject.toml lines 13-14):
```toml
# REMOVE from [project].dependencies:
"python-dotenv>=1.1.1",
"ruff>=0.12.11",

# ADD to [project.optional-dependencies].dev:
"python-dotenv>=1.1.1",
"ruff>=0.12.11",
```

---

### `tests/unit/test_ignore_rules.py` (new test file)

**Analog:** `tests/unit/test_file_listener.py` — specifically `TestFileListenerUtilities` class (lines 354-413).

**Module docstring pattern** (test_file_listener.py lines 1-5):
```python
"""
Comprehensive tests for FileListener with proper S3Manager mocking.
Tests the incremental backup functionality without testing S3 operations.
"""
```

**Import block pattern** (test_file_listener.py lines 7-16):
```python
import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from aws_copier.core.file_listener import FileListener
from aws_copier.models.simple_config import SimpleConfig
```
New test imports `IGNORE_RULES` and `IgnoreRules` from `aws_copier.core.ignore_rules`.

**Test class + method naming pattern** (test_file_listener.py lines 354-413):
```python
class TestFileListenerUtilities:
    """Test FileListener utility functions."""

    async def test_should_ignore_file(self, file_listener):
        """Test file ignore patterns."""
        # Test files that should be ignored
        assert file_listener._should_ignore_file(Path(".DS_Store"))
        assert file_listener._should_ignore_file(Path("Thumbs.db"))
        assert file_listener._should_ignore_file(Path(".milo_backup.info"))

        # Test files that should not be ignored
        assert not file_listener._should_ignore_file(Path("normal_file.txt"))
        assert not file_listener._should_ignore_file(Path("document.pdf"))
```
New test classes: `TestIgnoreRulesFiles`, `TestIgnoreRulesDirs`, `TestIgnoreRulesSensitive`, `TestIgnoreRulesSingleton`. No fixtures needed — tests call `IGNORE_RULES.should_ignore_file()` directly.

**Behavior-proving pattern** (D-01): Each test must prove a specific bug is fixed, not just check implementation:
```python
async def test_glob_pattern_matched_by_fnmatch(self):
    """IGNORE-01: *.pyc pattern matches report.pyc, not just exact '*.pyc' string."""
    assert IGNORE_RULES.should_ignore_file(Path("report.pyc"))
    assert IGNORE_RULES.should_ignore_file(Path("compiled.pyc"))
    assert not IGNORE_RULES.should_ignore_file(Path("*.pyc"))  # literal string not a real file
```

---

### `tests/unit/test_signal_handling.py` (new test file)

**Analog:** `tests/unit/test_folder_watcher.py` — specifically `TestFolderWatcherCore` (lines 78-145).

**Fixtures pattern** (test_folder_watcher.py lines 19-74):
```python
@pytest.fixture
def test_config(temp_watch_folder):
    """Test configuration with temporary watch folder."""
    return SimpleConfig(
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
        aws_region="us-east-1",
        s3_bucket="test-bucket",
        ...
    )

@pytest.fixture
def mock_file_listener():
    """Create a properly mocked FileListener."""
    mock = AsyncMock()
    mock._process_current_folder = AsyncMock()
    return mock
```

**Async test method pattern** (test_folder_watcher.py lines 89-103):
```python
async def test_start_folder_watcher(self, folder_watcher, temp_watch_folder):
    """Test starting the folder watcher."""
    with patch.object(folder_watcher.observer, "start") as mock_observer_start:
        await folder_watcher.start()
        assert folder_watcher.running is True
        mock_observer_start.assert_called_once()
```

**Behavior-proving drain test pattern** (D-01 + D-03 + D-04):
```python
async def test_sigterm_drains_uploads_before_exit(self):
    """ASYNC-06: SIGTERM waits up to 60s for in-flight uploads to complete."""
    # Create a slow-upload mock that takes 2 seconds
    # Send SIGTERM
    # Assert: upload completes before shutdown_event fires
    # Assert: no logger.warning about abandoned files
```

---

### `tests/unit/test_file_listener.py` (extended — ASYNC-02, ASYNC-03, CONFIG-01)

**Analog:** self. New test methods added to existing classes.

**Existing concurrent upload test to extend** (test_file_listener.py lines 289-305 — `test_concurrent_upload_with_semaphore`):
```python
async def test_concurrent_upload_with_semaphore(self, file_listener, temp_watch_folder):
    """Test that concurrent uploads respect semaphore limit."""
    files_to_upload = [f"file_{i}.txt" for i in range(10)]
    for filename in files_to_upload:
        (temp_watch_folder / filename).write_text(f"Content of {filename}")
    uploaded_files = await file_listener._upload_files(files_to_upload, temp_watch_folder)
    assert len(uploaded_files) == 10
    assert file_listener.s3_manager.upload_file.call_count == 10
```

New behavior-proving ASYNC-02 test must prove concurrency, not just completeness:
```python
async def test_upload_files_runs_concurrently_not_serially(self, file_listener, temp_watch_folder):
    """ASYNC-02: 10 files upload in parallel, not one-at-a-time."""
    # Use asyncio.Event barriers to prove concurrent execution
```

---

### `tests/unit/test_folder_watcher.py` (extended — ASYNC-01, ASYNC-04, ASYNC-05)

**Analog:** self. New test methods added to `TestFileChangeHandler`.

**ASYNC-01 test extends** `test_on_any_event_file_created` (test_folder_watcher.py lines 213-227):
```python
def test_on_any_event_file_created(self, file_change_handler, temp_watch_folder):
    """Test handling file created events."""
    test_file = temp_watch_folder / "new_file.txt"
    test_file.write_text("New content")
    event = FileCreatedEvent(str(test_file))
    with patch.object(file_change_handler, "_process_changed_file"):
        file_change_handler.on_any_event(event)
        file_change_handler.event_loop.call_soon_threadsafe.assert_called_once()
```

New ASYNC-01 test must prove `run_coroutine_threadsafe` is used, not `call_soon_threadsafe`:
```python
def test_on_any_event_uses_run_coroutine_threadsafe(self, ...):
    """ASYNC-01: watchdog events are bridged via run_coroutine_threadsafe, not call_soon_threadsafe."""
    # patch asyncio.run_coroutine_threadsafe and assert it is called
    # assert call_soon_threadsafe is NOT called
```

---

## Shared Patterns

### Error Handling
**Source:** `aws_copier/core/file_listener.py` lines 305-339 (`_upload_single_file`) and lines 306-338
**Apply to:** All new/modified async methods in `file_listener.py`, `folder_watcher.py`, `main.py`, `ignore_rules.py` (sync methods can omit `self._stats` increment)

```python
try:
    # ... logic ...
except PermissionError:
    logger.warning(f"Permission denied accessing {path}: {e}")
    # do NOT increment errors for permission issues
except Exception as e:
    logger.error(f"Error doing X for {path}: {e}")
    self._stats["errors"] += 1
    return False  # bool operations
    # return None  # Optional returns
```

### Logging Levels
**Source:** `aws_copier/core/file_listener.py` throughout
**Apply to:** All files

```python
logger.info(f"...")    # milestones: "Starting drain (max 60s)", "Uploaded: {path} -> {key}"
logger.debug(f"...")   # per-file details: individual file processing
logger.warning(f"...") # non-fatal: abandoned upload, permission denied
logger.error(f"...")   # failures that increment _stats["errors"]
```

### Google-Style Docstrings
**Source:** `aws_copier/core/file_listener.py` lines 296-305
**Apply to:** Every new method in every file

```python
async def method_name(self, arg: Type) -> ReturnType:
    """One-line description.

    Args:
        arg: Description of arg

    Returns:
        Description of return value
    """
```

### Type Hints — Python 3.9 Compatibility
**Source:** `aws_copier/core/file_listener.py` lines 7-9
**Apply to:** All new code

```python
# USE (typing module imports):
from typing import Dict, FrozenSet, List, Optional, Set, Tuple

# DO NOT USE (Python 3.10+ syntax):
dict[str, str]      # use Dict[str, str]
list[str]           # use List[str]
str | None          # use Optional[str]
set[asyncio.Task]   # use Set[asyncio.Task]
tuple[str, bool]    # use Tuple[str, bool]
```

### Import Block Structure
**Source:** `aws_copier/core/file_listener.py` lines 1-16
**Apply to:** All files

```python
"""Module docstring."""

# stdlib — alphabetical
import asyncio
import fnmatch
import json
import logging
import signal
import sys

# third-party (blank line separator)
import aiofiles

# project-local (blank line separator)
from aws_copier.core.ignore_rules import IGNORE_RULES
from aws_copier.core.s3_manager import S3Manager
from aws_copier.models.simple_config import SimpleConfig

# always immediately after imports, before any class/function
logger = logging.getLogger(__name__)
```

### Test Fixture Pattern
**Source:** `tests/unit/test_file_listener.py` lines 17-66
**Apply to:** `test_ignore_rules.py`, `test_signal_handling.py`

```python
@pytest.fixture
def temp_watch_folder():
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        (temp_path / "file1.txt").write_text("Content of file 1")
        yield temp_path

@pytest.fixture
def test_config(temp_watch_folder):
    return SimpleConfig(
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
        aws_region="us-east-1",
        s3_bucket="test-bucket",
        s3_prefix="backup",
        watch_folders=[str(temp_watch_folder)],
    )
```

---

## No Analog Found

| File | Role | Data Flow | Reason |
|---|---|---|---|
| `aws_copier/core/ignore_rules.py` | utility | transform | No frozen dataclass module exists in project; closest analog is inline dicts/sets in `file_listener.py` and `folder_watcher.py` being extracted |

---

## Key Observations for Planner

1. **`_should_ignore_file` exists in two places with diverging logic** — `file_listener.py` line 466 (no dot-file block) and `folder_watcher.py` line 142 (has dot-file block, missing glob eval). Both are deleted in IGNORE-03; `IGNORE_RULES.should_ignore_file()` replaces both call sites.

2. **`asyncio.gather` already used for MD5** (file_listener.py lines 253-263) — `_upload_files` must adopt the same pattern. The wrapper coroutine `_upload_with_timeout` takes the same shape as `_calculate_md5_with_semaphore` (lines 431-441).

3. **`_setup_signal_handlers()` stub already exists** in `main.py` lines 102-128 but is commented out (line 47) and uses the wrong approach (`signal.signal` outside async context). The method is rewritten in-place; the call site uncommented.

4. **`asyncio.BaseEventLoop` annotation** appears only at `folder_watcher.py` line 22 (`FileChangeHandler.__init__` signature). `FolderWatcher` already uses `asyncio.AbstractEventLoop` (line 180). Only one line needs changing.

5. **`upload_semaphore` hardcoded to 50** at `file_listener.py` line 34; `config.max_concurrent_uploads` defaults to 100 (simple_config.py line 48). CONFIG-01 is a one-line change.

6. **Existing tests for `call_soon_threadsafe`** in `test_folder_watcher.py` lines 213-266 assert the old behavior. Those assertions must be updated when ASYNC-01 is applied — the `mock_event_loop` fixture's `call_soon_threadsafe` mock will no longer be called.

---

## Metadata

**Analog search scope:** `aws_copier/core/`, `aws_copier/models/`, `tests/unit/`, `main.py`
**Files read:** 8 source files + 2 test files + pyproject.toml + REQUIREMENTS.md + CONTEXT.md + RESEARCH.md
**Pattern extraction date:** 2026-04-24
