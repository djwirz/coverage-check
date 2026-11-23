# Artifact spec: Coverage Check

Filled from the kit's `templates/artifact-spec.md` before any agent builds code. Draft for PR #1, 2026-11-23.

## One line
A service that checks patients' insurance coverage in bulk against a payer API that's slow, rate-limited and often fails, and still gives exactly one honest answer per patient, as FHIR, without ever going over the payer's limit. It runs on Kubernetes, and keeps those promises when its pods are killed mid-batch.

## What it exercises
**Python, FastAPI, async workers, PostgreSQL, rate limiting and retries, idempotency, FHIR R4, Docker, Kubernetes**, plus testing, CI and observability.
This list comes out of the design. It isn't a target the design was built to hit. Each item is there because the problem needs it. The payer's limit is shared across every worker, so the limiter has to be shared too. Workers die and restart, so jobs must be leased, resumable and idempotent. The answer has to fit what clinical systems already read, so it's FHIR. And the claim "it holds up when pods die and replicas change" means nothing until it runs on a real scheduler that kills pods, which is why Kubernetes is here. No skills score gates any slice, and a tool is never added just to show it off (CLAUDE.md).

## Problem and users
- **Who has the problem:** front-desk and billing teams at clinics, pharmacies and digital health companies, who need to know before a visit or a shipment whether a patient's coverage is active. Also the engineers who run the integration that answers them.
- **What they do today:** run eligibility checks one at a time in a payer portal, or a nightly batch script that hammers a clearinghouse. The script gets throttled, retries everything, sends some checks twice, quietly drops others, and leaves a CSV of "errors" that nobody owns.
- **Why it matters:** a wrong "covered" means a denied claim later. A missing answer means staff call the payer by hand. Going over the payer's limit gets the account throttled or cut off for everyone. People only trust these systems when every patient ends in a clear state and the system can show it stayed within the limit.

## Scope
- **Must have:**
  - Batch submission over HTTP with an idempotency key. Sending the same batch twice creates no new work.
  - A job queue in Postgres with leased claims, so a worker that dies mid-check loses nothing (ADR 0004).
  - One shared rate limit and concurrency cap across every worker replica (ADR 0005).
  - Retries with backoff and jitter that honor `Retry-After`, only for errors worth retrying. Permanent failures go straight to an exception queue.
  - A mock payer that misbehaves when told to: 429s, 5xx errors, timeouts, slow and malformed responses, and replies that arrive after the request timed out (ADR 0006).
  - Results as FHIR R4 `CoverageEligibilityResponse` resources, validated.
  - An exception queue that people can see and requeue from.
  - Everything running on a local Kubernetes cluster from one command, and the same cluster in CI (ADRs 0002, 0003).
  - A chaos run: pods killed and replicas scaled mid-batch, with the invariants checked afterward from the payer's own request log and the database.
  - Tests and CI, metrics, traces, alerts and a runbook.
- **Nice to have:**
  - Autoscaling workers on queue depth (spec §13).
  - A public deployment on a cloud cluster (spec §13).
  - A small read-only page for batches and the exception queue.
  - Helm packaging alongside Kustomize.
- **Non-goals (on purpose):**
  - Real payers, clearinghouses or X12 270/271. The payer protocol is a simplified JSON shaped by public eligibility concepts.
  - Real patient or member data. Synthetic only.
  - Claims, prior authorization or benefits calculation.
  - Multi-tenant auth and user accounts. One API token is enough for v1.
  - A production-grade managed Postgres or service mesh.

## Success metric
- **Never over the limit:** the mock payer's own log shows the request rate never went above the configured limit, at any replica count, through the chaos run.
- **Exactly one result:** every submitted check ends in exactly one final state: covered, not covered or exception. No duplicates and nothing lost, including with pods killed mid-flight.
- **Throughput close to the limit:** a large batch finishes in about the time the payer's limit allows, not far beyond it. The README reports the real number in words with its spread.
- **Recovery:** after a worker pod is killed, its leased checks are picked up again within the lease time.
- **Cost:** nothing locally. Any cloud deploy has a budget alarm and stays at a few dollars, or is brought up only for a demo window.

## Constraints
- **Time:** two to four days of focused sessions (build-plan ROADMAP, pace as of 2026-11-23). Kubernetes is new ground, so expect it to take longer than Holdfast.
- **Budget:** free tiers first. kind on the laptop and in CI costs nothing. Any paid cluster needs a budget alarm before the first deploy.
- **Data:** synthetic members and coverage only.
- **Privacy:** no employer details. Any comparison to past work stays at the level of public patterns, with no employer names.
- **Hosting:** local cluster first. A public deploy is decided at slice 6 (spec §13).

## Quality plan
- **Tests:**
  - Unit tests for the retry policy, the rate limiter's arithmetic, payer-response mapping and FHIR mapping.
  - Integration tests against real Postgres (Testcontainers): the claim and lease logic under many concurrent workers, idempotent submit, lease expiry.
  - Contract tests for the mock payer's failure modes.
  - End-to-end tests on a kind cluster in CI: submit a batch, get FHIR results.
  - The chaos run: pod kills, scaling, a degraded payer, with invariant checks.
- **Evals:** no AI features in this project.
- **Observability:**
  - Structured logs with batch, check and attempt ids.
  - Prometheus metrics: queue depth, in-flight checks, payer request rate against the limit, outcomes, retries, lease expiries.
  - OpenTelemetry traces from submit through worker to the payer call.
  - A Grafana dashboard, alerts and a runbook.
- **Accessibility and performance:** no UI in v1. Performance is measured against the payer's limit, not promised.
- **Security basics:**
  - Secrets in Kubernetes Secrets, never in manifests in the repo. `.env` is git-ignored.
  - Every API input validated with pydantic.
  - An API token on the submit endpoint.
  - Containers run as non-root with read-only filesystems where they can.
  - gitleaks in CI.
- **CI/CD:** every PR runs ruff, mypy, unit and integration tests, image builds, a kind cluster with the end-to-end suite, and gitleaks. Deploys to any cloud cluster happen only from `main`, if there is one.

## Depth target
Current rung: 0 · Target rung: 3 (engineered)

## Build stack (in order)
1. Python 3.12 with uv, ruff, mypy (strict), pytest (ADR 0007).
2. FastAPI for the API and the mock payer; asyncio workers with httpx; psycopg and Alembic on PostgreSQL (ADRs 0004, 0007).
3. Docker images, a kind cluster and Kustomize overlays (ADRs 0002, 0003).
4. Prometheus, Grafana and OpenTelemetry in the cluster.
5. Autoscaling and cloud hosting, if they earn their place: decided at slices 5 and 6 (spec §13).
