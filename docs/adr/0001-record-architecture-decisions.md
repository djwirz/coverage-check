# 1. Record architecture decisions

Date: 2026-11-23
Status: Accepted

## Context
Coverage Check is built in the open with Claude Code. Decisions made in chat get lost, and decisions made quietly in code are hard to review or explain later. Token Keeper, Holdfast and FHIR over MCP all used ADRs this way, and it worked.

## Options considered
1. **ADRs in the repo**, reviewed in pull requests like code.
2. **Decisions in PR descriptions only.** No extra files, but they're scattered and hard to find once merged.
3. **A single design doc kept up to date.** One place to read, but the history of why things changed is lost.

## Decision
Option 1. Every decision that would be expensive to reverse gets a short ADR in `docs/adr/`, numbered in order. Each one has the sections Context, Options considered, Decision, Why and Consequences (the kit's `templates/adr.md`).

An ADR is written with `Status: Accepted` in the pull request that introduces it. The PR is where it's debated, and merging is the acceptance. A merged ADR isn't rewritten. To change a decision, a new ADR supersedes it, and the old one stays, marked "Superseded by N". Typos and broken links can be fixed in place.

## Why
It's the same practice as the other projects, so reviewers and later Claude sessions find decisions in the same place in every repo.

## Consequences
- The reasons behind the design can be reviewed, and they outlive the chat that produced them.
- There's no status bookkeeping after merge, and the record of changed minds stays intact.
- Each decision costs a little time to write up. Trivial choices don't need one.
