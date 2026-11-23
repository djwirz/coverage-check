# 4. Postgres as the job queue, with leased claims

Date: 2026-11-23
Status: Accepted

## Context
Checks need a queue that:
- survives worker crashes,
- never hands one check to two workers at once,
- lets a check retry later without a worker sleeping on it,
- records every payer attempt,
- commits a check's result and its final state together.

The results and the audit trail already live in Postgres.

## Options considered
1. **Postgres as the queue.** Claims with `SELECT ... FOR UPDATE SKIP LOCKED`, a lease expiry on each claimed row, a `not_before` time for retries, and one `attempts` row per payer call written before the call. The result and the state change happen in one transaction.
2. **Redis with a job library (RQ, arq, Celery).** Fast and familiar, but it's another stateful service. The queue and the results would then live in two stores, so "done" and "result saved" can disagree after a crash.
3. **A Kubernetes Job per batch or per check.** It uses the scheduler as the queue. But a pod per check is far too heavy, and the scheduler knows nothing about rate limits or idempotency.
4. **A managed queue (SQS and the like).** Durable, with visibility timeouts that work like leases. But it's a cloud dependency for a project that must run locally from one command.

## Decision
Option 1. Postgres holds batches, checks, attempts and the limiter state. Workers claim a few checks at a time with `SKIP LOCKED` and a lease. Expired leases go back to `queued`. A retry sets `not_before`. A result commits in the same transaction as the check's final state.

## Why
One store makes the exactly-once and nothing-lost invariants a matter of transactions, not of keeping two systems in step. `SKIP LOCKED` is the well-known pattern for many workers on one queue. At this project's volume (thousands of checks, a payer limit of tens per second at most), Postgres has room to spare. It's also one less service in the cluster.

## Consequences
- Lease length is a real trade-off. Too short, and a slow call gets claimed twice. Too long, and a dead worker's checks wait. It's set well above the payer timeout, and lease expiries are a metric.
- A claimed-twice check is still safe: the attempt id is the payer's idempotency key, and the newest result wins (spec §6, invariant 4).
- Polling for work adds a little load and latency. `LISTEN/NOTIFY` can cut it later if it matters.
- Revisit if volume grows by orders of magnitude or if several services need to consume the queue.
