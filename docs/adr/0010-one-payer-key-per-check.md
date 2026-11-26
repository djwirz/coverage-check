# 10. One payer key per check, one long timeout, and slots that outlive silence

Date: 2026-11-26
Status: Accepted. Supersedes part of ADRs 4 and 9 (the attempt id as the payer's key), ADR 5 (`now()`) and ADR 7 (`httpx`).

## Context
Building the workers (slice 3a) meant deciding what a retry sends, how long to wait for the payer, and when an in-flight slot is free again (ADR 9's consequences). ADRs 4 and 9 and the spec had each payer call carry its attempt id as its idempotency key. That makes every retry a new question to the payer: after a timeout it is billed again while the first call may still be running, which is one of the failures this project exists to stop (spec §1).

## Options considered
The payer's idempotency key:
1. **One per check and cycle** (`<check id>:<cycle>`). Retries reuse it, so the payer replays an answer it already has, unbilled, or says it is still working. A requeue starts a new cycle and asks afresh.
2. **One per attempt**, as written. Simple to reason about per call, but retries can be billed twice.

Waiting for a slow or hung payer:
1. **One timeout, longer than the payer's longest call.** A late reply is simply heard.
2. **A soft deadline** that gives the check to someone else while still listening, and a hard one. Two timers and two owners of one call.
3. **A short timeout and close.** The reply is lost, and the payer is still busy with the call.

## Decision
Option 1 for both.

- The attempt row still has its own id. Only the key sent to the payer is shared between attempts.
- Attempt timeout (35 s, bounding the whole call) < slot expiry (40 s) < lease (45 s).
- A slot is released when a reply arrives or the connection never opened. If the request may have reached the payer and nothing came back, the slot is left to expire.
- A claim takes the token, the slot, the check and the attempt row in one transaction, so a leased check never waits for capacity. Only the holder of the lease's id may finish the check.
- The bucket must satisfy `burst + rate ≤ payer limit − 2`, because that sum is what a token bucket can send in one second. Defaults: 16 per second, burst 2, against a limit of 20; 8 in flight against 10. A grant older than a tenth of a second is not sent.
- Time in the limiter is the database's `clock_timestamp()`, read after the bucket row is locked. ADR 5 said `now()`, which is the start of the transaction and stale after waiting for the lock.
- The HTTP client is `httpx2`, Pydantic's continuation of the `httpx` that ADR 7 named.

## Why
It makes "never billed twice" true across timeouts, worker deaths and failed database writes, using the idempotency the payer already offers. One long timeout needs no second code path, and the slot rule keeps the cap honest whatever happens to the caller.

## Consequences
- It relies on the payer keeping keys for longer than a check can be retried. Real payers and clearinghouses expire them.
- The slot and lease lengths assume the payer finishes within about 30 s. A slower payer needs them raised; the mock's profile and these settings must move together.
- A dead worker's checks wait up to the lease (45 s), and its slots up to 40 s.
- A hung call holds a worker task and a slot until it ends, so many hangs make the in-flight cap, not the rate, set the pace.
- A margin of 2 requests absorbs roughly a tenth of a second of network jitter. The payer's log is the judge.
