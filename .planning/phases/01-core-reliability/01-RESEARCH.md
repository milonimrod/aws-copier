# Phase 1: Core Reliability - Research

**Researched:** 2026-04-24
**Domain:** Python asyncio, watchdog thread-bridging, signal handling, fnmatch ignore rules, aiofiles
**Confidence:** HIGH

---

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions

- **D-01:** Each fix gets a corresponding behavior-proving test using the existing `pytest-asyncio` + `moto[s3]` setup. Tests must prove the bug is actually fixed (e.g., a real-time watchdog event triggers an S3 upload without a scan cycle; 10 simultaneous changed files upload concurrently; SIGTERM drains in-flight uploads before the process exits).
- **D-02:** Tests co-located with the fix work — no separate test phase.
- **D-03:** On SIGTERM, drain in-flight uploads for up to **60 seconds**, then force-exit even if some uploads are still running.
- **D-04:** Each upload that doesn't complete within the drain window gets a `logger.warning()` naming the abandoned file, so the user knows what to expect on the next scan cycle.
- **D-05:** `IgnoreRules` is a frozen dataclass in `aws_copier/core/ignore_rules.py` with **instance methods** `should_ignore_file(path: Path) -> bool` and `should_ignore_dir(path: Path) -> bool`. Logic is fully centralized — callers never re-implement the check.
- **D-06:** A module-level singleton `IGNORE_RULES = IgnoreRules()` is exported from `ignore_rules.py`. `FileListener` and `FileChangeHandler` both import and use this single instance — no per-component instantiation.

### Claude's Discretion

- Exact fnmatch pattern list for IGNORE-01 (glob patterns to expand from the current set)
- Exact dot-file/sensitive-file deny list for IGNORE-02 (SSH keys, `.pem`, `.key`, etc.)
- Internal structure of the per-folder `asyncio.Lock` registry in `FileListener` (ASYNC-03)
- Whether `asyncio.wait_for` wraps individual upload coroutines or gathered tasks (ASYNC-02)
- How to surface the drain countdown in logs during shutdown

### Deferred Ideas (OUT OF SCOPE)

None — discussion stayed within phase scope.
</user_constraints>

---

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| ASYNC-01 | Replace `call_soon_threadsafe(asyncio.create_task, coro)` with `asyncio.run_coroutine_threadsafe` in `FileChangeHandler.on_any_event` | Verified: `asyncio.run_coroutine_threadsafe` exists in stdlib, fire-and-forget pattern tested |
| ASYNC-02 | Replace serial `for` loop + `await asyncio.wait_for(task)` in `_upload_files` with `asyncio.gather(*tasks, return_exceptions=True)` | Verified: tasks are already `create_task`'d concurrently; serial `wait_for` in a loop serializes the timeout windows; gather fixes both correctness and worst-case timeout behavior |
| ASYNC-03 | Convert `_load_backup_info` / `_update_backup_info` from blocking `open()` to `aiofiles`, add per-folder `asyncio.Lock` | Verified: `aiofiles` already installed; lock registry pattern tested |
| ASYNC-04 | Replace `asyncio.get_event_loop()` with `asyncio.get_running_loop()` / `asyncio.to_thread()` | Grep confirms no live `get_event_loop()` calls; `get_running_loop()` already used in `FolderWatcher.start()` — only check needed is for any future occurrences introduced during refactor |
| ASYNC-05 | Replace `asyncio.BaseEventLoop` type annotation with `asyncio.AbstractEventLoop` | Verified: `BaseEventLoop` appears on line 22 of `folder_watcher.py` in `FileChangeHandler.__init__` signature |
| ASYNC-06 | Re-enable signal handling: `loop.add_signal_handler` (Unix) + `signal.signal` + `loop.call_soon_threadsafe` (Windows), drain in-flight tasks for 60 s | Verified: `loop.add_signal_handler` available on macOS; `asyncio.all_tasks` + `asyncio.wait(timeout=60)` is the drain pattern |
| IGNORE-01 | Use `fnmatch.fnmatch` for glob patterns in `should_ignore_file` | Verified: `fnmatch.fnmatch('report.bak', '*.bak')` returns `True`; current code uses exact set membership, which misses `*.bak`-style patterns |
| IGNORE-02 | Add hardcoded dot-file / sensitive-file deny list to `should_ignore_file` | Verified: `fnmatch.fnmatch('.env', '.env')` and `fnmatch.fnmatch('id_rsa', 'id_rsa')` both `True`; `FileListener._should_ignore_file` currently has no dot-file block |
| IGNORE-03 | Create `aws_copier/core/ignore_rules.py` frozen dataclass; update `FileListener` and `FileChangeHandler` to use `IGNORE_RULES` singleton | Verified: `@dataclass(frozen=True)` with instance methods works correctly in Python 3.11 |
| IGNORE-04 | Increment `_stats["ignored_files"]` in `_scan_current_files` when `should_ignore_file` returns `True` | Confirmed: counter field exists in `_stats` dict but is never incremented in `_scan_current_files` |
| CONFIG-01 | Wire `config.max_concurrent_uploads` to `upload_semaphore` in `FileListener.__init__` | Confirmed: `upload_semaphore = asyncio.Semaphore(50)` hardcoded; `config.max_concurrent_uploads` field exists and defaults to `100` |
| CONFIG-02 | Fix pyproject.toml entrypoint: `"simple_main:main"` → `"main:sync_main"` | Confirmed: `simple_main` module does not exist; `main.py` exports `sync_main()` |
| CONFIG-03 | Remove `discovered_files_folder` field and `create_directories()` call from `SimpleConfig` | Confirmed: field on line 42-45, `create_directories()` on line 79-81 |
| CONFIG-04 | Move `ruff` and `python-dotenv` from runtime deps to dev deps | Confirmed: both appear in `[project].dependencies` in `pyproject.toml` |
</phase_requirements>

