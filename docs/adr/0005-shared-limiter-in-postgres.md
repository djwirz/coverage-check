# 5. One shared rate limit and in-flight cap, in Postgres

Date: 2026-11-23
Status: Accepted. Superseded in part by 10 (which database clock).

## Context
The payer allows a fixed number of requests per second and of requests in flight, counted across the whole account. Workers scale from one replica to many, and pods come and go. A limit that each worker enforces on itself only holds if every worker knows how many others are running, and in a cluster that changes all the time. Invariant 1 (spec §6) says the payer's own log must never show the limit exceeded.

## Options considered
1. **A limit per worker of total ÷ replicas.** No shared state. It's wrong whenever the replica count changes, and wrong during rollouts when old and new pods overlap.
2. **A token bucket and in-flight counter in Redis** (an atomic script). Fast and common. But it's another stateful service, and its state is lost when Redis restarts.
3. **A token bucket and in-flight counter in Postgres,** taken in a short transaction with a row lock. An in-flight slot is held as a row with an expiry, so a dead worker's slots come back.
4. **A single dispatcher pod** that alone calls the payer, with workers handing it requests. The limit is simple to enforce, but it's a single point of failure and a bottleneck. It's also the very thing the chaos run is meant to kill.

## Decision
Option 3. One bucket row per payer, holding a rate and burst, refilled by elapsed time when tokens are taken. In-flight slots are rows with an expiry. A worker takes a token and a slot in one short transaction before each call, and gives the slot back afterwards. A worker that gets neither waits a little with jitter. It never calls anyway.

## Why
The limit lives in one place, whatever the number of workers, so scaling and rollouts can't break it. Postgres is already the source of truth (ADR 0004), and at a payer limit of tens of requests per second, a short transaction per call is a small load. Slots that expire mean a killed pod can't leak capacity forever.

## Consequences
- Every payer call costs an extra round trip to Postgres. That's measured, and it's fine at this scale.
- Clock skew between pods doesn't matter, because refill time comes from the database clock (`now()`).
- The limiter is set a little below the payer's real limit, so network jitter can't push the payer's view over. The margin is configurable, and the payer's log is the judge.
- Revisit with Redis (option 2) if the limit or the worker count grows far beyond what a lock row handles comfortably.
