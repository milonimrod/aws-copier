# Codebase Structure

**Analysis Date:** 2026-04-24

## Directory Layout

```
aws-copier/
├── aws_copier/             # Main Python package
│   ├── __init__.py         # Package metadata (version 0.1.0)
│   ├── core/               # Business logic — scanning, watching, S3
│   │   ├── __init__.py
│   │   ├── file_listener.py    # Incremental backup scanner
│   │   ├── folder_watcher.py   # Real-time watchdog integration
│   │   └── s3_manager.py       # Async S3 client wrapper
│   ├── models/             # Data models / configuration
│   │   ├── __init__.py
│   │   └── simple_config.py    # SimpleConfig class + load_config()
│   └── ui/                 # Optional tkinter GUI
│       ├── __init__.py
│       └── simple_gui.py       # AWSCopierGUI, LogHandler, create_gui()
├── tests/
│   ├── __init__.py
│   └── unit/               # Unit tests (all tests currently here)
│       ├── __init__.py
│       ├── test_file_listener.py
│       ├── test_folder_watcher.py
│       ├── test_s3_manager.py
│       ├── test_s3_manager_comprehensive.py
│       └── test_simple_config.py
├── .planning/
│   └── codebase/           # GSD codebase analysis documents
├── main.py                 # Headless CLI entry point
├── main_gui.py             # GUI entry point
├── test_gui.py             # GUI smoke test script
├── config.yaml             # Runtime configuration (gitignored in practice)
├── pyproject.toml          # Project metadata, dependencies, tool config
├── pytest.ini              # Pytest configuration (asyncio_mode = auto)
├── setup_windows.py        # Windows-specific setup helper
├── .python-version         # Python version pin (read by pyenv/uv)
├── .pre-commit-config.yaml # Pre-commit hooks (ruff, black)
└── uv.lock                 # Lockfile for uv package manager
```

## Directory Purposes

**`aws_copier/core/`:**
- Purpose: Core backup engine; all business logic lives here
- Contains: Async classes for file scanning, event watching, and S3 operations
- Key files: `file_listener.py` (18.7K), `folder_watcher.py` (8.4K), `s3_manager.py` (15K)

**`aws_copier/models/`:**
- Purpose: Configuration model; no domain models beyond config
- Contains: `SimpleConfig` — the single config object passed to all components
- Key files: `simple_config.py` (5K)

**`aws_copier/ui/`:**
- Purpose: Optional tkinter-based GUI layer; not imported by headless mode
- Contains: `AWSCopierGUI` and `LogHandler`
- Key files: `simple_gui.py` (9.3K)

**`tests/unit/`:**
- Purpose: All automated tests; no integration or E2E subdirectory exists yet
- Contains: One test file per source module
- Key files: `test_file_listener.py` (22.2K), `test_folder_watcher.py` (17.8K), `test_s3_manager_comprehensive.py` (12.4K)

## Key File Locations

**Entry Points:**
- `main.py`: Headless asyncio application runner (`AWSCopierApp`)
- `main_gui.py`: GUI application runner (`AWSCopierGUIApp`)
- `test_gui.py`: Manual GUI smoke test

**Configuration:**
- `config.yaml`: Runtime YAML config (AWS credentials, watch folders, S3 settings)
- `pyproject.toml`: Build system, dependency declarations, ruff/black/mypy/pytest config
- `pytest.ini`: Pytest settings (asyncio auto mode, coverage thresholds)
- `.pre-commit-config.yaml`: Pre-commit hooks

**Core Logic:**
- `aws_copier/core/file_listener.py`: `FileListener` — incremental backup, MD5 comparison, upload orchestration
- `aws_copier/core/folder_watcher.py`: `FolderWatcher` + `FileChangeHandler` — real-time OS event handling
- `aws_copier/core/s3_manager.py`: `S3Manager` — all AWS S3 API calls (upload, check, multipart)
- `aws_copier/models/simple_config.py`: `SimpleConfig` + `load_config()` — YAML config loading

**Testing:**
- `tests/unit/test_file_listener.py`: FileListener unit tests
- `tests/unit/test_folder_watcher.py`: FolderWatcher unit tests
- `tests/unit/test_s3_manager.py`: S3Manager basic tests
- `tests/unit/test_s3_manager_comprehensive.py`: S3Manager extended tests
- `tests/unit/test_simple_config.py`: SimpleConfig unit tests

## Naming Conventions

**Files:**
- Snake case: `file_listener.py`, `simple_config.py`, `folder_watcher.py`
- Test files prefixed with `test_`: `test_file_listener.py`
- Entry point files at root use descriptive names: `main.py`, `main_gui.py`

**Directories:**
- Snake case, short and descriptive: `core/`, `models/`, `ui/`

**Classes:**
- PascalCase: `FileListener`, `FolderWatcher`, `S3Manager`, `SimpleConfig`, `AWSCopierGUI`
- Test classes not used — test files use bare functions

**Functions/Methods:**
- Snake case: `scan_all_folders()`, `upload_file()`, `load_config()`
- Private methods prefixed with `_`: `_process_current_folder()`, `_build_s3_key()`, `_calculate_md5()`
- Public async methods have no special prefix

## Where to Add New Code

**New core feature (e.g., download, restore):**
- Implementation: `aws_copier/core/<feature_name>.py`
- Tests: `tests/unit/test_<feature_name>.py`
- Wire into app: `main.py` and/or `main_gui.py`

**New configuration option:**
- Add field to `aws_copier/models/simple_config.py` in `__init__` with a default
- Add serialization to `save_to_yaml()` and `to_dict()`
- Update `tests/unit/test_simple_config.py`

**New UI component:**
- Add to `aws_copier/ui/simple_gui.py` or create `aws_copier/ui/<component>.py`
- Wire into `main_gui.py`

**New utility or shared helper:**
- If widely used: create `aws_copier/utils/` directory with `__init__.py`
- If specific to one module: add as private method in that module

**New test:**
- Place in `tests/unit/test_<module_name>.py`
- Use `pytest-asyncio` with `asyncio_mode = auto` (no `@pytest.mark.asyncio` decorator needed)
- Mock S3 using `moto[s3]` library

## Special Directories

**`.planning/codebase/`:**
- Purpose: GSD codebase analysis documents
- Generated: By GSD mapping agents
- Committed: Yes (planning artifacts)

**`.venv/`:**
- Purpose: Virtual environment managed by `uv`
- Generated: Yes
- Committed: No

**`.ruff_cache/`:**
- Purpose: Ruff linter cache
- Generated: Yes
- Committed: No

**`~/.aws-copier/discovered/`** (outside repo):
- Purpose: File discovery output folder (configured via `discovered_files_folder` in config)
- Generated: Yes (created by app at runtime)
- Committed: No

**`.milo_backup.info`** (written into watched directories at runtime):
- Purpose: Per-directory backup state tracking; JSON with `{timestamp, files: {filename: md5}}`
- Generated: Yes (by `FileListener` during backup)
- Committed: No (lives in user's watched folders, not the repo)

---

*Structure analysis: 2026-04-24*
