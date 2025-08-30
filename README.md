# AWS Copier

A cross-platform daemon for real-time folder synchronization to AWS S3 with file integrity verification.

## Features

- **Incremental Backup**: Smart `.milo_backup.info` tracking to only upload changed files
- **Real-time Monitoring**: Watches folders for changes and uploads immediately
- **Concurrent Uploads**: Up to 100 parallel uploads for maximum performance
- **File Integrity**: MD5 checksum verification for all uploads
- **Cross-platform**: Works on Windows, macOS, and Linux
- **Crash Recovery**: Persistent state tracking survives application restarts

## Quick Start

1. Install dependencies:
   ```bash
   uv sync
   ```

2. Configure your AWS credentials in `config.yaml`:
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
