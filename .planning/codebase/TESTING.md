# Testing Patterns

**Analysis Date:** 2026-04-24

## Test Framework

**Runner:**
- `pytest` >= 8.4.1
- Config: `pyproject.toml` `[tool.pytest.ini_options]` section (also a `pytest.ini` file exists but is permission-restricted)

**Assertion Library:**
- pytest built-in `assert` (no third-party assertion library)

**Async Support:**
- `pytest-asyncio` >= 1.1.0
- `asyncio_mode = "auto"` — all `async def test_*` methods run as coroutines automatically without `@pytest.mark.asyncio` decorator (though some tests still include it explicitly for clarity)

**Coverage:**
- `pytest-cov` >= 6.2.1
- Source: `aws_copier` package
- Reports: terminal (`--cov-report=term-missing`) and HTML (`--cov-report=html`)
- Threshold: 30% minimum (`--cov-fail-under=30`)

**Run Commands:**
```bash
# Run all tests
pytest

# Run with verbose output (default via addopts)
pytest -v

# Run specific test file
pytest tests/unit/test_file_listener.py

# Run specific test class
pytest tests/unit/test_file_listener.py::TestFileListenerCore

# Run specific test
pytest tests/unit/test_file_listener.py::TestFileListenerCore::test_scan_all_folders_initial

# Run with coverage report
pytest --cov=aws_copier --cov-report=term-missing
```

## Test File Organization

**Location:** Separate `tests/` directory (not co-located with source)

**Naming:**
- Test files: `test_<module_name>.py`
- Test classes: `Test<ClassName>` (e.g., `TestFileListenerCore`, `TestS3ManagerAWSOperations`)
- Test functions: `test_<description_of_behavior>` (e.g., `test_scan_all_folders_initial`, `test_build_s3_key_with_prefix`)

**Structure:**
```
tests/
├── __init__.py
└── unit/
    ├── __init__.py
    ├── test_file_listener.py          # FileListener tests (564 lines)
    ├── test_folder_watcher.py         # FolderWatcher + FileChangeHandler tests (447 lines)
    ├── test_s3_manager.py             # S3Manager basic tests (257 lines)
    ├── test_s3_manager_comprehensive.py  # S3Manager business logic tests (343 lines)
    └── test_simple_config.py          # SimpleConfig tests (250 lines)
```

Note: `test_gui.py` exists at the project root but is separate from the structured `tests/` directory.

## Test Structure

**Suite Organization:**

Tests are grouped into classes by behavior domain. A single module may have multiple test classes:

```python
class TestFileListenerCore:
    """Test core FileListener functionality."""

    async def test_scan_all_folders_initial(self, file_listener, temp_watch_folder):
        """Test initial scan creates backup info files."""
        ...

class TestFileListenerOperations:
    """Test specific FileListener operations."""
    ...

class TestFileListenerUploads:
    """Test FileListener upload operations."""
    ...
```

Some simpler test modules use top-level functions without classes (e.g., `test_simple_config.py`, basic tests in `test_s3_manager.py`).

**Patterns:**
- Each test method has a descriptive docstring
- Arrange-Act-Assert structure (implicit, no markers)
- Reset mock call counts between logical stages within a single test using `.reset_mock()`
- Explicit assertion on mock call counts (`assert_called_once()`, `assert_not_called()`, `call_count == N`)

## Mocking

**Framework:** `unittest.mock` — `AsyncMock`, `MagicMock`, `patch`

**Patterns:**

For async dependencies (S3Manager), use `AsyncMock`:
```python
@pytest.fixture
def mock_s3_manager():
    mock = AsyncMock()
    mock.upload_file.return_value = True
    mock.check_exists.return_value = False
    return mock
```

For patching module-level imports, use `@patch` decorator:
```python
@patch("aws_copier.core.s3_manager.get_session")
async def test_initialize_success(self, mock_get_session, s3_config):
    mock_session = MagicMock()
    mock_client = AsyncMock()
    mock_session.create_client.return_value.__aenter__ = AsyncMock(return_value=mock_client)
    mock_session.create_client.return_value.__aexit__ = AsyncMock(return_value=None)
    mock_get_session.return_value = mock_session
    ...
```

For context manager mocking (aiobotocore clients), set `__aenter__` and `__aexit__` explicitly on the mock:
```python
mock_s3_client.__aenter__ = AsyncMock(return_value=mock_s3_client)
mock_s3_client.__aexit__ = AsyncMock(return_value=None)
```

