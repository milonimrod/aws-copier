# Technology Stack: Async Patterns for aws-copier

**Project:** aws-copier (existing codebase — milestone improvements)
**Researched:** 2026-04-24
**Scope:** asyncio Python 3.11 patterns, aiobotocore lifecycle, aiofiles, thread bridging

---

## 1. Thread-to-Async Bridging: The Watchdog Bug

### The Anti-Pattern (current code)

`FileChangeHandler.on_any_event` does this:

```python
self.event_loop.call_soon_threadsafe(
    asyncio.create_task, self._process_changed_file(file_path, event.event_type)
)
```

This is broken. `call_soon_threadsafe` schedules its first argument as a *callback* to run on `self.event_loop`'s thread. The callback here is `asyncio.create_task`. When `asyncio.create_task` executes it calls `asyncio.get_running_loop()` internally — but at the moment the callback runs, the running loop is `self.event_loop`, so the task lands on the correct loop. However, the coroutine object `self._process_changed_file(...)` is created on the watchdog OS thread *before* the call, which is fine. The real problem is subtler: `asyncio.create_task` is a module-level function that looks up the running loop at the moment it is called. If `self.event_loop` is not the current running loop at the point the callback fires (which is implementation-dependent and prone to break during shutdown or when nested loops are involved), this raises `RuntimeError: no running event loop`.

**The root cause:** `asyncio.create_task` is not designed to be passed as a `call_soon_threadsafe` callback. The canonical, documented pattern for scheduling a coroutine from another thread is `run_coroutine_threadsafe`.

### Correct Pattern: `run_coroutine_threadsafe`

```python
def on_any_event(self, event: FileSystemEvent) -> None:
    # ... filtering logic ...

    asyncio.run_coroutine_threadsafe(
        self._process_changed_file(file_path, event.event_type),
        self.event_loop,
    )
```

`asyncio.run_coroutine_threadsafe(coro, loop)` is the only officially documented thread-safe way to schedule a coroutine onto an already-running event loop from another OS thread. It:
- Is thread-safe by design (uses `call_soon_threadsafe` internally)
- Returns a `concurrent.futures.Future` (can be discarded when fire-and-forget is acceptable)
- Requires the loop to already be running — which it is, since `FolderWatcher.start()` is called from within an async context

### When to use `call_soon_threadsafe` vs `run_coroutine_threadsafe`

| Situation | Use |
|-----------|-----|
| Schedule a plain callable (non-coroutine) from another thread | `loop.call_soon_threadsafe(callback, *args)` |
| Schedule a coroutine from another thread | `asyncio.run_coroutine_threadsafe(coro, loop)` |
| Schedule a coroutine from within async code | `asyncio.create_task(coro)` |

Do not mix these. `call_soon_threadsafe(asyncio.create_task, coro)` is specifically the anti-pattern to avoid.

### loop.create_task via call_soon_threadsafe (alternative)

The alternative fix — `self.event_loop.call_soon_threadsafe(self.event_loop.create_task, coro)` — also works because `loop.create_task` is a bound method on the specific loop instance, not a module-level function. However, `run_coroutine_threadsafe` is cleaner and is what the docs recommend.

### Task Lifecycle Warning

`asyncio.create_task` and `run_coroutine_threadsafe` both create tasks the event loop holds only *weakly*. For fire-and-forget tasks that must not be garbage-collected mid-run, keep a strong reference:

```python
# In __init__
self._background_tasks: set[asyncio.Task] = set()

# When scheduling
future = asyncio.run_coroutine_threadsafe(coro, self.event_loop)
# (run_coroutine_threadsafe returns a concurrent.futures.Future, not a Task —
# GC is not an issue for it; this note applies to bare create_task calls)
```

For `create_task` calls inside the event loop thread:

```python
task = asyncio.create_task(coro())
self._background_tasks.add(task)
task.add_done_callback(self._background_tasks.discard)
```

---

## 2. `asyncio.get_event_loop()` Deprecation

### What changed

`asyncio.get_event_loop()` emits `DeprecationWarning` in Python 3.10+ when called without a running loop, and the behavior has been tightened further in patch releases:

- Python 3.10.0–3.10.8 and 3.11.0: warns if there is no running loop
- Python 3.10.9, 3.11.1, 3.12+: warns only if there is no running loop *and* no current loop is set
- Future Python: will raise `RuntimeError`

**Current bug in `s3_manager.py`:**

```python
loop = asyncio.get_event_loop()             # deprecated
return await loop.run_in_executor(None, _hash_file)
```

This is called from within an async method (a coroutine), so there *is* a running loop. The fix is:

```python
loop = asyncio.get_running_loop()           # correct
return await loop.run_in_executor(None, _hash_file)
```

### Rules

