# coverage-check: instructions for Claude

A service that checks insurance coverage in bulk against a rate-limited, unreliable payer API. Workers share one rate limit and one concurrency cap, so the payer's limit is never exceeded, and every check ends in exactly one final state, as FHIR R4. It runs on Kubernetes (kind) and keeps those promises when pods are killed and scaled mid-batch. Use they/them for Dan.

## Status
Slices 1 to 5 (spec §11) are built: the project and CI, the schema, the batch API, members from `synthetic-cohort` (ADR 0008), the mock payer with its image (ADR 0009, `docs/mock-payer.md`), workers with the shared limiter, retries, FHIR results and the invariant report (ADR 0010), requeue, graceful shutdown, official FHIR validation in CI and crash tests. and the system on kind with `make up` and an end-to-end run in CI (ADR 0011). Slice 5 added the chaos run, NetworkPolicies and the PodDisruptionBudget, and settled on no autoscaling (ADR 0012). The cluster work is verified in CI only: a local cluster needs more free disk than Dan's Mac had. Next is slice 6, observability and the deploy decision (spec §13 Q3 and Q4; budget alarm first). `docs/spec.md` and the ADRs were accepted in PR #1. Open questions are in spec §13; each is settled by the slice it names. The judgment behind the project is in `docs/artifact-spec.md`.

## How we work
- Pull requests only. Never push to `main`. Merged PRs are the project's record.
- Spec first, then one thin end-to-end slice per PR (spec §11). Start each slice in plan mode.
- **Problem first.** Every tool, feature and dependency has to earn its place through the problem: an invariant, a failure mode in spec §6, or an acceptance criterion in §10. Each slice's plan says which one for anything new. Skills shown are a by-product. Never add something just to show a skill, and never cut something the invariants need to save time on polish. This applies to Kubernetes features too: an operator, a chart or an autoscaler comes in only when the problem needs it.
- Every decision that would be expensive to reverse gets an ADR in `docs/adr/` (format: ADR 0001). Don't decide quietly in code.
- ADRs are written as `Accepted` in the PR that adds them, and merging is the acceptance. Never rewrite a merged ADR's decision. Add a new ADR that supersedes it.
- When the spec and the code disagree, stop and ask. If the decision changes, update the spec in the same PR.
- Kubernetes is new ground for Dan. In each Kubernetes PR, explain what each new object does and why it's there, briefly, in the PR description, so the PR doubles as notes.
- Built with Claude Code in the open: the case study gets a section on how AI was used and where it was wrong, so note real mistakes in PR descriptions.

## Constraints
- Never employer code, data or names beyond what's public. Synthetic data only.
- Python, the default here (ADR 0007).
- Free tiers first. Nothing paid without Dan's go-ahead, and a budget alarm on any paid account before anything is deployed.
- **Cluster safety:** scripts and `make` targets act only on the kind cluster this repo creates. They check the current kubectl context and refuse to run against any other. Never run `kubectl` or `helm` against a cloud context unless Dan asks for it in that session.
- Secrets never in the repo, manifests, logs or PR text.
- Target depth: deployed (spec §13 Q3), tests and CI, ADRs, observability.
- Numbers in docs: honest estimates in words, never false precision.
- Check current kind, kubectl, Kustomize, KEDA, FastAPI, psycopg and OpenTelemetry usage against their docs before writing code. Don't rely on memory.

## Invariants (spec §6): never break these
1. **Under the limit:** the payer never sees more requests per second than the limit, or more in flight than the cap, at any replica count.
2. **Exactly one final state:** every check ends as covered, not covered or exception, with exactly one result.
3. **Nothing lost:** a dead worker's checks are claimed again after their lease expires.
4. **Idempotent:** re-submitting a batch creates nothing new. A repeated attempt never counts twice. A late reply never overwrites a newer result.
5. **Honest retries:** only retryable outcomes are retried, within a cap. Every exception has a reason.
6. **Valid output:** every result is a valid FHIR R4 `CoverageEligibilityResponse`.