---

## Summary

Phase 1 is a pure bug-fix refactor touching five files: `aws_copier/core/file_listener.py`, `aws_copier/core/folder_watcher.py`, `aws_copier/models/simple_config.py`, `main.py`, and `pyproject.toml` — plus one new file: `aws_copier/core/ignore_rules.py`. No new capabilities are added. Every change has a corresponding behavior-proving test.

The most architecturally significant change is ASYNC-01 (thread bridge) and ASYNC-06 (signal handling + drain), because they interact: the drain must wait for any in-flight upload coroutines that were submitted via the thread bridge. The implementation order locked in STATE.md (ignore_rules first → thread bridge → gather → aiofiles+lock → signal handling) ensures each dependency is satisfied before the next fix lands.

The second significant change is the centralization of ignore logic into `IgnoreRules` (IGNORE-03). Both `FileListener._should_ignore_file` and `FileChangeHandler._should_ignore_file` currently duplicate an overlapping but diverging set — `FileChangeHandler` blocks dot-files but `FileListener` does not, while `FileListener` has the more complete pattern set. The new frozen dataclass unifies them.

**Primary recommendation:** Implement in the order: `ignore_rules.py` → ASYNC-01 → ASYNC-02 → ASYNC-03 → ASYNC-04/05 → ASYNC-06. Each group can be one commit with its tests. CONFIG changes are independent and can be batched into a single CONFIG commit at any point.

---

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| Ignore-rule evaluation | `IgnoreRules` (new shared module) | — | Called by both `FileListener` (scan loop) and `FileChangeHandler` (event handler); must be a single source of truth |
| Thread → asyncio bridge | `FileChangeHandler.on_any_event` | `FolderWatcher.start` (stores loop ref) | Watchdog runs in OS thread; the bridge must schedule coroutines onto the asyncio loop |
| Concurrent upload orchestration | `FileListener._upload_files` | `FileListener._upload_single_file` | Fan-out scheduling belongs at the orchestrator level; semaphore enforcement stays inside the per-file method |
| Backup state I/O | `FileListener._load_backup_info` / `_update_backup_info` | Per-folder `asyncio.Lock` registry | Both read and write must be async; lock prevents interleaved reads and writes for the same folder |
| Signal handling + drain | `AWSCopierApp` in `main.py` | `asyncio` event loop | Signal handlers must be registered on the running loop; drain logic lives in the app-level shutdown path |
| Configuration loading | `SimpleConfig` in `simple_config.py` | `pyproject.toml` | Semaphore size and entrypoint wired here |

