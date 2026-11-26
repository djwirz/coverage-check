# Coverage Check

Checks insurance coverage in bulk against a payer API that's slow, rate-limited and often fails. Every worker shares one rate limit, so the payer's limit is never exceeded, and every patient ends in exactly one honest state: covered, not covered, or an exception a person can work through. Results are FHIR R4. It runs on Kubernetes and keeps those promises while its pods are killed and scaled mid-batch.

**Status:** early. Slices 1, 2 and 3a of [the plan](docs/spec.md#11-proposed-slice-plan-one-pr-each-after-this-spec) are built: batches go in, workers check them against a [mock payer](docs/mock-payer.md) that misbehaves on demand, and every check ends covered, not covered or as an exception, with a FHIR result. Still to come: requeue, graceful shutdown, Kubernetes and observability.

On a laptop, 3,000 checks with two workers took about four minutes and passed every line of the invariant report. The pace is set by the limiter (16 a second against the payer's 20), and dips when hanging calls fill the in-flight cap. Design: [docs/spec.md](docs/spec.md) and [docs/adr/](docs/adr/).

**Data:** synthetic only. Members come from [synthetic-cohort](https://github.com/djwirz/synthetic-cohort). Some insurer names are real companies', from Synthea's built-in list; the people, member ids and coverage are made up. There is no real payer: this project will only call its own mock.

## Run it locally

Needs [uv](https://docs.astral.sh/uv/) and Docker. Copy `.env.example` to `.env` and replace every `change-me`.

```bash
docker compose up -d --wait
```

That starts Postgres and the mock payer (on `127.0.0.1:8001`, building its image the first time).

```bash
uv run --env-file .env alembic upgrade head
```

```bash
uv run --env-file .env uvicorn --factory coverage_check.api:create_app
```

In two more shells (or more: the limit is shared), run a worker:

```bash
uv run --env-file .env coverage-check worker
```

Then write a sample batch (one check per member; `--count 3000` goes round them again):

```bash
uv run coverage-check seed --out batch.json
```

Submit it. Sending it again with the same key returns the same batch and creates nothing:

```bash
(set -a; . ./.env; curl -i http://127.0.0.1:8000/batches -H "Authorization: Bearer $COVERAGE_CHECK_API_TOKEN" -H "Idempotency-Key: demo-1" -H "Content-Type: application/json" --data @batch.json)
```

| Endpoint | Gives |
|---|---|
| `POST /batches` | 201 for a new batch, 200 for the same key and body, 409 for the same key with another body |
| `GET /batches/{id}` | counts by state and outcome, and each check's id |
| `GET /checks/{id}` | one check, with its FHIR `CoverageEligibilityResponse` once final |
| `GET /exceptions` | checks a person has to look at, oldest first |

Every endpoint needs the bearer token. Errors are RFC 9457 problem details.

When the batch is done, check the invariants against the database and the payer's own log, using the batch id from the submit reply:

```bash
uv run --env-file .env coverage-check report <batch-id>
```

It prints PASS or FAIL for each of the six and exits non-zero on a failure. Worker settings and their defaults are in `src/coverage_check/config.py`; set `WORKER_<NAME>` to change one. The limiter's settings and the mock's profile have to agree ([ADR 0010](docs/adr/0010-one-payer-key-per-check.md)).

## Try the mock payer

Ask it about the first member:

```bash
uv run coverage-check seed --count 1 | python3 -c 'import json, sys; print(json.dumps(json.load(sys.stdin)["checks"][0]))' | curl -i http://127.0.0.1:8001/eligibility -H "Idempotency-Key: try-1" --data @-
```

See everything it has received:

```bash
curl http://127.0.0.1:8001/requests
```

By default it fails now and then. `MOCK_PAYER_PROFILE=calm docker compose up -d --wait` turns the faults off. Protocol, profile and sources: [docs/mock-payer.md](docs/mock-payer.md).

## Test it

```bash
uv run pytest
```

The tests start their own Postgres with Testcontainers. With Docker Desktop on a Mac, if the container fails to start with a `docker.sock` mount error, use:

```bash
TESTCONTAINERS_DOCKER_SOCKET_OVERRIDE=/var/run/docker.sock uv run pytest
```

Lint and types: `uv run ruff check`, `uv run ruff format --check`, `uv run mypy`.
