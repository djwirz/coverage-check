# 9. The payer protocol, and what the mock payer records

Date: 2026-11-26
Status: Accepted. Superseded in part by 10 (the payer's idempotency key).

## Context
ADR 0006 chose our own mock payer. Slice 2 builds it, so two things must be fixed that the workers (slice 3) and the invariant report will depend on: the protocol (spec §13 Q5), and the payer's record of what it received, which is the evidence for invariant 1.

## Options considered
Protocol:
1. **Simplified JSON**, one request and one reply, shaped by public eligibility concepts.
2. **Close to X12 270/271.** More like a clearinghouse. A parsing project of its own, which proves none of the invariants.

The payer's records (request log, and answers by idempotency key):
1. **A SQLite file belonging to the mock.** In the standard library, survives a restart, separate from the engine.
2. **In memory.** Least code. A restart loses the evidence and answers a repeated attempt id as new.
3. **The engine's Postgres.** Durable, but the evidence would share a database with the system it judges, and the chaos run restarts that database.

## Decision
Protocol option 1 and records option 1. Details in [mock-payer.md](../mock-payer.md).

- A 200 carries the payer's answer, including "no such member" and "more than one match". Statuses other than 200 mean there is no answer.
- The attempt id is sent as `Idempotency-Key`. Only answers are stored under it.
- A request is **in flight** from when the payer receives it until the payer has finished with it, whether or not the caller is still listening.
- The rate limit counts every request received in any one-second window, which is invariant 1's own wording.
- Injected faults are drawn from a hash of seed, body and occurrence, so a run can be repeated.

## Why
The workers' hard problems are limits, retries and idempotency, and simple JSON leaves all of them in place. A file the payer owns is independent evidence at the cost of no new dependency. Counting abandoned requests as in flight is what a real payer would do, and the easier rule would hide a way of breaking the limit.

## Consequences
- **For slice 3:** the engine's own timeout firing does not mean the payer has finished. An in-flight slot freed then can put the payer over its cap. Slice 3 decides how long a slot is held, and also how a late reply is ever received, since a closed connection receives nothing (spec §6).
- A stored answer is repeated as it was, even if the member data has since changed.
- The mock is a single process: its limits are counted in memory, and it can't be scaled.
- A restart forgets the last second of arrivals, so the rate limit starts empty. Requests it was handling are marked `aborted`.
- A real payer or X12 would be a new adapter and a new ADR; the queue and limiter would not change.
