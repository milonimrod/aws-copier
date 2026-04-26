---
phase: 02-performance-polish
plan: "03"
subsystem: s3-manager
tags: [s3, aws, performance, lifecycle, credentials, PERF-03, CONFIG-05, CONFIG-07]

# Dependency graph
requires:
  - 02-01 (SimpleConfig.use_credential_chain attribute)
provides:
  - S3Manager.upload_file(precomputed_md5=...) kwarg (consumed by FileListener in Plan 02)
  - S3Manager.ensure_lifecycle_rule() method (called from main.py in Plan 05)
  - Credential-chain-aware client construction in _get_or_create_client and initialize
affects:
  - aws_copier/core/s3_manager.py
  - tests/unit/test_s3_manager_perf.py
  - 02-05 (main.py wires ensure_lifecycle_rule call)

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "PERF-03 MD5 dedup: precomputed_md5 or await self._calculate_md5(local_path) — caller wins"
    - "CONFIG-05 credential chain: client_kwargs dict built conditionally on use_credential_chain"
    - "CONFIG-07 lifecycle rule: D-12 put-only-when-no-rules-at-all; D-11 never-raise"
    - "TDD RED/GREEN across both tasks in one test file (12 tests)"

key-files:
  created:
    - tests/unit/test_s3_manager_perf.py
  modified:
    - aws_copier/core/s3_manager.py

key-decisions:
  - "D-05 applied: precomputed_md5 or await _calculate_md5 — falsy precomputed_md5 triggers recompute (backward compatible)"
  - "D-11 applied: ensure_lifecycle_rule catches all exceptions and logs warning rather than raising — daemon startup is never blocked"
  - "D-12 applied: put_bucket_lifecycle_configuration is only called when get_bucket_lifecycle_configuration raised NoSuchLifecycleConfiguration AND no rules were seen — existing rules (any kind) cause warn-and-skip"
  - "CONFIG-05 applied: client_kwargs dict built first then aws_access_key_id/aws_secret_access_key added only when not use_credential_chain; same pattern in both initialize() and _get_or_create_client()"
  - "Any added to typing imports to support Dict[str, Any] for client_kwargs"
  - "TDD RED commit 24cd005 captured all 12 tests; GREEN commit b3c666d implements all three features at once"

# Metrics
duration: "5min"
completed: "2026-04-26"
---

# Phase 02 Plan 03: S3Manager PERF-03 + CONFIG-05 + CONFIG-07 Summary

**precomputed_md5 kwarg on upload_file eliminates double MD5 computation; credential-chain-aware client construction honours use_credential_chain; ensure_lifecycle_rule prevents orphaned multipart parts accumulating S3 cost**

## Performance

- **Duration:** ~5 min
- **Started:** 2026-04-26
- **Completed:** 2026-04-26
- **Tasks:** 2 (TDD RED + GREEN across both tasks in one cycle)
- **Files modified:** 2

## Accomplishments

### upload_file signature delta (PERF-03)

Before:
```python
async def upload_file(self, local_path: Path, s3_key: str) -> bool:
    md5_hash = await self._calculate_md5(local_path)
```

After:
```python
async def upload_file(
    self,
    local_path: Path,
    s3_key: str,
    precomputed_md5: Optional[str] = None,  # PERF-03 / D-05
) -> bool:
    md5_hash = precomputed_md5 or await self._calculate_md5(local_path)
```

Plan 02's `_upload_single_file` can now pass `precomputed_md5=local_md5` — eliminating the duplicate MD5 computation between FileListener and S3Manager.

### Credential-chain-aware client construction (CONFIG-05)

Both `initialize()` and `_get_or_create_client()` now build `client_kwargs` conditionally:

```python
client_kwargs: Dict[str, Any] = {
    "region_name": self.config.aws_region,
    "config": self._client_config,
}
if not self.config.use_credential_chain:
    client_kwargs["aws_access_key_id"] = self.config.aws_access_key_id
    client_kwargs["aws_secret_access_key"] = self.config.aws_secret_access_key
# else: aiobotocore traverses env vars → ~/.aws/credentials → IAM instance profile
```

### ensure_lifecycle_rule method body (CONFIG-07)

Branch map:

| get_bucket_lifecycle_configuration result | put called? | Log |
|---|---|---|
| raises NoSuchLifecycleConfiguration AND no rules seen | Yes — creates rule with DaysAfterInitiation=1 | info: "S3 lifecycle rule set" |
| returns rules with AbortIncompleteMultipartUpload | No (D-12) | info: "already present (DaysAfterInitiation=N). Skipping." |
| returns rules WITHOUT AbortIncompleteMultipartUpload | No (D-12) | warning: "Could not verify..." |
| raises ClientError (not NoSuchLifecycleConfiguration) | No (D-11) | warning: "Could not verify..." |
| raises any other Exception | No (D-11) | warning: "Could not verify..." |
| put_bucket_lifecycle_configuration raises | — (D-11) | warning: "Could not verify..." |

