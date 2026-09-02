---
name: aws-copier-monitoring
description: This skill should be used when checking whether the deployed aws-copier daemon is healthy — is it running, has the initial/periodic scan finished, are uploads succeeding, is memory/CPU normal. Use it for "is it working", "check on the deploy", "is the scan done yet", "check the NAS", or when asked to watch/monitor an in-progress scan after a deploy.
version: 2.0.0
---

# AWS Copier Monitoring

How to check the health of the running aws-copier container on the NAS, and how to tell
normal/expected log noise apart from a real problem.

**No real-time file watcher.** aws-copier syncs only during a scan: once at startup, then
periodically every `FULL_RESCAN_INTERVAL_SECONDS` (default 6h, in `main.py`) as the sole
sync mechanism — see the `aws-copier-architecture` skill for why the real-time watcher was
removed. A file dropped into a watched folder is not picked up until the next scan runs;
that's expected, not a bug.

## Quick health check

```bash
ssh -l "Nimrod Milo" 192.168.8.201 "docker ps --filter name=aws-copier --format 'table {{.Names}}\t{{.Status}}'"
```

`Up <duration>` with no restart loop is healthy. If it's repeatedly restarting, `docker logs
aws-copier --tail 50` to see the crash.

## Is a scan (initial or periodic) still running, or has it finished?

For a large library the initial scan can take a long time (confirmed: tens of thousands of
files, multiple hours on first run over a slow link). Check whether the most recent scan has
finished:

```bash
ssh -l "Nimrod Milo" 192.168.8.201 "docker logs aws-copier 2>&1 | grep -E 'Incremental backup completed|Starting periodic full rescan|Periodic full rescan completed' | tail -5"
```

`Incremental backup completed: {...}` marks the initial scan finishing. `Starting periodic
full rescan` / `Periodic full rescan completed: {...}` mark each subsequent one — these
recur roughly every `FULL_RESCAN_INTERVAL_SECONDS`, checked once per 5-minute status-loop
pass, so the actual trigger can lag the exact interval by up to ~5 minutes.

## Scan progress while one is running

```bash
ssh -l "Nimrod Milo" 192.168.8.201 "docker logs aws-copier 2>&1 | grep -c 'Uploaded:'"
```

Gives a running total of files uploaded so far. Compare two readings a few minutes apart to
gauge throughput. Tail the log directly for a live view of which folder it's currently on:

```bash
ssh -l "Nimrod Milo" 192.168.8.201 "docker logs aws-copier --tail 10"
```

For a long wait, use a polling loop rather than repeated manual checks:

```bash
ssh -l "Nimrod Milo" 192.168.8.201 "until docker logs aws-copier 2>&1 | grep -q 'Incremental backup completed'; do sleep 10; done; echo DONE"
```

Run this via a background shell task with a generous timeout (large libraries can take a
long time even with the CONC-02 concurrent-folder-traversal optimization) — don't block on
it synchronously.

## The live dashboard

`http://<nas-ip>:8765` (if `web_enabled: true` in `config.yaml`, the default) — a
browser-based live log/stat view via SSE, started *before* the scan begins so it's available
even mid-scan. Good for a human glancing at progress; the SSH/log-grep approach above is
better for scripted checks.

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
- Multiple `Updated backup info for <same folder>: N uploaded` lines for what should have
  been a single batch of new files landing in one folder — this was a real bug (debounce
  keyed per-file, not per-folder) that's since been fixed by removing the real-time watcher
  entirely; shouldn't be possible anymore since only scheduled scans (never overlapping)
  trigger uploads now.
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