---

## Standard Stack

### Core (all already installed in project venv)

| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| `asyncio` | stdlib (3.11) | Event loop, tasks, locks, semaphores | Required by project constraint; all I/O must be async |
| `aiofiles` | >=24.1.0 | Async file I/O | Already imported for `_calculate_md5`; extend to backup state I/O |
| `watchdog` | >=3.0.0 | Cross-platform FS events in a thread | Already in use; `call_soon_threadsafe` bridge is the only change |
| `fnmatch` | stdlib | Unix shell glob matching | Correct tool for `*.bak`, `*.pyc`, `*~` patterns |
| `signal` | stdlib | Unix/Windows signal registration | Required for SIGTERM handling |
| `dataclasses` | stdlib (3.7+) | Frozen dataclass for `IgnoreRules` | Provides immutability and clean `__init__` for pattern sets |

### No New Dependencies Required

All fixes use Python stdlib or libraries already present in the project. No `pip install` / `uv add` needed for any of the 14 requirements.

**Version verification:** [VERIFIED: uv run python] — Python 3.11.11, all libraries confirmed importable via project venv.

---

## Architecture Patterns

### System Architecture Diagram

```
OS Filesystem Events (watchdog thread)
    |
    | asyncio.run_coroutine_threadsafe(coro, loop)      [ASYNC-01 fix]
    v
asyncio Event Loop (main thread)
    |
    +-- FileChangeHandler._process_changed_file(path)
    |       |
    |       v
    |   FileListener._process_current_folder(folder_path)
    |       |
    |       +-- _load_backup_info()   [aiofiles + Lock]   [ASYNC-03 fix]
    |       |
    |       +-- _scan_current_files() [parallel MD5]
    |       |       |-- IGNORE_RULES.should_ignore_file()  [IGNORE-01/02/03/04 fix]
    |       |
    |       +-- _upload_files()  [asyncio.gather]          [ASYNC-02 fix]
    |       |       |-- _upload_single_file() x N
    |       |               |-- upload_semaphore (from config)  [CONFIG-01 fix]
    |       |               +-- S3Manager.upload_file()
    |       |
    |       +-- _update_backup_info()  [aiofiles + Lock]   [ASYNC-03 fix]
    |
SIGTERM -----> loop.add_signal_handler callback           [ASYNC-06 fix]
                    |
                    v
               asyncio.all_tasks() -> asyncio.wait(timeout=60)
                    |-- warn abandoned files
                    +-- loop.stop()
```

### Recommended Project Structure (after Phase 1)

```
aws_copier/
├── core/
│   ├── ignore_rules.py      # NEW: IgnoreRules frozen dataclass + IGNORE_RULES singleton
│   ├── file_listener.py     # MODIFIED: uses IGNORE_RULES, aiofiles, gather, config semaphore
│   ├── folder_watcher.py    # MODIFIED: run_coroutine_threadsafe, AbstractEventLoop annotation
│   └── s3_manager.py        # UNCHANGED
├── models/
│   └── simple_config.py     # MODIFIED: remove discovered_files_folder + create_directories()
└── ui/
    └── simple_gui.py        # UNCHANGED
main.py                      # MODIFIED: signal handlers re-enabled with drain logic
pyproject.toml               # MODIFIED: entrypoint fix + dep group moves
tests/unit/
├── test_ignore_rules.py     # NEW: covers IGNORE-01, IGNORE-02, IGNORE-03, IGNORE-04
├── test_file_listener.py    # EXTENDED: ASYNC-02, ASYNC-03, CONFIG-01 tests added
├── test_folder_watcher.py   # EXTENDED: ASYNC-01, ASYNC-04, ASYNC-05 tests added
└── test_signal_handling.py  # NEW: covers ASYNC-06 drain behavior
```

### Pattern 1: run_coroutine_threadsafe (ASYNC-01 fix)

**What:** Submit a coroutine from a non-asyncio thread to a running event loop.
**When to use:** Any time watchdog's `FileSystemEventHandler` (running in a thread) needs to trigger async work.

