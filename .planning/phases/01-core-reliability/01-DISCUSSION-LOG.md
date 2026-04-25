# Phase 1: Core Reliability - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-04-24
**Phase:** 01-core-reliability
**Areas discussed:** Testing strategy, Shutdown drain behavior, IgnoreRules module interface

---

## Testing Strategy

| Option | Description | Selected |
|--------|-------------|----------|
| Tests alongside fixes | Each ASYNC/IGNORE/CONFIG fix gets a corresponding test using pytest-asyncio + moto[s3]. Already have the infrastructure — tests stay co-located with the work. | ✓ |
| Skip tests for now | Focus on correctness fixes only. Tests can be added as a follow-up phase or left to the existing minimal coverage floor (30% enforced). | |
| You decide | Claude picks the testing approach based on what makes sense per fix. | |

**User's choice:** Tests alongside fixes

**Follow-up — depth of tests:**

| Option | Description | Selected |
|--------|-------------|----------|
| Behavior | Tests prove the bug is fixed: e.g., a real-time event actually triggers upload without a scan cycle, 10 simultaneous files upload concurrently, SIGTERM drains uploads before exit. | ✓ |
| Coverage only | Tests exercise the code paths at minimum depth to hit coverage targets — no deep behavior verification. | |
| You decide | Claude determines test depth per requirement. | |

**User's choice:** Behavior-proving tests

---

## Shutdown Drain Behavior

| Option | Description | Selected |
|--------|-------------|----------|
| Cap at 60 seconds | Drain in-flight uploads for up to 60s, then exit even if some uploads are still running. Prevents the daemon from hanging indefinitely on a stuck transfer. | ✓ |
| Wait indefinitely | Wait until all in-flight uploads complete, no matter how long. Guarantees no upload is cut mid-transfer — but risks a hung process on network issues. | |
| Cap at 300 seconds (5 min) | Match the existing scan cycle interval. A single upload shouldn't take longer than one scan cycle. | |

**User's choice:** Cap at 60 seconds

**Follow-up — logging abandoned uploads:**

| Option | Description | Selected |
|--------|-------------|----------|
| Yes — log each abandoned upload | logger.warning() for each file whose upload was cut short. Makes it easy to identify what needs to be re-synced on next start. | ✓ |
| No — silent exit | Just exit cleanly. The next scan cycle will re-upload anything that didn't finish. | |

**User's choice:** Log each abandoned upload as warning

---

## IgnoreRules Module Interface

| Option | Description | Selected |
|--------|-------------|----------|
| Instance methods on the dataclass | IgnoreRules.should_ignore_file(path) and .should_ignore_dir(path) live on the class. FileListener and FileChangeHandler both call the same methods — logic is centralized, callers don't re-implement the check. | ✓ |
| Pure data only | IgnoreRules is just a frozen dataclass with sets/tuples of patterns. FileListener and FileChangeHandler import it and run their own fnmatch checks against those constants. | |
| You decide | Claude picks the interface that fits best. | |

**User's choice:** Instance methods on the dataclass

**Follow-up — singleton vs per-component:**

| Option | Description | Selected |
|--------|-------------|----------|
| Module-level singleton | IGNORE_RULES = IgnoreRules() at module level in ignore_rules.py. FileListener and FileChangeHandler import and use the same instance — no duplication, consistent behavior guaranteed. | ✓ |
| Instantiated per-component | Each component creates its own IgnoreRules(). Allows future customization per-component but adds boilerplate for no current benefit. | |

**User's choice:** Module-level singleton

---

## Claude's Discretion

- Exact fnmatch pattern list for glob-based ignore rules (IGNORE-01)
- Exact sensitive-file deny list for dot-file blocking (IGNORE-02)
- Internal structure of per-folder asyncio.Lock registry (ASYNC-03)
- Whether asyncio.wait_for wraps individual coroutines or gathered tasks (ASYNC-02)
- Shutdown drain countdown logging format

## Deferred Ideas

None.
