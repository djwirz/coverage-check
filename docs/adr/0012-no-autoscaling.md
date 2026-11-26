# 12. No autoscaling: workers are scaled by hand

Date: 2026-11-26
Status: Accepted

## Context
Spec §13 Q2 asked whether workers should scale themselves. The pace of a batch is set by the shared limiter (ADR 0005, 0010), not by the number of workers: two pods with four tasks each already fill it. Workers mostly wait on the payer, so CPU says nothing about whether more are needed.

## Options considered
1. **No autoscaling.** A fixed number of replicas, changed by hand.
2. **KEDA on queue depth**, with its Postgres scaler. Fewer workers when the queue is empty.
3. **A HorizontalPodAutoscaler on a custom metric**, through a Prometheus adapter.

## Decision
Option 1.

## Why
Neither autoscaler fixes a failure in spec §6 or meets an acceptance criterion: more pods would not finish a batch sooner, and idle pods cost nothing on kind. Each would add a component to install, pin and explain. What needs showing is that the limit holds at any replica count, and the chaos run shows that by scaling from 1 to 10 by hand.

## Consequences
- Workers sit idle when there is no work.
- A payer with a higher limit would need more workers, set by hand.
- Revisit if the cluster is paid for by the hour (slice 6), or if several payers with their own limits make the right number of workers vary.