```python
# Source: Python stdlib asyncio docs [VERIFIED: python3 -c "help(asyncio.run_coroutine_threadsafe)"]
# In FileChangeHandler.on_any_event (REPLACES call_soon_threadsafe anti-pattern):
asyncio.run_coroutine_threadsafe(
    self._process_changed_file(file_path, event.event_type),
    self.event_loop
)
# Fire-and-forget: do NOT call .result() here — that would deadlock the watchdog thread
```

### Pattern 2: asyncio.gather for concurrent upload (ASYNC-02 fix)

**What:** Replace serial `for filename, task in upload_tasks: await asyncio.wait_for(task, 300)` with a single `gather` call.
**When to use:** Fan-out of N upload coroutines where all should run in parallel up to semaphore limit.

```python
# Source: Python stdlib asyncio [VERIFIED: tested in project venv]
# Wrap per-task timeout INSIDE the coroutine, not around the already-created task:
async def _upload_with_timeout(self, filename: str, folder_path: Path) -> tuple[str, bool]:
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

# Then in _upload_files:
tasks = [
    asyncio.create_task(self._upload_with_timeout(name, folder_path))
    for name in files_to_upload
]
results = await asyncio.gather(*tasks, return_exceptions=True)
uploaded_files = [
    name for name, ok in results
    if not isinstance(ok, Exception) and ok
]
```

### Pattern 3: aiofiles + per-folder Lock (ASYNC-03 fix)

**What:** Replace blocking `open()` in `_load_backup_info` / `_update_backup_info` with `aiofiles.open()` guarded by a per-folder `asyncio.Lock`.
**When to use:** Any time the backup info file for a folder is read or written — prevents race between a scan cycle and a real-time event hitting the same folder.

```python
# Source: aiofiles library [VERIFIED: tested in project venv]
# In FileListener.__init__:
self._folder_locks: Dict[Path, asyncio.Lock] = {}

def _get_folder_lock(self, folder_path: Path) -> asyncio.Lock:
    if folder_path not in self._folder_locks:
        self._folder_locks[folder_path] = asyncio.Lock()
    return self._folder_locks[folder_path]

# In _load_backup_info:
async with self._get_folder_lock(backup_info_file.parent):
    async with aiofiles.open(backup_info_file, "r", encoding="utf-8") as f:
        content = await f.read()
    data = json.loads(content)
    return data.get("files", {})

# In _update_backup_info:
async with self._get_folder_lock(backup_info_file.parent):
    async with aiofiles.open(backup_info_file, "w", encoding="utf-8") as f:
        await f.write(json.dumps(backup_info, indent=2))
```

### Pattern 4: IgnoreRules frozen dataclass (IGNORE-03 fix)

**What:** Single frozen dataclass with all ignore logic, exported as a module-level singleton.

```python
# Source: Python stdlib dataclasses [VERIFIED: tested in python3]
import fnmatch
from dataclasses import dataclass, field
from pathlib import Path
from typing import FrozenSet

GLOB_PATTERNS: FrozenSet[str] = frozenset({
    # System files
    ".DS_Store", "Thumbs.db", "desktop.ini",
    "hiberfil.sys", "pagefile.sys", "swapfile.sys",
    # Temp / editor
    "*.tmp", "*.temp", "*.swp", "*.swo",
    # Build artifacts
    "*.pyc", "*.pyo",
    # Backup files
    "*.bak", "*.backup", "*~",
    # Our own file
    ".milo_backup.info",
})

SENSITIVE_DENY: FrozenSet[str] = frozenset({
    ".env", ".env.*",        # dotenv files
    "*.pem", "*.key",        # TLS / private key files
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",  # SSH private keys
    "*.p12", "*.pfx",        # PKCS certs
    ".npmrc", ".pypirc",     # credential config files
    ".netrc",
    "*.secret",
})

IGNORE_DIRS: FrozenSet[str] = frozenset({
    ".git", ".svn", ".hg",
    "__pycache__", ".pytest_cache",
    "node_modules", ".venv", "venv",
    ".aws-copier",
    "$RECYCLE.BIN", "System Volume Information",
    ".Trashes", ".Spotlight-V100", ".fseventsd",
    ".vscode", ".idea",
})

@dataclass(frozen=True)
class IgnoreRules:
    """Centralized, immutable ignore-rule set for file and directory filtering."""
    glob_patterns: FrozenSet[str] = GLOB_PATTERNS
    sensitive_deny: FrozenSet[str] = SENSITIVE_DENY
    ignore_dirs: FrozenSet[str] = IGNORE_DIRS

    def should_ignore_file(self, path: Path) -> bool:
        """Return True if the file must not be uploaded."""
        name = path.name
        # Block dot-files and sensitive patterns first
        if name.startswith("."):
            return True
        for pattern in self.sensitive_deny:
            if fnmatch.fnmatch(name, pattern):
                return True
        for pattern in self.glob_patterns:
            if fnmatch.fnmatch(name, pattern):
                return True
        return False

    def should_ignore_dir(self, path: Path) -> bool:
        """Return True if the directory should be skipped entirely."""
        name = path.name
        if name in self.ignore_dirs:
            return True
        if name.startswith("."):
            return True
        try:
            if path.is_symlink():
                return True
        except Exception:
            return True
        return False

# Module-level singleton — import this, do not instantiate IgnoreRules directly
IGNORE_RULES: IgnoreRules = IgnoreRules()
```