- Inside a coroutine or callback: always use `asyncio.get_running_loop()`. It raises `RuntimeError` immediately if called outside an async context, which surfaces bugs early.
- Outside async code (e.g., sync setup before `asyncio.run()`): use `asyncio.new_event_loop()` to create a loop explicitly. Do not rely on `get_event_loop()` creating one for you.
- `asyncio.get_event_loop()` has no legitimate use in Python 3.11 application code.

---

## 3. `asyncio.gather` vs Serial Awaits

### The Serial Upload Bug

`_upload_files` in `file_listener.py` creates all tasks upfront but then awaits them one-by-one:

```python
for filename, task in upload_tasks:
    result = await asyncio.wait_for(task, timeout=300)  # serial
```

Creating tasks with `asyncio.create_task` does schedule them immediately, so they *can* run concurrently during other awaits. But wrapping each `await` in `wait_for` with a per-task await means only one task is "in focus" at a time. The semaphore is released after each upload, but the next task only starts executing after the previous `wait_for` completes. The effective concurrency is 1 (or, at best, the number of tasks that were already running before the first `await`). The semaphore is bypassed entirely.

### Correct Pattern: `asyncio.gather` with `return_exceptions=True`

```python
async def _upload_files(self, files_to_upload: list[str], folder_path: Path) -> list[str]:
    if not files_to_upload:
        return []

    tasks = [
        asyncio.create_task(
            asyncio.wait_for(
                self._upload_single_file(filename, folder_path),
                timeout=300,
            ),
            name=f"upload-{filename}",
        )
        for filename in files_to_upload
    ]

    results = await asyncio.gather(*tasks, return_exceptions=True)

    uploaded: list[str] = []
    for filename, result in zip(files_to_upload, results):
        if isinstance(result, BaseException):
            logger.error(f"Upload failed for {filename}: {result}")
            self._stats["errors"] += 1
        elif result is True:
            uploaded.append(filename)
        else:
            self._stats["errors"] += 1

    return uploaded
```

Key points:
- `asyncio.gather(*tasks)` starts all tasks and runs them concurrently, yielding control between them during I/O awaits
- `return_exceptions=True` prevents the first failure from cancelling all remaining uploads — critical for a backup tool
- The per-file `wait_for(timeout=300)` is placed *inside* each task so timeouts are per-file, not per-batch
- The semaphore in `_upload_single_file` still limits concurrency correctly; it is entered concurrently across all running tasks

### `gather` vs `TaskGroup` (Python 3.11+)

`asyncio.TaskGroup` (new in 3.11) is the modern structured concurrency primitive. For this codebase, `gather` with `return_exceptions=True` is the better fit:

| | `asyncio.gather(return_exceptions=True)` | `asyncio.TaskGroup` |
|--|--|--|
| One failure cancels others? | No — all run to completion | Yes — first exception cancels group |
| Collect per-item results? | Yes — results list | Requires external collection |
| Best for backup uploads? | Yes — want all attempts, not fail-fast | No — fail-fast is wrong here |

`TaskGroup` is appropriate when *any* failure should abort the batch (e.g., initializing required components). For best-effort parallel uploads, `gather(return_exceptions=True)` is correct.

### When to use each pattern

| Pattern | When to use |
|---------|-------------|
| `await coro()` | Single sequential operation |
| `asyncio.create_task(coro())` + `await task` | Background task you may want to cancel independently |
| `asyncio.gather(*coros, return_exceptions=True)` | Parallel best-effort batch where all results needed |
| `asyncio.gather(*coros)` (no return_exceptions) | Parallel where first failure should propagate immediately |
| `async with asyncio.TaskGroup() as tg:` | Parallel where any failure should cancel the group (structured concurrency) |

---

## 4. aiofiles: When and How to Use It

### The Sync I/O Bug

`_load_backup_info` and `_update_backup_info` are both declared `async` but use blocking `open()`. On a local SSD this is tolerable but it blocks the event loop and defeats the async guarantee. On network-mounted drives or slow disks it causes the entire application to stall.

### Correct Pattern

```python
# Reading JSON with aiofiles
async def _load_backup_info(self, backup_info_file: Path) -> dict[str, str]:
    if not backup_info_file.exists():
        return {}
    try:
        async with aiofiles.open(backup_info_file, "r", encoding="utf-8") as f:
            content = await f.read()
        data = json.loads(content)       # json.loads is CPU-only, not I/O — no await needed
        return data.get("files", {})
    except Exception as e:
        logger.warning(f"Failed to load backup info from {backup_info_file}: {e}")
        return {}

# Writing JSON with aiofiles
async def _update_backup_info(self, backup_info_file: Path, backup_files: dict[str, str]) -> None:
    backup_info = {"timestamp": datetime.now().isoformat(), "files": backup_files}
    content = json.dumps(backup_info, indent=2)   # serialize synchronously before opening file
    try:
        async with aiofiles.open(backup_info_file, "w", encoding="utf-8") as f:
            await f.write(content)
    except Exception as e:
        logger.error(f"Failed to update backup info {backup_info_file}: {e}")
```

