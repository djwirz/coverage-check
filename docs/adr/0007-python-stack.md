# 7. Python stack: FastAPI, asyncio workers, psycopg, uv

Date: 2026-11-23
Status: Accepted

## Context
The project needs an HTTP API, a mock payer, async workers that spend most of their time waiting on the network, Postgres access with careful transactions, FHIR R4 output, and a CLI. Python is the default for Dan's projects where it fits, and Token Keeper and FHIR over MCP set the toolchain (uv, ruff, mypy strict, pytest).

## Options considered
1. **Python:** FastAPI for the API and mock payer, an asyncio worker with httpx, psycopg 3 with plain SQL for the queue and limiter, Alembic for migrations, pydantic for validation.
2. **Python with an ORM for everything** (SQLAlchemy ORM). Less SQL to write, but the queue and limiter depend on exact SQL (`SKIP LOCKED`, row locks, single-statement updates), which an ORM hides.
3. **Go for the workers.** A good fit for concurrent network work, and it would cover Dan's optional Go piece. But it's a second language in the core path of a Python project, and asyncio covers waiting on the network well.

## Decision
Option 1. Python 3.12, uv, ruff, mypy (strict), pytest, Testcontainers for Postgres. FastAPI for the API and the mock payer. An asyncio worker using httpx. psycopg 3 with explicit SQL for the queue, limiter and attempts. Alembic for migrations. pydantic for inputs and payer replies. FHIR resources built as small pydantic models and validated in CI.

## Why
It matches the toolchain Dan already uses, so the learning goes into Kubernetes and concurrency, not tooling. Explicit SQL keeps the concurrency logic visible and reviewable, and that logic is the part that has to be right.

## Consequences
- The SQL for claims and the limiter is ours to get right. Integration tests with many concurrent workers cover it.
- Library APIs move. Versions are pinned in `uv.lock`, and current FastAPI, psycopg and OpenTelemetry usage is checked against their docs when each slice starts.
- Go stays out of the core. A Go load generator remains an optional idea in build-plan.
