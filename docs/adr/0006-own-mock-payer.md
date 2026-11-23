# 6. Our own mock payer, unreliable on purpose

Date: 2026-11-23
Status: Accepted

## Context
The system has to be shown working against a payer that fails, and the failures must be repeatable so tests and the chaos run mean something. Real payer and clearinghouse sandboxes need sign-ups and agreements, speak X12, are well-behaved compared with the real thing, and aren't free to hammer. Invariant 1 needs a record of what the payer actually received.

## Options considered
1. **Our own mock payer service.** FastAPI, seeded synthetic members, a failure profile from config (429s with `Retry-After`, 5xx errors, timeouts, slow and malformed replies, late replies), its own rate limit, idempotency on attempt ids, and a request log.
2. **A real clearinghouse sandbox.** Realistic protocol and data shapes. But it needs signup and terms, uses X12, fails rarely and not on demand, and may forbid load testing.
3. **A generic HTTP mocking tool** (WireMock or similar) with scripted responses. Quick to set up, but stateful behavior (idempotency, its own rate limit, late replies, a request log usable as evidence) takes as much work as writing the service.

## Decision
Option 1. `mock-payer` is a separate service in the repo and its own Deployment in the cluster. Its protocol is a simplified JSON request and reply (spec §13 Q5). Its failure profile and limit come from a ConfigMap. It keeps a request log that the invariant report reads.

## Why
Failures on demand, with fixed seeds, make retries and chaos testable. The payer's own log is independent evidence that the limit held, separate from the engine's own counting. And it keeps the project synthetic and free.

## Consequences
- The mock is only as realistic as its failure profile. The profile and its sources (public descriptions of eligibility APIs and their failure modes) are written down in the repo.
- The engine talks to the payer through a small client interface, so a real payer adapter could be added later without touching the queue or limiter.
- The mock is part of the test surface, with its own contract tests.
