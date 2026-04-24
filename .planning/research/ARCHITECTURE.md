# Architecture Patterns: Async Python S3 Backup Daemon

**Domain:** Async Python daemon with watchdog filesystem events and S3 I/O
**Researched:** 2026-04-24
**Overall confidence:** HIGH (all patterns verified against official Python 3.11 docs)

---

## Concern 1: Shared Ignore Patterns Across FileListener and FileChangeHandler

### Current State

Both classes define their own hardcoded `ignore_patterns` set in `__init__`. The sets have
diverged: `FileChangeHandler` puts `$RECYCLE.BIN` and `System Volume Information` in
`ignore_patterns` (treated as file names), while `FileListener` correctly puts them in
`ignore_dirs`. Because there is no shared source of truth, a pattern added to fix a bug in
one class silently stays broken in the other.

There is a second problem compounding this: neither class actually evaluates glob patterns.
`"*.pyc"` is in both sets, but `FileListener._should_ignore_file` only does set-membership
and `startswith` checks, so `report.pyc` is never matched. `FileChangeHandler._should_ignore_file`
has the same flaw. The glob entries are dead weight until `fnmatch` is introduced.

### Recommended Approach

Extract all ignore data into a module-level frozen dataclass in a new
`aws_copier/core/ignore_rules.py` module. The dataclass holds three separate collections
that map to the three distinct matching strategies currently scattered across both classes:
exact names, glob patterns, and directory names.

```python
# aws_copier/core/ignore_rules.py
import fnmatch
from dataclasses import dataclass, field
from pathlib import Path

@dataclass(frozen=True)
class IgnoreRules:
    exact_names: frozenset[str] = field(default_factory=frozenset)
    glob_patterns: frozenset[str] = field(default_factory=frozenset)
    dir_names: frozenset[str] = field(default_factory=frozenset)

    def should_ignore_file(self, path: Path) -> bool:
        name = path.name
        if name.startswith("."):
            return True
        if name in self.exact_names:
            return True
        return any(fnmatch.fnmatch(name, pat) for pat in self.glob_patterns)

    def should_ignore_directory(self, path: Path) -> bool:
        name = path.name
        if name.startswith("."):
            return True
        return name in self.dir_names


DEFAULT_IGNORE_RULES = IgnoreRules(
    exact_names=frozenset({
        ".DS_Store", "Thumbs.db", "desktop.ini",
        "hiberfil.sys", "pagefile.sys", "swapfile.sys",
        ".gitignore", ".milo_backup.info",
    }),
    glob_patterns=frozenset({
        "*.pyc", "*.bak", "*.backup", "*~",
        "*.tmp", "*.temp", "*.swp", "*.swo",
    }),
    dir_names=frozenset({
        ".git", ".svn", ".hg", "__pycache__",
        ".pytest_cache", "node_modules", ".venv", "venv",
        ".vscode", ".idea", ".aws-copier",
        "$RECYCLE.BIN", "System Volume Information",
        ".Trashes", ".Spotlight-V100", ".fseventsd",
    }),
)
```

Both `FileListener` and `FileChangeHandler` drop their local sets and accept an
`IgnoreRules` instance via constructor injection, defaulting to `DEFAULT_IGNORE_RULES`.
This also fixes the `ignored_files` counter bug at the same time: the counter is
incremented wherever `should_ignore_file` returns `True`, which is now a single call site
per class.

```python
# FileListener
def __init__(self, config, s3_manager, ignore_rules=DEFAULT_IGNORE_RULES):
    self._ignore = ignore_rules

# FileChangeHandler
def __init__(self, config, watch_folder, file_listener, event_loop,
             ignore_rules=DEFAULT_IGNORE_RULES):
    self._ignore = ignore_rules
```

Using `frozen=True` prevents accidental mutation at runtime and makes the object safely
shareable across the watcher thread and the asyncio thread without any locking.

### Migration Complexity: Easy

The change is purely additive. Create `ignore_rules.py`, wire it through constructors,
remove the two inline sets, and update the three `_should_ignore_*` call sites. No
behavioral change for code that uses the default rules. Unit tests for `IgnoreRules` can
cover all three matching strategies independently, including the glob paths that are
currently untestable.

---

## Concern 2: Bridging Watchdog's Sync Thread to the asyncio Event Loop

### Current State

