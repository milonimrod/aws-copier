# AWS Copier

A cross-platform daemon for real-time folder synchronization to AWS S3 with file integrity verification.

## Features

- **Incremental Backup**: Smart `.milo_backup.info` tracking to only upload changed files
- **Real-time Monitoring**: Watches folders for changes and uploads immediately
- **Concurrent Uploads**: Up to 100 parallel uploads for maximum performance
- **File Integrity**: MD5 checksum verification for all uploads
- **Cross-platform**: Works on Windows, macOS, and Linux
- **Crash Recovery**: Persistent state tracking survives application restarts

## Running on Docker (e.g. a UGreen NAS)

The daemon is designed to run headless in a container, which is the recommended way to run
it on a NAS.

1. Copy `config.yaml.example` to `config.yaml` (gitignored — it will hold real credentials)
   and fill in your AWS credentials, S3 bucket, and `watch_folders`. The keys under
   `watch_folders` are **container** paths (e.g. `/data/documents`), not host paths.
2. Edit `docker-compose.yml`: replace the placeholder host paths on the left of each `:` in
   `volumes` with the real paths of the NAS shares you want backed up, matching the
   container paths (right of the `:`) you used in `config.yaml`. Set `PUID`/`PGID` to match
   the owner of those shares (`ls -n /volumeX/your-share` on the NAS shows the numeric IDs).
3. Build and start:
   ```bash
   docker compose up -d --build
   ```
4. Watch logs with `docker compose logs -f`, or open `http://<nas-ip>:8765` for the live
   dashboard (disable by removing the `ports` mapping or setting `web_enabled: false`).

Restart policy is `unless-stopped`, so the daemon comes back up after a NAS reboot.

### Building elsewhere and copying the image to the NAS

If you'd rather not build on the NAS itself (slower CPU, no build tools, etc.), build on
another machine and transfer the image instead of using `--build` in step 3 above:

1. Check the NAS's CPU architecture once, e.g. `ssh <user>@<nas-ip> uname -m` — `x86_64`
   means `amd64`, `aarch64` means `arm64`.
2. On the build machine:
   ```bash
   docker/build-and-export.sh amd64   # or arm64 — matches the NAS's architecture
   ```
   This produces `aws-copier-<arch>.tar.gz`. Cross-building `arm64` from an `amd64` machine
   (or vice versa) needs QEMU emulation registered with buildx first (one-time):
   `docker run --privileged --rm tonistiige/binfmt --install all`.
3. Copy the tarball to a location the NAS can reach (a shared folder, `scp`, etc.), then on
   the NAS:
   ```bash
   docker load -i aws-copier-<arch>.tar.gz
   docker compose up -d   # no --build — uses the image you just loaded
   ```

## Quick Start (local / dev, without Docker)

1. Install dependencies:
   ```bash
   uv sync
   ```

2. Copy `config.yaml.example` to `config.yaml` and configure your AWS credentials:
   ```yaml
   aws_access_key_id: "your-access-key-id"
   aws_secret_access_key: "your-secret-access-key"
   s3_bucket: "your-bucket-name"
   watch_folders:
     - "/path/to/folder1"
     - "/path/to/folder2"
   ```

3. Run the application:
   ```bash
   # Console version
   uv run python main.py

   # GUI version (cross-platform)
   uv run python main_gui.py

   # Test GUI with simulated logs
   uv run python test_gui.py
   ```

## GUI Features

The AWS Copier includes a simple, cross-platform GUI built with Tkinter:

### ✅ **Supported Platforms:**
- **Windows** (including WSL/Ubuntu)
- **macOS**
- **Linux**

### 🎛️ **GUI Components:**
- **📋 Real-time log display** - Shows all application logs with auto-scroll
- **🔴 Shutdown button** - Gracefully stops the application
- **📦 Minimize button** - Minimizes the window
- **🧹 Clear logs** - Clears the log display
- **📊 Status indicator** - Shows current application status

The GUI automatically captures all application logs and displays them in real-time, making it easy to monitor the backup process.

## Testing

Run the comprehensive test suite:
```bash
uv run pytest tests/unit/ -v
```

## Architecture

- **FileListener**: Performs incremental backup scans using `.milo_backup.info` files
- **FolderWatcher**: Real-time file system monitoring
- **S3Manager**: Async S3 operations with connection pooling
- **SimpleConfig**: YAML-based configuration management
