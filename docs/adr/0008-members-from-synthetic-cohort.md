# 8. Members come from synthetic-cohort, pinned by tag

Date: 2026-11-26
Status: Accepted

## Context
Check batches and the mock payer (slice 2) need members with a name, birth date, member id, payer and a coverage state, with every state present. Spec §13 Q6 asked for Synthea or a small seeded generator in this repo, and recommended the generator unless Synthea's output held usable payer data. Since then, [synthetic-cohort](https://github.com/djwirz/synthetic-cohort) has been released: one cohort shared between projects, with a `members.json` built from Synthea's payer data plus assigned problem states (its spec §5 and ADR 0003). FHIR over MCP already depends on its `v0.1.0` tag.

## Options considered
1. **A seeded generator in this repo.** Full control over the number and mix of members. A second set of people to design, test and keep reproducible, unrelated to the other projects'.
2. **A copy of `members.json` committed here.** No dependency, but the copy can drift from its source without anyone noticing.
3. **Depend on synthetic-cohort at a tag.** The same people everywhere, hashed and tested at the source. A change to the data needs a new tag there.

## Decision
Option 3. `synthetic-cohort` is a runtime dependency pinned to `v0.1.0`, with the commit locked in `uv.lock`. This repo reads `synthetic_cohort.members()` and `synthetic_cohort.manifest()` and generates no people of its own.

- **Service date:** seeded checks use the cohort's reference date, `manifest()["generation"]["reference_date"]`, because the coverage states are true on that day. The API accepts any date.
- **Batch size:** there are a few dozen members. A larger batch goes round them again; each check is its own row and its own payer call. No load-only members.
- **Expected answers:** `coverage_state` is what the payer should say. It is never part of a check request, and the API refuses unknown fields.
- **Payer names** are Synthea's and include real insurers. They stay, with a note that the people and their coverage are made up.

## Why
The data already exists, covers every state this project needs, and costs one dependency line. Repeating members is enough to load the queue and the limiter, which care about the number of checks and not about who they are.

## Consequences
- `missing` and `ambiguous` are only labels. The slice 2 mock payer has to produce "no such member" and "more than one match".
- A large batch asks about the same members many times, so it shows volume, not variety.
- Installing needs access to GitHub, including in CI and later in image builds.
- Changing the people, states or payer names means a new tag in synthetic-cohort and a deliberate upgrade here.
- The limiter belongs to the payer API this service calls (one account), not to the insurer name on a check (ADR 0005).