### Pattern 5: Signal handling + drain (ASYNC-06 fix)

**What:** Register SIGTERM/SIGINT on the running loop, trigger graceful drain of in-flight upload tasks, then exit.

```python
# Source: Python stdlib asyncio, signal [VERIFIED: loop.add_signal_handler confirmed on macOS]
# In AWSCopierApp.start(), after event loop is running:
import sys

def _setup_signal_handlers(self) -> None:
    loop = asyncio.get_running_loop()
    if sys.platform != "win32":
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(
                sig,
                lambda s=sig: asyncio.ensure_future(self._handle_signal(s))
            )
    else:
        # Windows: signal.signal runs in the main thread; schedule shutdown via call_soon_threadsafe
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(
                sig,
                lambda s, _: loop.call_soon_threadsafe(
                    asyncio.ensure_future, self._handle_signal(s)
                )
            )

async def _handle_signal(self, signum: int) -> None:
    logger.info(f"Received signal {signum}, starting graceful drain (max 60s)")
    self.running = False
    self.shutdown_event.set()  # wake main loop

async def shutdown(self) -> None:
    # Drain in-flight uploads (ASYNC-06)
    upload_tasks = {
        t for t in asyncio.all_tasks()
        if t.get_name().startswith("upload-") or "upload" in str(t.get_coro())
    }
    if upload_tasks:
        logger.info(f"Draining {len(upload_tasks)} in-flight uploads (max 60s)")
        done, pending = await asyncio.wait(upload_tasks, timeout=60)
        for t in pending:
            # Get file name from task if available, else generic label
            logger.warning(f"Abandoned in-flight upload task: {t.get_name()}")
            t.cancel()
    # ... rest of shutdown
```

**Note:** Identifying "upload tasks" by name requires tasks to be created with `name=` kwarg in `_upload_files`. Alternatively, maintain a set of active upload tasks in `FileListener` for cleaner tracking.

### Anti-Patterns to Avoid

- **`call_soon_threadsafe(asyncio.create_task, coro)`**: On Python 3.10+, this silently drops real-time events. The coroutine is created in the watchdog thread but `create_task` has no running loop reference — use `run_coroutine_threadsafe` instead. [VERIFIED: confirmed bug exists at `folder_watcher.py:101-102`]
- **`asyncio.wait_for(already_created_task, timeout)`**: When you call `wait_for` on a task that was already created with `create_task`, you are not wrapping the coroutine — you are racing a timeout against a task that may already be partially complete. Wrap the coroutine before creating the task.
- **`asyncio.get_event_loop()` outside a coroutine**: Raises `DeprecationWarning` on Python 3.10+ and will raise `RuntimeError` in a future version. Use `asyncio.get_running_loop()` inside a coroutine.
- **Module-level `asyncio.Semaphore()`**: Semaphores must be created inside the same event loop they are used in. `FileListener.__init__` is called from async context so it is safe, but do not move semaphore creation to module-level code.
- **Instantiating `IgnoreRules` inside `FileListener.__init__` and `FileChangeHandler.__init__`**: Creates diverging instances. Use the `IGNORE_RULES` singleton.

