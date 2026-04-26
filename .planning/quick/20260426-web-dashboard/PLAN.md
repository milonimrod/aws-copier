---
slug: web-dashboard
date: 2026-04-26
status: in-progress
---

# Quick Task: Web Dashboard + PowerShell QuickEdit Fix

## Goal
1. Add a web dashboard that streams live logs to a browser (SSE-based)
2. Fix Windows PowerShell QuickEdit mode causing the process to freeze on key/click

## Tasks
- [ ] Add `aiohttp` to pyproject.toml dependencies
- [ ] Add `web_port` option to SimpleConfig (default 8765)
- [ ] Create `aws_copier/web/__init__.py`
- [ ] Create `aws_copier/web/dashboard.py` with WebDashboard class + LogBroadcaster
- [ ] Update `main.py` to start WebDashboard and fix PowerShell QuickEdit
- [ ] Run `uv sync` to install new dependency
