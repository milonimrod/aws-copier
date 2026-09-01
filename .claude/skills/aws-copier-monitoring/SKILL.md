---
name: aws-copier-monitoring
description: This skill should be used when checking whether the deployed aws-copier daemon is healthy — is it running, has the initial scan finished, is the real-time watcher active, are uploads succeeding, is memory/CPU normal. Use it for "is it working", "check on the deploy", "is the scan done yet", "check the NAS", or when asked to watch/monitor an in-progress scan after a deploy.
version: 1.0.0
---

# AWS Copier Monitoring

How to check the health of the running aws-copier container on the NAS, and how to tell
normal/expected log noise apart from a real problem.

## Quick health check

```bash
ssh -l "Nimrod Milo" 192.168.8.201 "docker ps --filter name=aws-copier --format 'table {{.Names}}\t{{.Status}}'"
```

`Up <duration>` with no restart loop is healthy. If it's repeatedly restarting, `docker logs
aws-copier --tail 50` to see the crash.

## Is the initial scan still running, or has it finished?

`main.py`'s startup sequence is: connect to S3 → run the full `scan_all_folders()` once →
**only then** start the real-time folder watcher. For a large library this scan can take a
long time (confirmed: tens of thousands of files, multiple hours on first run) — and until
it finishes, **new files dropped into watched folders are not picked up at all**, since the
watcher isn't running yet. This is expected behavior, not a bug, but it's the single most
common "why isn't it picking up my new photos" question.

Check whether it's finished:

```bash
ssh -l "Nimrod Milo" 192.168.8.201 "docker logs aws-copier 2>&1 | grep -E 'Folder Watcher started|Incremental backup completed'"
```

Empty output = still scanning. Once you see `Folder Watcher started`, real-time tracking is
live.

## Scan progress while it's still running

```bash
ssh -l "Nimrod Milo" 192.168.8.201 "docker logs aws-copier 2>&1 | grep -c 'Uploaded:'"
```

Gives a running total of files uploaded so far this scan. Compare two readings a few minutes
apart to gauge throughput. Tail the log directly for a live view of which folder it's
currently on:

```bash
ssh -l "Nimrod Milo" 192.168.8.201 "docker logs aws-copier --tail 10"
```

For a long wait, use a polling loop rather than repeated manual checks:

```bash
ssh -l "Nimrod Milo" 192.168.8.201 "until docker logs aws-copier 2>&1 | grep -q 'Folder Watcher started'; do sleep 10; done; echo DONE"
```

Run this via a background shell task with a generous timeout (large libraries can take a
long time even after the CONC-02 concurrent-folder-traversal optimization) — don't block
on it synchronously.

## The live dashboard

`http://<nas-ip>:8765` (if `web_enabled: true` in `config.yaml`, the default) — a
browser-based live log/stat view via SSE, started *before* the scan begins so it's available
even mid-scan. Good for a human glancing at progress; the SSH/log-grep approach above is
better for scripted checks.

## Testing that real-time watching actually works

Docker bind mounts on native Linux (the NAS's own Docker, not Docker Desktop) propagate
`inotify` events correctly — this has been verified empirically, not just assumed. To
re-confirm after any relevant change:

1. Start tailing logs in the background: `ssh -l "Nimrod Milo" 192.168.8.201 "docker logs -f aws-copier"`.
2. Create a **clearly-named test file** in a test subfolder under the watched path (e.g.
   `/volume1/pictures/ClaudeWatchTest/test.txt` over SSH) — never touch real files for this.
3. Within ~2-5 seconds (the debounce window plus processing time) you should see `📁 File
   created:` then an upload log line for it.
4. **Clean up the test file/folder afterward** — `rm -rf` it from the NAS and, if it made it
   to S3, delete the corresponding test object too.

This only works once the watcher has actually started (see above) — testing during the
initial scan will show nothing happening, which is expected, not a failure.

## Known-benign log lines (do not treat as errors)

- `Could not verify multipart lifecycle rule: bucket has existing lifecycle rules but none
  is AbortIncompleteMultipartUpload` — expected; the app deliberately never overwrites an
  existing lifecycle config it didn't create (D-12). Cosmetic.
- SSH's `WARNING: connection is not using a post-quantum key exchange algorithm` banner —
  harmless, unrelated to aws-copier.

## Signs of an actual problem

- A burst of `Upload timeout for <file> (300s, ...)` lines **all with the same timestamp** —
  this was a real bug (semaphore queue-wait counted against the timeout) that's since been
  fixed; if it recurs, check whether `_upload_with_timeout` in `file_listener.py` still
  acquires `upload_semaphore` *before* starting the timed region, not inside it.
- `Task exception was never retrieved` for `ClientRequest.write_bytes` — was a symptom of
  the same mass-timeout bug (many cancellations at once); shouldn't appear in isolation.
- Memory (`docker stats aws-copier`) climbing and never coming back down across many hours —
  the periodic housekeeping (`FileListener.clear_caches()` + `_trim_memory()`) runs every
  5 minutes in the main status loop; if RSS keeps climbing well past what a scan alone would
  need, check that loop is actually executing (look for repeated `📊 Backup Status:` lines
  in the logs at ~5-minute intervals — their presence confirms the housekeeping call
  alongside them is running too).
- Any `ERROR` line with a Python traceback that isn't one of the two benign cases above.

## Spot-checking S3 state directly

To confirm a specific local file actually made it to S3 correctly (not just "no error
logged"), verify size and MD5 metadata match, using the real credentials already in
`config.yaml`:

```python
from aws_copier.models.simple_config import SimpleConfig
from aws_copier.core.s3_manager import S3Manager
# load config.yaml, s3.initialize(), s3.get_object_info(key), compare against local MD5/size
```

See the `aws-copier-architecture` skill for the module map if deeper investigation is
needed, or spawn the `aws-copier-explorer` agent for a read-only trace through the code.