For side effects (simulating partial success):
```python
def mock_upload_side_effect(file_path, s3_key):
    filename = file_path.name
    return filename.startswith("success")

file_listener.s3_manager.upload_file.side_effect = mock_upload_side_effect
```

For patching instance methods mid-test:
```python
with patch.object(folder_watcher.observer, "start") as mock_observer_start:
    await folder_watcher.start()
    mock_observer_start.assert_called_once()
```

**What to Mock:**
- All external AWS/S3 calls (`get_session`, S3 client methods)
- `FileListener` when testing `FolderWatcher` (use `AsyncMock` for the whole object)
- `S3Manager` when testing `FileListener` (use `AsyncMock`)
- `Observer` (watchdog) methods when testing `FolderWatcher`
- Event loops (`MagicMock` with custom `call_soon_threadsafe` side effect)

**What NOT to Mock:**
- File system operations — tests use `tempfile.TemporaryDirectory()` for real temp folders
- MD5 calculations — tested against real file content
- `SimpleConfig` — instantiated directly with test parameters
- `asyncio` primitives (semaphores, tasks, gather)

## Fixtures and Factories

**Test Data:**
```python
@pytest.fixture
def temp_watch_folder():
    """Create a temporary folder structure for testing."""
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        (temp_path / "file1.txt").write_text("Content of file 1")
        (temp_path / "file2.txt").write_text("Content of file 2")
        subdir = temp_path / "subdir"
        subdir.mkdir()
        (subdir / "file3.txt").write_text("Content of file 3")
        yield temp_path
```

**Fixture chaining:** Fixtures depend on each other in a chain — `file_listener` depends on `test_config` which depends on `temp_watch_folder`:
```python
@pytest.fixture
def test_config(temp_watch_folder):
    return SimpleConfig(watch_folders=[str(temp_watch_folder)], ...)

@pytest.fixture
def file_listener(test_config, mock_s3_manager):
    return FileListener(test_config, mock_s3_manager)
```

**Location:** All fixtures are defined within each test file (no shared `conftest.py`)

**Temporary files:** `tempfile.TemporaryDirectory()` and `tempfile.NamedTemporaryFile()` used as context managers; cleanup is automatic

## Coverage

**Requirements:** 30% minimum threshold (explicitly noted as low/temporary in config comment)

**View Coverage:**
```bash
pytest --cov=aws_copier --cov-report=term-missing
# HTML report generated to htmlcov/
```

## Test Types

**Unit Tests:**
- All tests in `tests/unit/` are unit tests
- Each test isolates one class/module by mocking its dependencies
- No integration with real AWS (all mocked)
- Real filesystem via `tempfile` for file operation tests

**Integration Tests:**
- No dedicated integration test directory exists
- `TestFolderWatcherIntegration` class in `test_folder_watcher.py` tests component interactions but still mocks the observer

**E2E Tests:**
- Not present. `test_gui.py` at root may be a manual test file.

## Common Patterns

**Async Testing (asyncio_mode = "auto"):**
```python
async def test_scan_all_folders_initial(self, file_listener, temp_watch_folder):
    await file_listener.scan_all_folders()
    assert (temp_watch_folder / ".milo_backup.info").exists()
```

**Mixed async/sync in the same class:**
```python
class TestS3ManagerBasicOperations:
    def test_build_s3_key_with_prefix(self, s3_manager):  # sync
        ...

    async def test_calculate_md5_success(self, s3_manager):  # async
        ...
```

**Error case testing:**
```python
async def test_calculate_md5_nonexistent_file(self, file_listener):
    md5_hash = await file_listener._calculate_md5(Path("/nonexistent/file.txt"))
    assert md5_hash is None
```

**Exception testing:**
```python
with pytest.raises(FileNotFoundError):
    SimpleConfig.load_from_yaml(non_existent_path)
```

**Runtime error testing:**
```python
with pytest.raises(RuntimeError, match="Event loop not available"):
    await folder_watcher._add_folder_watch(temp_watch_folder)
```

**Verifying behavior does NOT happen:**
```python
file_listener.s3_manager.upload_file.assert_not_called()
file_change_handler.event_loop.call_soon_threadsafe.assert_not_called()
```

**Inspecting call arguments:**
```python
upload_calls = file_listener.s3_manager.upload_file.call_args_list
uploaded_files = [call[0][0].name for call in upload_calls]
assert "file1.txt" in uploaded_files
```

---

*Testing analysis: 2026-04-24*
