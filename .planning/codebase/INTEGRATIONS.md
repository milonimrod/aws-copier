# External Integrations

**Analysis Date:** 2026-04-24

## APIs & External Services

**Cloud Storage:**
- AWS S3 — Primary storage destination for all backed-up files
  - SDK/Client: `aiobotocore` (async wrapper over `botocore`) — `aws_copier/core/s3_manager.py`
  - Operations used: `put_object`, `head_object`, `head_bucket`, `create_multipart_upload`, `upload_part`, `complete_multipart_upload`, `abort_multipart_upload`
  - Auth: `AWS_ACCESS_KEY_ID` / `aws_access_key_id` config key, `AWS_SECRET_ACCESS_KEY` / `aws_secret_access_key` config key
  - Region: `AWS_REGION` / `aws_region` config key (default: `us-east-1`)
  - Connection pool: `AioConfig(max_pool_connections=100)` configured in `S3Manager.__init__`
  - Client lifecycle: managed via `contextlib.AsyncExitStack` pattern in `S3Manager`

## Data Storage

**Databases:**
- None — no database used

**File Storage:**
- AWS S3 — All backed-up files go to a single configured bucket
  - Bucket: `s3_bucket` config key / `S3_BUCKET` env var
  - Optional key prefix: `s3_prefix` config key / `S3_PREFIX` env var (default: `"backup"` in example config)
  - Full S3 key format: `{s3_prefix}/{s3_folder_name}/{relative_file_path}` (built in `FileListener._build_s3_key`)
  - Multipart upload threshold: 100 MB — files larger than this use chunked multipart (5 MB parts)

**Local State / Tracking:**
- `.milo_backup.info` — JSON file written per-folder alongside watched files; tracks MD5 hashes of previously backed-up files to enable incremental backup
  - Format: `{"timestamp": "<ISO8601>", "files": {"<filename>": "<md5_hex>"}}`
  - Written by `FileListener._update_backup_info` in `aws_copier/core/file_listener.py`

**Caching:**
- None — MD5 tracking in `.milo_backup.info` serves as the change-detection cache

## Authentication & Identity

**Auth Provider:**
- AWS IAM — static credentials (access key ID + secret access key)
  - Credentials provided via YAML config file or environment variables
  - No IAM role / instance profile support detected
  - No MFA or STS assume-role support detected
  - Credentials stored in plaintext in `config.yaml` (gitignored `.env` is an alternative)

## File System Monitoring

**watchdog (OS-level events):**
- `watchdog.observers.Observer` — starts OS-native file system watcher threads
- `watchdog.events.FileSystemEventHandler` — base class for `FileChangeHandler` in `aws_copier/core/folder_watcher.py`
- Events handled: `created`, `modified` (directory events and deletions are ignored)
- Bridge pattern: watchdog callbacks call `asyncio.get_running_loop().call_soon_threadsafe(...)` to schedule async upload tasks

## Monitoring & Observability

**Error Tracking:**
- None (no Sentry, Datadog, etc.)

**Logs:**
- Python stdlib `logging` module throughout all modules
- Log level: `INFO` set in `main.py` via `logging.basicConfig`
- GUI mode adds a `LogHandler` queue that routes log records into the tkinter scroll widget (`aws_copier/ui/simple_gui.py`)
- Format: `%(asctime)s - %(name)s - %(levelname)s - %(message)s`

## CI/CD & Deployment

**Hosting:**
- None detected — runs as a local daemon process

**CI Pipeline:**
- None detected — no GitHub Actions, CircleCI, or similar config files present

**Pre-commit:**
- `.pre-commit-config.yaml` — hooks for ruff lint, ruff-format, uv-lock sync, AST check, YAML/TOML validation

## Environment Configuration

**Required config keys (YAML or env vars):**
- `aws_access_key_id` / `AWS_ACCESS_KEY_ID` — AWS credential
- `aws_secret_access_key` / `AWS_SECRET_ACCESS_KEY` — AWS credential
- `aws_region` / `AWS_REGION` — AWS region (default: `us-east-1`)
- `s3_bucket` / `S3_BUCKET` — Target S3 bucket name
- `s3_prefix` / `S3_PREFIX` — Optional key prefix in bucket
- `watch_folders` — Dict mapping local folder paths to S3 folder names (or legacy list of paths)

**Optional config keys:**
- `max_concurrent_uploads` — Semaphore limit (default: 100)
- `discovered_files_folder` — Local path for discovered file output (default: `~/.aws-copier/discovered`)

**Config file location:**
- Dev/project: `config.yaml` in project root (read by `main.py`)
- Default user-level: `~/aws-copier-config.yaml` (read by `load_config()` in `aws_copier/models/simple_config.py`)

**Secrets location:**
- Plaintext in `config.yaml` (gitignored) or environment variables; no secrets manager integration

## Webhooks & Callbacks

**Incoming:**
- None

**Outgoing:**
- None

---

*Integration audit: 2026-04-24*
