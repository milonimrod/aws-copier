# AWS Copier

A cross-platform daemon for periodic incremental folder synchronization to AWS S3 with file integrity verification.

## Features

- **Incremental Backup**: Smart `.milo_backup.info` tracking to only upload changed files
- **Periodic Full Rescan**: Re-scans every watch folder on a fixed interval (default 6h) —
  the sole sync mechanism (no real-time file watcher — see Architecture below for why)
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
another machine and transfer the image instead of using `--build` above.

**One-command deploy** (recommended, once set up): `docker/deploy.sh` builds, exports, and
remotely loads + restarts the container on the NAS in a single call. Requires:
- The NAS's docker share mounted locally over NFS/SMB (see `NAS_LOCAL_DIR` below) — the
  built image is dropped straight onto it, no separate copy step.
- Passwordless SSH to the NAS (`ssh-copy-id <user>@<nas-ip>` once).
- Your NAS SSH user in the `docker` group (`sudo usermod -aG docker <user>` on the NAS,
  then open a *new* SSH session — group membership only applies to fresh logins), so it can
  talk to the Docker socket without needing a password each deploy.

```bash
docker/deploy.sh amd64   # or arm64 — must match the NAS's architecture
```

Defaults assume this machine's actual setup; override via env vars if yours differs:
`NAS_HOST` (default `192.168.8.201`), `NAS_SSH_USER` (default `Nimrod Milo`),
`NAS_LOCAL_DIR` (local NFS-mounted path to the NAS's aws-copier folder, default
`/mnt/nas-docker/aws-copier`), `NAS_REMOTE_DIR` (that same folder's path as seen on the NAS
itself, default `/volume1/docker/aws-copier`).

**Manual steps**, if you'd rather not set up SSH access:

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
- **S3Manager**: Async S3 operations with connection pooling
- **SimpleConfig**: YAML-based configuration management

No real-time file watcher — periodic full scans (`FULL_RESCAN_INTERVAL_SECONDS` in
`main.py`, default 6h) are the sole sync mechanism. An earlier version used a
`watchdog`-based real-time watcher; it was removed after two real production bugs turned up
in that subsystem specifically (duplicate uploads from per-file debounce keying, and files
inside ignored directories slipping through undetected), plus a standing risk that heavy
background filesystem churn (e.g. a NAS-bundled cloud-sync tool) could overflow the OS's
`inotify` event queue and silently drop events. For a personal backup tool, bounded
scan-interval latency was judged a better tradeoff than an entire watcher subsystem's worth
of edge cases.