### How aiofiles works

aiofiles delegates file operations to a thread pool (using `loop.run_in_executor` internally). It does not use OS-level async file I/O (which Python's asyncio does not expose). The benefit is that blocking disk reads/writes happen on a pool thread rather than the event loop thread, so other coroutines continue running during the wait.

### aiofiles vs `loop.run_in_executor` directly

Both offload to thread pool. aiofiles is the right choice when:
- The code is already using `aiofiles.open` (as this codebase does in `_calculate_md5`)
- You want standard file-like async API

`run_in_executor` is preferable when:
- You need to call an entire sync function atomically (e.g., the MD5 computation in `s3_manager.py` which reads the whole file in one go)
- You're wrapping third-party sync code that is not a simple open/read/write sequence

The codebase already uses `aiofiles` correctly in `_calculate_md5`. The two async methods that use `open()` instead are simply oversights.

### Note on `json.load` / `json.dump`

`json.load(f)` takes a file object. `aiofiles` file objects are not regular file objects — passing them to `json.load` will fail. Always read the full content first with `await f.read()`, then parse with `json.loads(content)`. Similarly, serialize with `json.dumps(data)` first, then write the string.

---

## 5. aiobotocore Session and Client Lifecycle

### Current Usage (correct)

The codebase uses the `AsyncExitStack` pattern:

```python
self._exit_stack = contextlib.AsyncExitStack()
self._s3_client = await self._exit_stack.enter_async_context(
    self._session.create_client("s3", ...)
)
```

This is the recommended pattern for long-lived clients. The key insight from aiobotocore maintainers: **keep as few clients as possible for as long as possible** because each client has its own connection pool. Creating a new client per request discards the pool on every close.

### Version note: `get_session` import path

The codebase imports `from aiobotocore.session import get_session`. This remains valid in aiobotocore 3.x. The CHANGES.rst entry "remove AioSession and get_session top level names" from v1.4 referred to removing `aiobotocore.get_session` (top-level), not `aiobotocore.session.get_session` (module-level). The session-module import is still the documented pattern in aiobotocore 3.5.

### Breaking change in aiobotocore 3.0 (December 2025)

aiobotocore 3.0 forbids "creating loose ClientSession when AioBaseClient exits context." This means: once a client's `__aexit__` is called (i.e., `async with session.create_client(...) as client:` block exits), you cannot use the client again. The `AsyncExitStack` pattern in this codebase sidesteps this — the stack is closed only in `S3Manager.close()`, so the client remains valid for the application's lifetime.

**Risk:** The `initialize()` method creates a *temporary* test client using `async with` that it closes after the `head_bucket` check. This is safe — it's intentionally a short-lived connectivity test. The long-lived client is created separately via `_get_or_create_client()`.

### Caution: close() before re-initialization

After calling `await s3_manager.close()`, the `_exit_stack` is set to `None`. Re-calling `_get_or_create_client()` without re-creating the stack will raise `AttributeError`. The current code creates a new `AsyncExitStack()` if none exists (line 54), which covers this case.

### AioConfig and connection pool sizing

```python
self._client_config = AioConfig(max_pool_connections=100)
```

The pool size should be at least as large as the maximum number of concurrent S3 operations. Since the upload semaphore will be wired to `config.max_concurrent_uploads` (which defaults to 100), `max_pool_connections=100` is appropriate. Keep them in sync.

### `asyncio.wait_for` inside S3 calls

The existing `asyncio.wait_for(..., timeout=300)` wrappers around `put_object` and `upload_part` calls are correct. aiobotocore does not have its own per-call timeout parameter exposed at the Python level in the same way; `wait_for` is the idiomatic Python asyncio approach.

---

## 6. Signal Handling in Async Daemons

### Current bug

`_setup_signal_handlers()` in `main.py` is defined but its call is commented out. It uses `signal.signal(SIGINT, handler)` where the handler calls `asyncio.create_task(self._set_shutdown_event())`. Calling `asyncio.create_task` from a signal handler (which runs on the main thread interrupting the event loop) is not safe — the event loop may be in the middle of executing another step.

### Correct Pattern: `loop.add_signal_handler` (Unix)

```python
async def start(self):
    loop = asyncio.get_running_loop()

    if os.name != "nt":  # Unix/macOS only
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(
                sig,
                self.shutdown_event.set   # plain callable, not a coroutine
            )
    else:
        # Windows: signal.signal is the only option
        signal.signal(signal.SIGINT, lambda s, f: self.shutdown_event.set())
```

`loop.add_signal_handler` is Unix-only. It registers a plain callable (not a coroutine) that the event loop invokes safely between coroutine steps. This is the only signal API that can interact with asyncio objects safely.

For Windows, `signal.signal` is required. The handler must be minimal — set a threading.Event or call `loop.call_soon_threadsafe(shutdown_event.set)` from it, never call asyncio async objects directly.

**Cross-platform shutdown pattern:**

```python
def _schedule_shutdown(self) -> None:
    """Called from signal handler — must be sync and minimal."""
    if self.loop and not self.loop.is_closed():
        self.loop.call_soon_threadsafe(self.shutdown_event.set)
```

Then register it with `signal.signal` on Windows and `loop.add_signal_handler` on Unix.

---

## 7. `asyncio.AbstractEventLoop` Type Annotation

The codebase uses `asyncio.BaseEventLoop` as a type annotation in `FileChangeHandler.__init__`. This was deprecated as a public type in Python 3.10. Replace with `asyncio.AbstractEventLoop`:

```python
def __init__(
    self,
    config: SimpleConfig,
    watch_folder: Path,
    file_listener: FileListener,
    event_loop: asyncio.AbstractEventLoop,   # not BaseEventLoop
):
```

---

## 8. `asyncio.to_thread` vs `run_in_executor`

Python 3.9 introduced `asyncio.to_thread(func, *args)` as a higher-level alternative to `loop.run_in_executor(None, func)`. For this codebase:

```python
# Old (currently in s3_manager._calculate_md5):
loop = asyncio.get_event_loop()
return await loop.run_in_executor(None, _hash_file)

# New (Python 3.9+):
return await asyncio.to_thread(_hash_file)
```

`asyncio.to_thread` automatically uses the running loop's default executor and does not require obtaining the loop object. It is the preferred form in Python 3.11. The fix to `get_event_loop()` can use either; `to_thread` is cleaner.

---

## 9. pytest-asyncio Configuration

The project uses `asyncio_mode = "auto"` in `pyproject.toml`, which means all `async def test_*` functions are automatically treated as async tests. This is correct for this codebase.

The pinned `pytest-asyncio>=1.1.0` in `pyproject.toml` is suspicious — version 1.1.0 does not exist; the library was at 0.x before jumping to its current `0.21+` versioning scheme. The effective constraint is the earlier `pytest-asyncio>=0.21.0` line. This is a minor packaging inconsistency, not a runtime issue.

---

## Summary: Bug-to-Pattern Map

| Bug in codebase | Correct pattern | Section |
|-----------------|-----------------|---------|
| `call_soon_threadsafe(asyncio.create_task, coro)` | `asyncio.run_coroutine_threadsafe(coro, loop)` | §1 |
| `asyncio.get_event_loop()` in s3_manager | `asyncio.get_running_loop()` or `asyncio.to_thread()` | §2 |
| Serial `await` loop in `_upload_files` | `asyncio.gather(*tasks, return_exceptions=True)` | §3 |
| `open()` in async `_load_backup_info` / `_update_backup_info` | `async with aiofiles.open(...)` | §4 |
| `_setup_signal_handlers()` commented out + uses `signal.signal` | `loop.add_signal_handler` (Unix) / `call_soon_threadsafe` (Windows) | §6 |
| `asyncio.BaseEventLoop` type annotation | `asyncio.AbstractEventLoop` | §7 |

---

## Sources

- [Python 3.11 asyncio dev guide — thread safety, signal handlers](https://docs.python.org/3.11/library/asyncio-dev.html)
- [Python 3.11 asyncio tasks — create_task, gather, run_coroutine_threadsafe, deprecations](https://docs.python.org/3.11/library/asyncio-task.html)
- [Python 3.11 asyncio event loop — get_running_loop, add_signal_handler, call_soon_threadsafe](https://docs.python.org/3.11/library/asyncio-eventloop.html)
- [aiobotocore tutorial — session and client creation](https://aiobotocore.aio-libs.org/en/latest/tutorial.html)
- [aiobotocore discussion: per-request vs single client](https://github.com/aio-libs/aiobotocore/discussions/1105) — maintainer: "keep as few clients as possible for as long as possible"
- [aiobotocore discussion: reusable initialization](https://github.com/aio-libs/aiobotocore/discussions/1110)
- [aiobotocore PyPI — version 3.5.0 changelog](https://pypi.org/project/aiobotocore/)
- [aiofiles GitHub — async file I/O via thread pool](https://github.com/Tinche/aiofiles)
- [TaskGroup and asyncio.timeout in Python 3.11](https://www.dataleadsfuture.com/why-taskgroup-and-timeout-are-so-crucial-in-python-3-11-asyncio/)
- [asyncio.gather vs wait — deep comparison](https://superfastpython.com/asyncio-gather-vs-wait/)