`FileChangeHandler.on_any_event` (a synchronous watchdog callback on the OS monitor
thread) bridges into asyncio with:

```python
self.event_loop.call_soon_threadsafe(
    asyncio.create_task, self._process_changed_file(file_path, event.event_type)
)
```

This is the canonical "wrong-loop" anti-pattern. `asyncio.create_task` is a module-level
function that calls `get_running_loop()` internally. When Python executes the callback on
the event loop thread, `asyncio.create_task` will see the event loop is running and
schedule the task — but only in CPython where the event loop happens to be running at the
exact moment the callback fires. The coroutine object is also constructed eagerly on the
watchdog thread (`self._process_changed_file(...)` is called immediately), before the event
loop picks it up. Under PyPy or if the loop is momentarily idle, this raises
`RuntimeError: no running event loop`. The type annotation `asyncio.BaseEventLoop` on the
parameter is also deprecated since Python 3.10; the public interface is
`asyncio.AbstractEventLoop`.

### Recommended Approach

Use `asyncio.run_coroutine_threadsafe`. This is the only API explicitly documented as the
correct way to submit a coroutine from a non-asyncio thread to a running event loop
(Python docs: "This function is meant to be called from a different OS thread than the one
where the event loop is running").

```python
# aws_copier/core/folder_watcher.py

import asyncio
from asyncio import AbstractEventLoop

class FileChangeHandler(FileSystemEventHandler):
    def __init__(
        self,
        config: SimpleConfig,
        watch_folder: Path,
        file_listener: FileListener,
        event_loop: AbstractEventLoop,   # corrected type
        ignore_rules=DEFAULT_IGNORE_RULES,
    ):
        ...
        self.event_loop = event_loop

    def on_any_event(self, event: FileSystemEvent) -> None:
        try:
            if event.is_directory:
                return
            if event.event_type not in ("created", "modified"):
                return

            file_path = Path(event.src_path)
            if self._ignore.should_ignore_file(file_path):
                return
            if not file_path.exists():
                return

            # Correct: submit coroutine from non-asyncio thread
            asyncio.run_coroutine_threadsafe(
                self._process_changed_file(file_path, event.event_type),
                self.event_loop,
            )
        except Exception as e:
            logger.error(f"Error handling file system event: {e}")
```

`run_coroutine_threadsafe` returns a `concurrent.futures.Future`. For a fire-and-forget
handler like this one, the return value can be discarded. If you later need to track
whether the processing succeeded (e.g., for backpressure or bounded queuing), you can
attach a done-callback:

```python
future = asyncio.run_coroutine_threadsafe(coro, self.event_loop)
future.add_done_callback(lambda f: f.exception() and logger.error(...))
```

There is a subtle but important secondary issue: `FolderWatcher.start()` stores
`asyncio.get_running_loop()` at start time. This is correct when running headless
(`main.py`), where a single event loop lives for the whole process lifetime. In GUI mode
(`main_gui.py`), the background loop is a different loop object from the one on the main
thread. `FolderWatcher` already receives `self.event_loop` from the background thread
when `start()` is awaited there, so the reference is correct — no change needed for GUI
mode, but a clarifying comment is warranted.

### Migration Complexity: Easy

One line change in `on_any_event`: replace `call_soon_threadsafe(asyncio.create_task, coro)`
with `run_coroutine_threadsafe(coro, self.event_loop)`. Fix the type annotation.
No structural changes needed.

---

## Concern 3: Concurrent S3 Uploads with asyncio.gather and Per-Task Timeouts

### Current State

`_upload_files` creates all upload tasks with `asyncio.create_task`, then iterates the
list and awaits each task individually inside a `for` loop:

```python
for filename, task in upload_tasks:
    result = await asyncio.wait_for(task, timeout=300)
```

This is serial execution. The second task does not start until the first `await` returns.
`create_task` schedules the coroutines to become runnable, but they only run when the
event loop gets control — which only happens at an `await` point. Since each iteration
blocks waiting for its own task, earlier tasks never run concurrently. The semaphore's 50
slots are never exercised: effective concurrency is 1.

### Recommended Approach

Replace the serial loop with `asyncio.gather` plus per-task `asyncio.wait_for` wrappers.
This is the correct pattern for Python 3.11 (where `asyncio.timeout` is available, but
`asyncio.wait_for` remains cleaner for mapping per-item timeouts into a gather call).

```python
async def _upload_files(
    self, files_to_upload: list[str], folder_path: Path
) -> list[str]:
    if not files_to_upload:
        return []

    logger.info(
        f"Starting concurrent upload of {len(files_to_upload)} files "
        f"(semaphore limit {self.upload_semaphore._value})"
    )

    # Wrap each coroutine with a per-file timeout before passing to gather.
    # The semaphore inside _upload_single_file limits real concurrency.
    async def upload_with_timeout(filename: str) -> tuple[str, bool]:
        try:
            result = await asyncio.wait_for(
                self._upload_single_file(filename, folder_path),
                timeout=300,
            )
            return filename, result
        except asyncio.TimeoutError:
            logger.error(f"Upload timeout for {filename} (5 minutes)")
            self._stats["errors"] += 1
            return filename, False

    results = await asyncio.gather(
        *(upload_with_timeout(name) for name in files_to_upload),
        return_exceptions=True,
    )

    uploaded = []
    for item in results:
        if isinstance(item, BaseException):
            logger.error(f"Upload task raised unexpectedly: {item}")
            self._stats["errors"] += 1
        else:
            filename, success = item
            if success:
                uploaded.append(filename)
            else:
                self._stats["errors"] += 1

    logger.info(
        f"Completed upload: {len(uploaded)}/{len(files_to_upload)} succeeded"
    )
    return uploaded
```

Key design decisions:

1. `return_exceptions=True` — `gather` does not cancel the rest of the batch if one task
   raises. This matches the existing log-and-continue error strategy for a backup daemon.
   Individual exceptions are inspected after the gather completes.

2. `asyncio.wait_for` wraps each coroutine before gather receives it, not the task.
   Wrapping after `create_task` can leave the original task orphaned when `wait_for`
   raises `TimeoutError`; wrapping the coroutine directly avoids that leak.

3. The semaphore in `_upload_single_file` (`async with self.upload_semaphore`) remains
   the actual concurrency throttle. `gather` submits all N tasks simultaneously; the
   semaphore ensures at most `max_concurrent_uploads` are in flight at once.

4. With this change, wiring `config.max_concurrent_uploads` to the semaphore in
   `__init__` becomes meaningful:
   ```python
   self.upload_semaphore = asyncio.Semaphore(config.max_concurrent_uploads)
   ```

**Note on TaskGroup:** `asyncio.TaskGroup` (Python 3.11+) provides structured concurrency
with automatic cancellation on first exception. It is the right choice when a single
failure should abort the whole batch. For this daemon's log-and-continue strategy —
where one failed upload must not abort the other 49 — `gather(return_exceptions=True)` is
the correct primitive. TaskGroup would be appropriate if the design shifts to a
fail-fast-and-retry-whole-batch model.

### Migration Complexity: Easy

Drop-in replacement for the `for` loop in `_upload_files`. No callers change; the return
type `list[str]` is preserved. The semaphore in `_upload_single_file` needs no change.

---

## Concern 4: Per-Directory .milo_backup.info State File Approach

### Current State

Each directory gets a `.milo_backup.info` JSON file mapping `{filename: md5}`. The
complete file is read at the start of every scan and rewritten at the end if anything
changed. There is no in-memory cache between scan cycles, so repeated 5-minute status
loops re-read and re-hash all files even when nothing changed.

A second issue: both `_load_backup_info` and `_update_backup_info` are `async` methods
that use synchronous `open()` for I/O, blocking the event loop on every disk access.
`aiofiles` is already a project dependency and is used elsewhere (in `_calculate_md5`).

### Recommended Approach

The per-directory `.milo_backup.info` design is fundamentally sound for this use case.
The alternatives (a central SQLite database, an S3 manifest, or a Redis cache) all add
dependencies or network round-trips without meaningful benefit for a personal single-node
daemon. The design decision in `PROJECT.md` is correct: keep local state files.

Two targeted fixes make the current approach correct and non-blocking:

**Fix 1 — Use aiofiles for all backup info I/O:**

```python
async def _load_backup_info(self, backup_info_file: Path) -> dict[str, str]:
    if not backup_info_file.exists():
        return {}
    try:
        async with aiofiles.open(backup_info_file, "r", encoding="utf-8") as f:
            data = json.loads(await f.read())
        return data.get("files", {})
    except Exception as e:
        logger.warning(f"Failed to load backup info from {backup_info_file}: {e}")
        return {}

async def _update_backup_info(
    self, backup_info_file: Path, backup_files: dict[str, str]
) -> None:
    payload = json.dumps(
        {"timestamp": datetime.now().isoformat(), "files": backup_files},
        indent=2,
    )
    try:
        async with aiofiles.open(backup_info_file, "w", encoding="utf-8") as f:
            await f.write(payload)
    except Exception as e:
        logger.error(f"Failed to update backup info {backup_info_file}: {e}")
```

`aiofiles.open` delegates file I/O to a thread pool executor, so the event loop remains
unblocked during disk access. This matters for network-mounted volumes and slow HDDs
where a single `open()` call can stall for hundreds of milliseconds.

**Fix 2 — Optional in-memory cache to avoid redundant re-reads:**

For the 5-minute status loop, the scan currently re-reads every `.milo_backup.info` file
even when no watchdog event has fired for that directory. A simple dict cache on
`FileListener` avoids this without changing the correctness model:

```python
# In __init__
self._backup_info_cache: dict[Path, dict[str, str]] = {}

# In _load_backup_info: return cache hit if file mtime unchanged
async def _load_backup_info(self, backup_info_file: Path) -> dict[str, str]:
    if not backup_info_file.exists():
        return {}
    try:
        mtime = backup_info_file.stat().st_mtime
        cached = self._backup_info_cache.get(backup_info_file)
        if cached is not None and cached["_mtime"] == mtime:
            return cached["files"]
        # ... load from disk, store with mtime ...
    except Exception:
        ...
```

This is a performance improvement, not a bug fix. Do it in a second pass after the
aiofiles fix is validated.

**On the design overall:** storing `.milo_backup.info` inside each watched directory
means it is visible to the user, Git-indexable, and accidentally backed up to S3 if the
ignore rule is missing. The ignore rule exists (`".milo_backup.info"` is in both sets),
but the glob-pattern bug means a file named anything other than exactly
`.milo_backup.info` would not be caught. Once `IgnoreRules` is in place (Concern 1), the
exact-name match will work correctly regardless of the glob fix.

### Migration Complexity: Easy (aiofiles fix) / Medium (cache)

The aiofiles change is a direct substitution in two methods with no interface change.
The cache adds a small amount of state to `FileListener.__init__` and a conditional read
path; test it against the periodic scan loop to verify cache invalidation is correct
after a successful upload (the `_update_backup_info` call must also clear or update the
cache entry).

---

## Concern 5: Signal Handling in a Python asyncio Daemon

### Current State

`_setup_signal_handlers()` is defined but the call is commented out in `main.py`. The
existing implementation uses `signal.signal()` — the standard library's synchronous signal
API — with a callback that calls `asyncio.create_task(self._set_shutdown_event())`. This
is incorrect: `signal.signal` callbacks execute on the main thread between bytecode
instructions, not inside the event loop. Calling `asyncio.create_task` there has the same
wrong-loop problem as Concern 2.

### Recommended Approach

Use `loop.add_signal_handler()` for Unix (macOS, Linux) and fall back to
`signal.signal` with `call_soon_threadsafe` for Windows. `loop.add_signal_handler` is
the only asyncio-safe signal API: it registers a synchronous callback that runs inside
the event loop's regular callback queue, so it can safely call `loop.call_soon_threadsafe`
or set an `asyncio.Event` without any cross-thread issues.

`loop.add_signal_handler` is explicitly documented as Unix-only. The correct cross-platform
pattern is a try/except on `NotImplementedError`:

```python
# main.py — inside AWSCopierApp

def _setup_signal_handlers(self) -> None:
    """Register graceful-shutdown handlers for SIGINT and SIGTERM."""
    loop = asyncio.get_running_loop()

    def _request_shutdown(signame: str) -> None:
        logger.info(f"Received {signame}, shutting down...")
        # shutdown_event.set() is safe here: we're inside the event loop
        self.shutdown_event.set()

    try:
        # Unix (macOS, Linux) — event-loop-safe, runs in the loop's callback queue
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(
                sig,
                functools.partial(_request_shutdown, sig.name),
            )
        logger.debug("Signal handlers registered via loop.add_signal_handler")

    except (NotImplementedError, AttributeError):
        # Windows — loop.add_signal_handler is not available
        # Use signal.signal + call_soon_threadsafe to cross the thread boundary
        def _windows_handler(signum: int, _frame) -> None:
            signame = signal.Signals(signum).name
            loop.call_soon_threadsafe(
                functools.partial(_request_shutdown, signame)
            )

        signal.signal(signal.SIGINT, _windows_handler)
        try:
            signal.signal(signal.SIGBREAK, _windows_handler)
        except (AttributeError, OSError):
            pass
        logger.debug("Signal handlers registered via signal.signal (Windows fallback)")
```

Then uncomment the call in `start()`:

```python
async def start(self) -> None:
    await self.s3_manager.initialize()
    self._setup_signal_handlers()    # <-- re-enable this line
    await self.file_listener.scan_all_folders()
    ...
```

`_setup_signal_handlers` must be called from within a running asyncio coroutine so that
`asyncio.get_running_loop()` returns the loop that owns `shutdown_event`. Calling it
before `asyncio.run(main())` or after the loop stops will raise `RuntimeError`. The
current placement after `initialize()` is correct.

**Why not `signal.signal` + `asyncio.create_task` (the current broken pattern)?**
`signal.signal` handlers execute on the main thread. `asyncio.create_task` requires a
running event loop on the calling thread. In headless mode, `asyncio.run()` drives the
event loop on the main thread, so `create_task` happens to work — but it is relying on
an implementation detail of when the signal fires. The documented and safe approach is to
use `loop.add_signal_handler` (Unix) or `call_soon_threadsafe` (Windows) so the handler
is explicitly aware of which loop to target.

**GUI mode note:** `_setup_signal_handlers` in `main_gui.py` (line 160) runs before the
background loop starts, so `asyncio.get_running_loop()` is not yet available. The GUI
app's signal handling should reference the background loop directly via
`self.loop.call_soon_threadsafe`. This is the fragile area noted in CONCERNS.md; the
recommended fix is the same `call_soon_threadsafe` pattern shown in the Windows fallback
above, wired to `self.loop` after it is created.

### Migration Complexity: Medium

The logic is simple, but the three-platform path (Unix, Windows, GUI mode) requires
care. Uncomment the call, replace `signal.signal` with `loop.add_signal_handler`, add
the Windows fallback, and add an integration test that sends `SIGTERM` to the process
and asserts clean shutdown. The GUI path is a separate, independent change.

---

## Summary: Migration Order

| Concern | Complexity | Do First? |
|---------|------------|-----------|
| 1. Shared IgnoreRules module + fnmatch | Easy | Yes — unblocks stat counter fix |
| 2. run_coroutine_threadsafe for watchdog | Easy | Yes — active crash risk |
| 3. asyncio.gather for concurrent uploads | Easy | Yes — performance blocker |
| 4a. aiofiles for backup info I/O | Easy | Yes — correctness in async context |
| 4b. In-memory backup info cache | Medium | Later — perf-only |
| 5. Signal handling re-enable | Medium | After 1-4 — reliability milestone |

Concerns 1, 2, 3, and 4a are all self-contained and safe to implement in the same
milestone. None of them change public interfaces or require callers to update. Concern 5
should follow because it benefits from a stable, tested shutdown path (verifying that
in-flight gather tasks cancel cleanly on SIGTERM requires Concerns 3 and 4a to be correct
first).

---

## Sources

- Python 3.11 docs — `asyncio.run_coroutine_threadsafe`:
  https://docs.python.org/3/library/asyncio-task.html#asyncio.run_coroutine_threadsafe
- Python 3.11 docs — `loop.add_signal_handler` (Unix only):
  https://docs.python.org/3/library/asyncio-eventloop.html#asyncio.loop.add_signal_handler
- Python 3.11 docs — `asyncio.gather` and TaskGroup comparison:
  https://docs.python.org/3/library/asyncio-task.html#asyncio.gather
- Python 3.11 docs — `asyncio.timeout` context manager (3.11+):
  https://docs.python.org/3/library/asyncio-task.html#asyncio.timeout
- Python 3.11 docs — `asyncio.wait_for` per-task timeout:
  https://docs.python.org/3/library/asyncio-task.html
- Developing with asyncio — thread safety guidelines:
  https://docs.python.org/3/library/asyncio-dev.html
