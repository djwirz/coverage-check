# Coverage Check: spec

Status: Accepted (PR #1), updated with slices 1 to 6a · Date: 2026-11-26 · Owner: Dan

## 1. Problem

Before a visit, a refill or a shipment, someone needs to know whether a patient's insurance is active and what it covers. In bulk, that means sending thousands of eligibility checks to a payer (or a clearinghouse) whose API:
- **is rate-limited**, with one limit for the whole account. Go over it and you get 429s, then a throttled or suspended account.
- **is unreliable:** 5xx errors, timeouts, slow replies, the odd malformed body, and replies that arrive after the caller gave up.
- **has no undo:** a check sent twice is two billable calls, and two different answers if coverage changed in between.

The usual batch script fails in familiar ways:
- **Over the limit:** more workers means more throughput until the payer starts refusing. Each worker limits itself, but nothing limits all of them together.
- **Duplicates:** a retry after a timeout sends the check again while the first reply is still on its way. A re-run batch sends everything again.
- **Lost work:** a worker crashes mid-batch and its checks never finish. Nobody notices until a patient is at the desk.
- **Retrying the wrong things:** "member not found" retried ten times, while a 503 gives up after one.
- **An errors CSV nobody owns** instead of a queue someone can work through.

Coverage Check is a small service that takes batches of checks, runs them through workers that share one rate limit and one concurrency cap, retries only what's worth retrying, and ends every check in exactly one final state: **covered**, **not covered**, or **exception** (for a person to look at). Results are FHIR R4 `CoverageEligibilityResponse` resources. It runs on Kubernetes because the claim worth making is that these promises hold when pods are killed, rescheduled and scaled mid-batch. The way to show that is on a real scheduler.

**What a viewer sees.**
1. One command brings up a local cluster with the API, workers, the mock payer, Postgres and a dashboard.
2. A batch of a few thousand checks about synthetic patients goes in. The dashboard shows the payer request rate climbing to the limit line and staying under it.
3. Mid-batch, worker pods are deleted and the deployment is scaled up and down. The rate stays under the line, and the leased checks are picked up again.
4. The batch finishes. The invariant report says it all: every check in one final state, no duplicate payer calls for one attempt, and a peak rate under the limit. The exception queue holds the patients the payer couldn't find, with the reason.
5. One check's trace, from submit through the worker to each payer attempt and its retry.

That sequence is the README GIF and the case study.

### Why this is worth building (portfolio framing)
It shows Python for a real backend service, async workers, PostgreSQL used as a queue with real concurrency control, rate limiting and retry design, idempotency end to end, FHIR R4 output, and Kubernetes run for real: probes, graceful shutdown, config and secrets, scaling, and failure on purpose. It draws only on public material about eligibility checking and FHIR.

## 2. Non-goals (v1)

- **Real payers, clearinghouses or X12.** The mock payer speaks a simplified JSON protocol shaped by public eligibility concepts (ADR 0009).
- **Real data.** Synthetic members and coverage only.
- **Claims, prior authorization or benefit calculation.** The answer is: is coverage active, under which plan, and a few plain benefit fields the mock returns.
- **Accounts and multi-tenancy.** One API token.
- **A UI.** The API, the dashboard and a CLI are enough. A small read-only page is a nice-to-have.
- **Production-grade managed infrastructure:** no service mesh, no multi-region, no managed Postgres in v1.

## 3. Appetite

- **Time:** two to four days of focused sessions. It may take longer than Holdfast because Kubernetes is new ground. The slices that carry the learning are 4 and 5.
- **Money:** nothing locally (kind, in CI too). Any cloud cluster has a budget alarm first and stays at a few dollars, or exists only for a demo window (§13 Q3).
- **Shape:** seven thin PRs after this one, local-first. The first three don't touch Kubernetes. Kubernetes comes in at slice 4, once the code it runs is already correct.
- **If time runs short, cut in this order.** The project exists to prove three things: the limit holds across replicas, every check ends exactly once, and both survive pods dying on a real cluster. Cut what proves none of those first:
  1. A public cloud deploy.
  2. Autoscaling (scale by hand in the chaos run instead).
  3. Helm packaging and a UI.
  4. Traces (logs with ids and metrics stay).

  **Never cut:**
  - The shared limiter and concurrency cap.
  - Leases and idempotency keys.
  - Separating retryable from permanent errors.
  - The mock payer's request log and the invariant report built from it.
  - The chaos run on a real cluster.
  - The one-command local cluster.
  - A budget alarm before anything paid.

## 4. Architecture

```
 client / CLI                  Kubernetes namespace: coverage-check
 ────────────                  ─────────────────────────────────────────────────────────────────────
 POST /batches  ──▶  api (Deployment, FastAPI)
 (Idempotency-Key)     validates, writes batch + checks ─▶ Postgres (StatefulSet)
 GET  /batches/{id}                                           checks: queued → leased → done / exception
 GET  /checks/{id}      ◀── FHIR CoverageEligibilityResponse  limiter: shared token bucket + in-flight cap
 GET  /exceptions                                             attempts: one row per payer call
 POST /exceptions/{id}/requeue                                   ▲
                                                                 │ claim (FOR UPDATE SKIP LOCKED, lease)
                       worker (Deployment, N replicas, asyncio) ─┘ take a token, call the payer, record
                         │ httpx, timeout, attempt id as idempotency key
                         ▼
                       mock-payer (Deployment, FastAPI)
                         seeded synthetic members · failure profile from a ConfigMap · request log
 metrics (reads Postgres) · Prometheus · Grafana · OpenTelemetry collector · Jaeger (in cluster)
```

### Components
- **API.** FastAPI. `POST /batches` takes a list of checks (member id, name, birth date, payer, service date) and an `Idempotency-Key` header. The same key with the same body returns the original batch; the same key with a different body is a 409. It writes the batch and its checks in one transaction. Reads return status and results. Exceptions can be listed and requeued.
- **Queue (Postgres).** Checks move through `queued → leased → done | exception`. A worker claims a few at a time with `FOR UPDATE SKIP LOCKED` and sets a lease expiry. A check whose lease expires goes back to `queued`. Each payer call is an `attempt` row with its own id, written before the call is made (ADR 0004). One transaction takes the capacity, the check and the attempt row together (ADR 0010).
- **Limiter.** One shared token bucket (requests per second) and one in-flight cap (concurrent payer calls), both kept in Postgres and taken in a short transaction. They belong to the payer API being called (one account, one bucket), not to the insurer named on a check. Neither depends on how many workers are running. A worker that can't get a token waits. It never calls anyway (ADR 0005). `burst + rate` stays under the payer's limit, and a slot is freed only when the payer has replied or never got the request; otherwise it expires (ADR 0010).
- **Worker.** An asyncio process running a small pool of tasks, capped well below the global in-flight cap. Each task: take a token, an in-flight slot and a check, call the payer with the check's idempotency key (the same on every retry), classify the outcome, write the result or schedule a retry. One timeout, longer than the payer's longest call (ADR 0010). On SIGTERM it stops claiming, finishes what it holds, hands back anything still unfinished when the grace period ends, and exits. The grace period is longer than the attempt timeout.
- **Retry policy.** Retry 429s (honoring `Retry-After`), 409 "still in progress", 5xx errors, timeouts and connection errors, with exponential backoff and full jitter, up to an attempt cap. "Member not found", "invalid request" and malformed replies that repeat go to `exception` with a reason. A retry happens by setting the check's `not_before` time. Workers never sleep on it.
- **Mock payer.** A separate FastAPI service that knows the synthetic members from `synthetic-cohort` (ADR 0008). Their `missing` and `ambiguous` states are only labels there, so the mock makes them happen: no such member, or more than one match. Its failure profile is a file, from a ConfigMap in the cluster: how often each failure happens, a latency range, its own rate limit and in-flight cap, and late replies (docs/mock-payer.md). It honors idempotency keys, so a repeated key returns the first answer. It keeps its own request log in SQLite, which is the evidence for invariant 1 (ADRs 0006 and 0009). A request counts as in flight there until the payer has finished with it, even if the caller gave up.
- **FHIR mapping.** A payer reply becomes a `CoverageEligibilityResponse`: outcome, whether coverage is in force, the insurer and plan, the service date, and the benefit items the mock returns. It references a contained `Coverage` and `Patient`. An exception gets one too, with outcome `error` and the reason. CI validates examples from this mapping with HL7's official validator for R4. It runs with no terminology server, so codes from outside the R4 packages are not checked there.
- **Kubernetes.** Images built locally and loaded into kind. Kustomize base plus overlays for `local`, `ci`, and `cloud` if there is one (ADR 0003). Each Deployment has readiness and liveness probes (workers serve no HTTP, so theirs look at files the worker touches), resource requests and limits, a non-root security context, and config from ConfigMaps and Secrets. Workers get a termination grace period longer than their longest call. A PodDisruptionBudget covers the API (against evictions; it does not limit `kubectl delete`).

## 5. Interfaces
- **HTTP API:** the endpoints in §4. Bearer token. Errors as RFC 9457 problem details.
- **CLI** (`coverage-check`). `seed`, `worker` and `report` exist from slice 3. `submit`, `status` and `exceptions` are thin API clients left to slice 7, if the demo needs them:
  - `submit <file>` sends a batch.
  - `status <batch>` shows its progress.
  - `exceptions [--requeue <id>]` lists or requeues exceptions.
  - `seed` writes a sample batch from the synthetic members (ADR 0008), with the cohort's reference date as the service date. The expected answer (`coverage_state`) is never in a request.
  - `worker` runs a worker.
  - `report <batch>` prints the invariant report from the database and the mock payer's log.
- **Cluster:**
  - `make up` creates the kind cluster, builds and loads images, applies the local overlay, waits for readiness and seeds.
  - `make down` removes the cluster.
  - `make e2e` submits a batch, checks the invariants and validates the stored results.
  - `make chaos` runs the chaos scenario against a running cluster.
- **Config:** payer limit, in-flight cap, lease length, attempt cap, backoff, and the failure profile, all in ConfigMaps. The API token and database password in Secrets. No secret values in the repo.

## 6. Invariants and failure modes

The invariants (checked by the tests and by the invariant report after every chaos run):
1. **Under the limit:** in any one-second window of the mock payer's request log, the request count never goes above the configured limit, and concurrent requests never go above the in-flight cap, whatever the replica count.
2. **Exactly one final state:** every submitted check ends in exactly one of covered, not covered or exception, and holds exactly one result.
3. **Nothing lost:** a check held by a worker that dies is picked up again after its lease expires. No check is still queued or leased once its batch is reported done.
4. **Idempotent:** a re-submitted batch with the same key creates no checks. A payer call repeated under the same key is never billed twice. A late reply to an expired attempt is recorded but never overwrites a newer result.
5. **Honest retries:** only retryable outcomes are retried, within the attempt cap. Every exception has a reason, and no check is retried forever.
6. **Valid output:** every result is a valid FHIR R4 `CoverageEligibilityResponse`.

| Failure | What happens |
|---|---|
| Payer returns 429 | The attempt is recorded. The retry waits at least `Retry-After`. The limiter is not loosened. |
| Payer 5xx, timeout or connection error | Retry with backoff and jitter, up to the cap, then an exception with reason `payer_unavailable`. |
| Member not found / invalid request | No retry. An exception with the payer's reason. |
| Malformed reply | Retried once (it might be transient). If it happens again, an exception with reason `malformed_response` and the body kept for inspection. |
| Slow or late reply | The one timeout is longer than the payer's longest call, so it is heard. If the call does time out, the retry carries the same key: the payer says it is still in progress, or replays its answer without billing again. |
| Reply reaches a worker that lost its lease | Recorded against that attempt. The check is not touched (invariant 4). |
| Worker pod killed mid-call | Its leases expire and the checks are claimed again. The new attempt has a new id and the same payer key, so an answer the payer already gave is not billed again. |
| Worker receives SIGTERM | It stops claiming and finishes in-flight checks within the grace period. Any left are released: queued again, the attempt not counted against the cap. Exit 0. |
| Postgres restarts | The API answers 503 and workers wait and try again; on Kubernetes both also fail their readiness checks (slice 4). No check is marked done without its result committed. |
| Duplicate batch submission | The original batch is returned (same key and body) or a 409 is returned (same key, different body). |
| Scaled from 2 to 10 workers | Throughput stays at the limit. The rate does not go up (invariant 1). |

## 7. Observability
Who counts what, and where traces are kept: ADR 0013. What each alert means: [runbook.md](runbook.md).

- **Logs:** structured JSON with batch, check and attempt ids, worker pod name, outcome and timing. No member details above debug level, even though the data is synthetic.
- **Metrics (Prometheus):** read from the database by one `metrics` process, and from the mock payer for what it received.
  - Queue depth by state.
  - In-flight payer calls against the cap.
  - Payer requests per second against the limit.
  - Outcomes by class.
  - Retries and their reasons.
  - Lease expiries.
  - Late replies.
  - Time from submit to final state.
- **Traces (OpenTelemetry):** one trace per check, from submit through each claim and payer attempt, kept in Jaeger. The trace screenshot is an acceptance criterion. An attempt whose worker was killed leaves no span.
- **Dashboard (Grafana):** the rate against the limit line, queue depth, outcomes and lease expiries. It's the backdrop for the GIF.
- **Alerts:**
  - Payer rate within a small margin of the limit for a sustained period, an early warning.
  - Any one-second window over the limit, which means invariant 1 is broken.
  - Exceptions rising fast.
  - Leased checks older than several lease lengths.
  - The numbers themselves missing, because without them every other alert is silent.
  - The budget alarm, if there is a cloud deploy.

## 8. Security
- The API token and database credentials are in Kubernetes Secrets, created by `make up` from a git-ignored `.env`. They never appear in manifests, logs or PR text.
- Inputs are validated with pydantic: ids, dates, batch size caps.
- Containers run as non-root with read-only root filesystems where possible, and no privilege escalation.
- NetworkPolicies refuse all incoming connections by default, then let only workers reach the mock payer, and only the API, workers and the migration job reach Postgres. `make e2e` proves they are enforced (ADR 0011).
- gitleaks in CI. `.env.example` holds placeholders only.

## 9. Testing and CI
- **Unit:**
  - The retry classifier and backoff.
  - The limiter's arithmetic.
  - Payer-reply mapping.
  - FHIR mapping.
  - Idempotency-key handling.
- **Integration (Testcontainers Postgres):**
  - Many concurrent workers claiming from one queue, with no double claims.
  - Lease expiry and reclaim.
  - The shared limiter under contention.
  - Idempotent submit.
  - A late reply arriving after a newer result.
- **Contract:** each failure mode of the mock payer, and its idempotency.
- **End to end on kind (CI):**
  - Bring up the cluster with the `ci` overlay.
  - Submit a batch and wait for it to finish.
  - Check the FHIR results validate.
  - Run the invariant report.
  - Check that Prometheus scraped it, every dashboard query has data, and Jaeger holds a retried check's trace.
  - Fire every alert once, on purpose.
- **Chaos (local by hand; a short version in CI):**
  - Delete worker pods mid-batch.
  - Scale workers up and down.
  - Restart Postgres.
  - Raise the payer's failure rate.
  - Try to evict both API pods.
  - The invariant report must pass, and the run must show the disruption really happened.
- **Full chaos run** (3,000 checks, three times): its own workflow, on demand and when the cluster files change.
- **CI on every PR:** ruff, mypy, unit, integration, contract, alert-rule tests, official FHIR validation, image builds, kind end to end with the short chaos run, and gitleaks.

### Local development
`docker compose up` runs Postgres and the mock payer for slices 1 to 3. `uv run` runs the API (uvicorn) and the workers on the laptop, and `uv run coverage-check ...` is the CLI. From slice 4, `make up` runs everything on kind.

## 10. Acceptance criteria (v1 "done", depth 3)
1. **One command:** `make up` on a clean machine with Docker and make brings up the whole system and seeds it, fetching pinned kind and kubectl itself (ADR 0011). It needs several gigabytes of free disk. `make down` removes it.
2. **Correct under chaos:** a chaos run on a batch of a few thousand checks passes the invariant report. It's in the README with the numbers in words.
3. **Throughput:** the batch finishes in about the time the payer's limit allows, with the measured figure and spread in the README.
4. **Tests and CI:** every automated category in §9 runs on each PR, including the kind end-to-end run.
5. **Observed:** the dashboard, a trace screenshot of one check with a retry, and alerts that have each been fired once on purpose, with `docs/runbook.md` covering each.
6. **Deployed:** per §13 Q3. Either a public cluster from `main` with a budget alarm first, or, if that's cut, CI's kind cluster as the deployed environment, with the reason in an ADR.
7. **Record:** ADRs for every expensive decision, and each PR says how AI was used and what it got wrong.

Stretch: autoscaling on queue depth, a cloud deploy, Helm packaging, a read-only page.

## 11. Proposed slice plan (one PR each, after this spec)
1. **Local foundation:**
   - The uv project with ruff, mypy, pytest, and CI with gitleaks.
   - Docker Compose for Postgres, plus the Alembic schema (batches, checks, attempts, limiter).
   - The API's `POST /batches` with idempotency, and the read endpoints.
   - Synthetic members from `synthetic-cohort` and `seed` (ADR 0008).
2. **The mock payer:**
   - The cohort's members, the simplified eligibility protocol (§13 Q5) and the failure profile.
   - Idempotency on attempt ids and the request log.
   - Contract tests.
   - The Dockerfile (multi-stage, non-root, one image for every service), so Compose can run the mock.
3. **Workers, correct on the laptop,** in two PRs:
   - **3a:** leased claims, the shared limiter and in-flight cap, the retry policy, FHIR results, the invariant report, and integration tests with concurrent claimers.
   - **3b:** requeue from the exception queue, graceful shutdown, the official FHIR R4 validator in CI, and tests that kill worker processes and restart Postgres.
4. **Onto Kubernetes:**
   - The kind cluster config, with the image from slice 2 loaded into it.
   - The Kustomize base and the `local` and `ci` overlays, with probes, resources, ConfigMaps and Secrets.
   - Postgres in the cluster (§13 Q1).
   - `make up` and `make down`, and the kind end-to-end run in CI.
5. **Correct under chaos:**
   - `make chaos` (pod kills, scaling, a Postgres restart, a degraded payer) and its short CI version.
   - The PodDisruptionBudget and NetworkPolicies.
   - Autoscaling if it earns its place (§13 Q2).
   - The throughput measurement.
6. **Observe and deploy:**
   - Prometheus, Grafana and the OpenTelemetry collector in the cluster, with metrics, traces, the dashboard, alerts (each fired once) and the runbook.
   - Then the deploy decision (§13 Q3 and Q4): budget alarm first, the `cloud` overlay, deploy from `main` and a smoke test, or the ADR for staying local.
7. **Demo polish:** the README GIF, the trace screenshot, the invariant report and throughput in the README, and case-study notes.

A companion course: master.dev's "Kubernetes: Production-Grade Container Orchestration" (build-plan `research/courses-2026-11-18.md`), useful before slice 4.

## 12. What runs where
| | Slices 1–3 | Slices 4–5 | Slice 6+ |
|---|---|---|---|
| Laptop | Compose: Postgres and the mock payer; API and workers with uv | kind: everything | kind: everything plus observability |
| CI | Testcontainers Postgres | kind cluster in the job | the same, plus the deploy from `main` if there is one |
| Cloud | none | none | per §13 Q3 |

## 13. Open questions (settle before slice 1 unless noted)
1. **Postgres in the cluster, and the CNI.** Settled at slice 4: a plain StatefulSet and kind's default networking (ADR 0011). Slice 5 showed that it enforces NetworkPolicy.
2. **Autoscaling.** Settled at slice 5: none, workers are scaled by hand (ADR 0012).
3. **A public deploy, and where.** A managed cluster isn't free (build-plan `research/free-tiers-2026-11-18.md`). Options:
   - k3s on an always-free VM.
   - A small managed cluster brought up only for demo windows and then torn down.
   - No public deploy, with CI's kind cluster as the deployed environment and the reason in an ADR.

   Recommendation: decide at slice 6, after checking current prices and free limits, with the budget alarm first. Local-only is an honest outcome if the cost isn't worth it.
4. **Terraform.** Only if Q3 picks a cloud cluster or VM. Then Terraform provisions it, because a hand-built cluster can't be rebuilt or reviewed. It isn't added otherwise (problem first, CLAUDE.md). Decide with Q3.
5. **The payer protocol.** Settled at slice 2: simplified JSON (ADR 0009, docs/mock-payer.md).
6. **Synthetic members.** Settled at slice 1: they come from `synthetic-cohort`, pinned by tag (ADR 0008).
7. **Case-study angle.** How much comparison to past professional work is safe? Public patterns only, no employer names. Decide in slice 7.