---

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Glob pattern matching | Custom regex or `str.endswith` chains | `fnmatch.fnmatch` | Handles `*`, `?`, `[seq]` correctly; already stdlib |
| Thread→asyncio scheduling | `call_soon_threadsafe(create_task, coro)` | `asyncio.run_coroutine_threadsafe(coro, loop)` | Returns a `Future`; safe across Python 3.10+ |
| Concurrent task fan-out with error isolation | `for t in tasks: await t` | `asyncio.gather(*tasks, return_exceptions=True)` | Single call, parallel execution, exception captured not raised |
| Async file I/O | `open()` in an async context | `aiofiles.open()` | Truly async; blocking `open()` blocks the event loop |
| Read-modify-write safety | Ad-hoc flags | `asyncio.Lock` per folder | Prevents interleaved reads/writes without threading overhead |

---

## Common Pitfalls

### Pitfall 1: `wait_for` on a pre-created task wraps only the *timeout*, not the *coroutine*

**What goes wrong:** `task = create_task(coro); await wait_for(task, timeout=300)` — if the task finishes in 1s, `wait_for` returns correctly. But if the timeout fires, `wait_for` cancels the task. The concern is that calling `wait_for` on an already-running task reuses the existing task rather than wrapping the coroutine in a new `shield`. This is actually the intended behavior in CPython, but the *real* problem is that a serial `for` loop of `wait_for(task)` calls means the second timeout doesn't start until the first one completes or times out.
**Why it happens:** `_upload_files` creates all tasks eagerly (good: all start concurrently) but then awaits them in a serial loop (bad: second file's 300s timeout window starts only after the first file finishes or times out).
**How to avoid:** `asyncio.gather(*[wrap_coro_in_wait_for(c) for c in coroutines], return_exceptions=True)` where `wrap_coro_in_wait_for` wraps the *coroutine* (not the task) in `asyncio.wait_for`. [VERIFIED: behavior confirmed in Python 3.11]

### Pitfall 2: `loop.add_signal_handler` callback must be a plain callable, not a coroutine

**What goes wrong:** `loop.add_signal_handler(signal.SIGTERM, async_func())` — `async_func()` creates a coroutine object, not a callable. The handler is never invoked.
**Why it happens:** `add_signal_handler` expects `Callable[[], None]`, not a coroutine.
**How to avoid:** Use a lambda that calls `asyncio.ensure_future(coro)`:
`loop.add_signal_handler(signal.SIGTERM, lambda: asyncio.ensure_future(self._handle_signal()))` [VERIFIED: tested on macOS]

### Pitfall 3: `asyncio.wait(tasks, timeout=60)` with an empty set raises ValueError

**What goes wrong:** `asyncio.wait(set(), timeout=60)` raises `ValueError: Set of coroutines/Futures is empty.`
**Why it happens:** `asyncio.wait` requires at least one awaitable.
**How to avoid:** Guard the call: `if upload_tasks: done, pending = await asyncio.wait(upload_tasks, timeout=60)` [VERIFIED: CPython behavior]

### Pitfall 4: `asyncio.all_tasks()` includes the current task and infrastructure tasks

**What goes wrong:** Draining "all tasks" during shutdown may include the shutdown coroutine itself, creating a deadlock or infinite wait.
**Why it happens:** `asyncio.all_tasks()` returns every scheduled task in the loop.
**How to avoid:** Exclude `asyncio.current_task()` from the drain set. Better: maintain an explicit set of active upload tasks in `FileListener` (a `Set[asyncio.Task]`) and drain only those. [ASSUMED — standard practice, not tested]

### Pitfall 5: `FileChangeHandler._should_ignore_file` and `FileListener._should_ignore_file` diverge silently

**What goes wrong:** The handler blocks dot-files (`.env`) but the listener does not — so dot-files blocked in real-time events can still be uploaded during the 5-minute scan cycle.
**Why it happens:** Two independent implementations with overlapping but different sets.
**How to avoid:** IGNORE-03 — use `IGNORE_RULES.should_ignore_file()` in both places. [VERIFIED: confirmed divergence by reading both implementations]

### Pitfall 6: pyproject.toml entrypoint points to non-existent module

**What goes wrong:** `uv run aws-copier` fails with `ModuleNotFoundError: No module named 'simple_main'`.
**Why it happens:** `pyproject.toml` line 32: `aws-copier = "simple_main:main"` — module `simple_main` does not exist in the project.
**How to avoid:** CONFIG-02 — change to `aws-copier = "main:sync_main"`. [VERIFIED: `main.py` exports `sync_main()`; `simple_main.py` not found in project tree]

---

## Code Examples

### Full `ignore_rules.py` skeleton (IGNORE-03)

```python
# Source: Python stdlib fnmatch + dataclasses [VERIFIED: syntax tested]
"""Centralized ignore rules for file and directory filtering."""

import fnmatch
from dataclasses import dataclass
from pathlib import Path
from typing import FrozenSet

logger = logging.getLogger(__name__)

# ... GLOB_PATTERNS, SENSITIVE_DENY, IGNORE_DIRS frozensets as shown in Pattern 4 above ...

@dataclass(frozen=True)
class IgnoreRules:
    """Centralized, immutable ignore-rule set consumed by FileListener and FileChangeHandler."""
    glob_patterns: FrozenSet[str] = GLOB_PATTERNS
    sensitive_deny: FrozenSet[str] = SENSITIVE_DENY
    ignore_dirs: FrozenSet[str] = IGNORE_DIRS

    def should_ignore_file(self, path: Path) -> bool:
        """Return True if the file must not be uploaded to S3."""
        ...

    def should_ignore_dir(self, path: Path) -> bool:
        """Return True if the directory should be skipped entirely."""
        ...

IGNORE_RULES: IgnoreRules = IgnoreRules()
```

### _load_backup_info with aiofiles (ASYNC-03)

```python
# Source: aiofiles library [VERIFIED: tested in project venv]
async def _load_backup_info(self, backup_info_file: Path) -> Dict[str, str]:
    if not backup_info_file.exists():
        return {}
    try:
        async with self._get_folder_lock(backup_info_file.parent):
            async with aiofiles.open(backup_info_file, "r", encoding="utf-8") as f:
                content = await f.read()
        data = json.loads(content)
        return data.get("files", {})
    except Exception as e:
        logger.warning(f"Failed to load backup info from {backup_info_file}: {e}")
        return {}
```

---

## Runtime State Inventory

This phase is a code/config refactor — no rename or migration. No runtime state to inventory.

**None — verified**: No stored data, live service config, OS-registered state, secrets, or build artifacts reference strings being changed in this phase.

---

## Environment Availability

| Dependency | Required By | Available | Version | Fallback |
|------------|------------|-----------|---------|----------|
| Python 3.11 | All code | Yes | 3.11.11 | — |
| uv | Project management | Yes | 0.11.6 | — |
| pytest | Tests | Yes | 8.4.1 | — |
| pytest-asyncio | Async tests | Yes | in venv | — |
| moto[s3] | S3 mock tests | Yes | in venv | — |
| aiofiles | ASYNC-03 | Yes | >=24.1.0 | — |
| watchdog | ASYNC-01 | Yes | >=3.0.0 | — |
| asyncio | All async | Yes | stdlib | — |
| fnmatch | IGNORE-01 | Yes | stdlib | — |
| signal | ASYNC-06 | Yes | stdlib | — |

**No missing dependencies.** All required libraries are present in the project venv.

---

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| `call_soon_threadsafe(create_task, coro)` | `run_coroutine_threadsafe(coro, loop)` | Python 3.10 (DeprecationWarning; behavior change) | Real-time events silently dropped in current code |
| `asyncio.get_event_loop()` | `asyncio.get_running_loop()` | Python 3.10 | `get_event_loop()` emits DeprecationWarning; `get_running_loop()` raises if no loop |
| `asyncio.BaseEventLoop` annotation | `asyncio.AbstractEventLoop` annotation | Python 3.10+ (style convention) | `BaseEventLoop` is an implementation detail, `AbstractEventLoop` is the public contract |
| Blocking `open()` in async context | `aiofiles.open()` | Project best-practice (not a Python version change) | Blocks the event loop on every file read/write |

**Deprecated/outdated:**
- `asyncio.get_event_loop()` outside a coroutine: DeprecationWarning in 3.10+, RuntimeError planned for 3.12+ [CITED: https://docs.python.org/3.11/library/asyncio-eventloop.html#asyncio.get_event_loop]
- `asyncio.BaseEventLoop` as a type annotation: `AbstractEventLoop` is the public interface; `BaseEventLoop` is an implementation detail not part of the documented public API [ASSUMED]

---

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| A1 | Maintaining an explicit `Set[asyncio.Task]` in `FileListener` is cleaner than filtering `asyncio.all_tasks()` for the drain | Common Pitfalls #4 | Low — either approach works; explicit set is more robust |
| A2 | `asyncio.BaseEventLoop` as annotation is non-public API / style convention to avoid | State of the Art | Low — both compile; only a style/forward-compat concern |
| A3 | IGNORE-02 deny list should include `.npmrc`, `.pypirc`, `.netrc`, `*.p12`, `*.pfx`, `*.secret` in addition to SSH keys and `.pem`/`.key` | Pattern 4 (IgnoreRules) | Low — adding more patterns is safe; missing a pattern means a secret reaches S3 |

---

## Open Questions

1. **Should upload tasks be tracked explicitly or filtered from `asyncio.all_tasks()`?**
   - What we know: `asyncio.all_tasks()` includes infrastructure tasks (status loop, watcher) that should not be drained
   - What's unclear: Whether giving tasks a name prefix (`asyncio.create_task(coro, name="upload-...")`) is sufficient to identify them
   - Recommendation: Maintain `self._active_upload_tasks: Set[asyncio.Task]` in `FileListener`; add on task creation, discard on completion. Cleanest and most explicit.

2. **Which IGNORE-02 sensitive-file patterns should be in the deny list?**
   - What we know: User expects `.env`, SSH keys, `.pem` blocked (from REQUIREMENTS.md)
   - What's unclear: Exact list beyond those — `.npmrc`? `.pypirc`? `*.secret`?
   - Recommendation: Start with the list in Pattern 4. It is Claude's discretion per D-06.

---

## Security Domain

ASVS categories for this phase:

| ASVS Category | Applies | Standard Control |
|---------------|---------|-----------------|
| V5 Input Validation | Yes — file path validation | `fnmatch` + explicit deny list in `IgnoreRules` |
| V6 Cryptography | No | MD5 for deduplication only, not security |
| V2 Authentication | No | AWS credentials unchanged in this phase |

### Known Threat Pattern: Secret File Upload

| Pattern | STRIDE | Standard Mitigation |
|---------|--------|---------------------|
| `.env` / SSH private key in watched folder silently uploaded to S3 | Information Disclosure | IGNORE-02 hardcoded deny list in `IgnoreRules.should_ignore_file` |
| Glob pattern `*.pyc` not matched via set membership | Tampering (data correctness) | IGNORE-01: use `fnmatch.fnmatch` for all patterns |

---

## Sources

### Primary (HIGH confidence)
- Python stdlib `asyncio` — `run_coroutine_threadsafe`, `all_tasks`, `wait`, `gather`, `add_signal_handler` all verified via `python3 -c` and `uv run python -c` in the project venv
- Python stdlib `fnmatch` — `fnmatch.fnmatch` behavior verified for all patterns used in ignore rules
- Python stdlib `dataclasses` — `@dataclass(frozen=True)` with instance methods verified
- `aiofiles` library — async read/write with `asyncio.Lock` tested in project venv

### Secondary (MEDIUM confidence)
- Codebase grep — confirmed exact line numbers for all bugs: `folder_watcher.py:101-102` (ASYNC-01), `file_listener.py:34` (CONFIG-01), `file_listener.py:215` (ASYNC-03), `pyproject.toml:32` (CONFIG-02), `simple_config.py:42-45` (CONFIG-03)
- Existing tests confirmed passing (103 passed, 2 warnings) — baseline established

### Tertiary (LOW confidence)
- None

---

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH — all libraries stdlib or already installed; verified importable
- Architecture: HIGH — bugs confirmed by code inspection; fix patterns verified in Python 3.11
- Pitfalls: HIGH — bugs confirmed by direct grep of source; fixes tested inline

**Research date:** 2026-04-24
**Valid until:** 2026-12-01 (stdlib APIs are stable; watchdog API unchanged)
