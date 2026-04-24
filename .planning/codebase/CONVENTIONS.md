# Coding Conventions

**Analysis Date:** 2026-04-24

## Naming Patterns

**Files:**
- Module files use `snake_case`: `file_listener.py`, `s3_manager.py`, `folder_watcher.py`, `simple_config.py`
- Test files use `test_` prefix: `test_file_listener.py`, `test_s3_manager.py`
- Entry point scripts use `snake_case`: `main.py`, `main_gui.py`

**Classes:**
- `PascalCase` for all classes: `FileListener`, `S3Manager`, `FolderWatcher`, `SimpleConfig`, `FileChangeHandler`

**Methods/Functions:**
- `snake_case` for all methods and functions: `scan_all_folders`, `upload_file`, `check_exists`, `get_s3_name_for_folder`
- Private methods prefixed with `_`: `_process_folder_recursively`, `_calculate_md5`, `_build_s3_key`, `_get_or_create_client`
- Async methods are not specially named — async-ness is indicated only by `async def`

**Variables:**
- `snake_case` for all variables: `backup_info_file`, `files_to_upload`, `upload_semaphore`
- Private instance attributes prefixed with `_`: `self._stats`, `self._s3_client`, `self._exit_stack`
- Constants use `UPPER_SNAKE_CASE`: `DEFAULT_CONFIG_PATH`

**Types:**
- Type hints used throughout all public and private methods
- `Optional[str]` over `str | None` (Python 3.9 target, pre-union syntax)
- `Dict`, `List` from `typing` (not builtin `dict`, `list`) for compatibility with Python 3.9

## Code Style

**Formatting:**
- Tool: `ruff-format` (via pre-commit hook) + `black` in dev dependencies
- Line length: 120 characters (ruff config), 88 characters (black config) — ruff takes precedence
- Python target version: 3.9

**Linting:**
- Tool: `ruff`
- Ignored rules: `E501` (line length), `E402` (module-level imports not at top), `E731` (lambda assignment), `E741` (ambiguous variable names), `E712` (comparison to True/False)
- Pre-commit hook auto-fixes linting issues with `--fix` flag

**Type Checking:**
- Tool: `mypy` (strict mode) + `ty` (pre-commit hook, exit-zero — non-blocking)
- mypy config enforces: `disallow_untyped_defs`, `disallow_incomplete_defs`, `warn_return_any`

## Import Organization

**Order:**
1. Standard library imports (`asyncio`, `hashlib`, `json`, `logging`, `pathlib`, `typing`)
2. Third-party imports (`aiofiles`, `aiobotocore`, `watchdog`, `yaml`)
3. Internal project imports (`from aws_copier.core...`, `from aws_copier.models...`)

**Style:**
- Each group separated by a blank line
- No path aliases — always use full package paths (`from aws_copier.core.s3_manager import S3Manager`)
- `logger = logging.getLogger(__name__)` is always defined at module level immediately after imports

## Error Handling

**Patterns:**
- All public and private async methods wrap logic in `try/except Exception as e`
- Errors are logged with `logger.error(f"...")` and the exception `e` appended
- Error methods return `False` (bool operations) or `None` (Optional returns) — never raise to caller
- `PermissionError` is caught separately and logged as a warning (not error) for directory access
- `ClientError` from botocore is caught specifically for S3 404 checks, then falls through to generic `Exception`
- Abort/cleanup on partial failure (e.g., multipart upload abort on part failure)

Example pattern from `aws_copier/core/s3_manager.py`:
```python
except ClientError as e:
    if e.response["Error"]["Code"] == "404":
        return False  # File doesn't exist
    logger.error(f"Error checking S3 object existence: {e}")
    return False
except Exception as e:
    logger.error(f"Unexpected error checking S3 object: {e}")
    return False
```

## Logging

**Framework:** `logging` (standard library)
**Setup:** `logger = logging.getLogger(__name__)` at module level in every file

**Patterns:**
- `logger.info(f"...")` for normal operation milestones (starting scans, uploading files, stats)
- `logger.debug(f"...")` for verbose per-file details (individual MD5 computations, upload completions)
- `logger.warning(f"...")` for non-fatal issues (missing watch folders, permission denied, cannot verify MD5)
- `logger.error(f"...")` for failures that increment `_stats["errors"]`
- f-strings used for all log message formatting — no `%` formatting

## Comments

**When to Comment:**
- Module-level docstrings on every file (brief one-liner describing purpose)
- Every class has a one-line docstring
- Every method has a Google-style docstring with `Args:` and `Returns:` sections
- Inline comments for non-obvious logic blocks (e.g., `# Skip ignored directories`, `# Ensure forward slashes`)

**Docstring style** (Google format):
```python
async def upload_file(self, local_path: Path, s3_key: str) -> bool:
    """Upload file to S3 with MD5 checksum verification.

    Args:
        local_path: Path to local file
        s3_key: S3 object key

    Returns:
        True if upload successful, False otherwise
    """
```

## Function Design

**Size:** Methods are generally 20-50 lines; complex orchestration methods (`_process_current_folder`, `_upload_files`) run up to 60 lines but are well-commented

**Parameters:** Constructor parameters use `**kwargs` with explicit `.get()` defaults in `SimpleConfig`; all other methods use explicit typed parameters

**Return Values:**
- Boolean (`bool`) for success/failure operations
- `Optional[str]` / `Optional[dict]` for lookups that may find nothing
- `List[str]` for collections of results
- `None` (implicit) for side-effect-only methods

## Module Design

**Exports:** Each `__init__.py` is minimal or empty — imports done directly from submodules

**Class structure:**
- `__init__` initializes all instance state explicitly
- Public methods (no underscore) form the external API
- Private methods (single underscore) are implementation details
- Statistics tracked in `self._stats` dict, exposed via `get_statistics()` / `reset_statistics()`

**Concurrency:**
- `asyncio.Semaphore` used to cap concurrent operations: `upload_semaphore = asyncio.Semaphore(50)`, `md5_semaphore = asyncio.Semaphore(50)`
- `asyncio.create_task()` for fire-and-forget parallel work
- `asyncio.gather(*tasks, return_exceptions=True)` for parallel fan-out with error isolation
- `asyncio.wait_for(..., timeout=N)` on all external I/O calls to prevent hangs

---

*Convention analysis: 2026-04-24*