**D-11 requirement:** `ensure_lifecycle_rule` never raises — all exceptions are caught and logged as warnings. Daemon startup is never blocked.

**D-12 requirement:** `put_bucket_lifecycle_configuration` is only called when the get call raised `NoSuchLifecycleConfiguration` (no lifecycle config at all). If the get call returned ANY rules — even unrelated ones — the method warns and returns without calling put.

### Plan wiring confirmations

- **Plan 05 (main.py):** Can wire `await self.s3_manager.ensure_lifecycle_rule()` in `AWSCopierApp.start()` after `initialize()` with no further S3Manager changes needed.
- **Plan 02 (FileListener):** `_upload_single_file` can pass `precomputed_md5=local_md5` to `s3_manager.upload_file(...)` — the kwarg exists and the backward-compatible default (`None`) means callers that omit it continue to work.

## Task Commits

| Task | Gate | Commit | Description |
|------|------|--------|-------------|
| 1+2 combined | RED | 24cd005 | test(02-03): failing tests for PERF-03, CONFIG-05, CONFIG-07 |
| 1+2 combined | GREEN | b3c666d | feat(02-03): implement all three features |

## New Tests (12 total in test_s3_manager_perf.py)

### TestPrecomputedMd5 (3 tests — PERF-03)

| Test | What it proves |
|------|----------------|
| `test_upload_uses_precomputed_md5` | When precomputed_md5 provided, `_calculate_md5` is NOT called |
| `test_upload_recomputes_when_omitted` | When precomputed_md5 omitted, `_calculate_md5` IS called once |
| `test_upload_passes_precomputed_md5_into_metadata` | S3 Metadata["md5-checksum"] contains the precomputed value |

### TestCredentialChainClientWiring (3 tests — CONFIG-05)

| Test | What it proves |
|------|----------------|
| `test_get_or_create_client_omits_creds_when_chain_active` | chain_config → no explicit creds in create_client kwargs |
| `test_get_or_create_client_passes_creds_when_chain_inactive` | explicit_creds_config → creds present in kwargs |
| `test_initialize_uses_chain_aware_kwargs` | initialize() also omits creds when use_credential_chain=True |

### TestEnsureLifecycleRule (6 tests — CONFIG-07)

| Test | What it proves |
|------|----------------|
| `test_creates_rule_when_no_lifecycle_config` | NoSuchLifecycleConfiguration → put called with correct rule |
| `test_skips_when_abort_rule_already_present` | Existing AbortIncomplete rule → put NOT called, info logged |
| `test_warns_and_skips_when_other_rules_but_no_abort` | Other rules exist → put NOT called, warning logged |
| `test_warns_and_returns_on_other_client_error` | AccessDenied → no raise, warning logged |
| `test_warns_when_put_fails` | put raises → no raise, warning logged |
| `test_does_not_raise_on_unexpected_exception` | Generic Exception from get → no raise (D-11) |

**Total: 12 new tests pass. Full suite: 180 tests pass (no regressions).**

## Deviations from Plan

None — plan executed exactly as written. Both tasks were implemented in a single RED/GREEN TDD cycle since the test file covered all classes simultaneously and the implementation was coherent as a single diff.

## TDD Gate Compliance

- RED gate: `24cd005` — `test(02-03)` commit with 12 failing tests
- GREEN gate: `b3c666d` — `feat(02-03)` commit; all 12 new tests pass, all 168 existing tests still pass (180 total)

## Known Stubs

None.

## Threat Flags

None — no new network endpoints, auth paths, file access patterns, or schema changes beyond what was planned and covered by the threat model (T-02-11 through T-02-17).

## Self-Check

- [x] `aws_copier/core/s3_manager.py` exists and contains `precomputed_md5`, `use_credential_chain`, `ensure_lifecycle_rule`
- [x] `tests/unit/test_s3_manager_perf.py` exists with 12 tests
- [x] Commit 24cd005 exists (RED gate)
- [x] Commit b3c666d exists (GREEN gate)
- [x] `uv run pytest tests/unit/test_s3_manager_perf.py -x` exits 0 (12 tests)
- [x] `uv run pytest tests/unit/test_s3_manager.py tests/unit/test_s3_manager_comprehensive.py -x` exits 0 (31 tests)
- [x] `uv run pytest -x` exits 0 (180 tests)

## Self-Check: PASSED
