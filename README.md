# Coverage Check

Checks insurance coverage in bulk against a payer API that's slow, rate-limited and often fails. Every worker shares one rate limit, so the payer's limit is never exceeded, and every patient ends in exactly one honest state: covered, not covered, or an exception a person can work through. Results are FHIR R4. It runs on Kubernetes and keeps those promises while its pods are killed and scaled mid-batch.

**Status:** early. Slices 1 to 5 of [the plan](docs/spec.md#11-proposed-slice-plan-one-pr-each-after-this-spec) are built: batches go in, workers check them against a [mock payer](docs/mock-payer.md) that misbehaves on demand, and every check ends covered, not covered or as an exception, with a FHIR result. It runs on a local Kubernetes cluster and keeps its promises there while pods are killed ([below](#under-chaos)); still to come are observability and a demo.

On a laptop, 3,000 checks with two workers took about four minutes and passed every line of the invariant report. The pace is set by the limiter (16 a second against the payer's 20), and dips when hanging calls fill the in-flight cap. Design: [docs/spec.md](docs/spec.md) and [docs/adr/](docs/adr/).

**Data:** synthetic only. Members come from [synthetic-cohort](https://github.com/djwirz/synthetic-cohort). Some insurer names are real companies', from Synthea's built-in list; the people, member ids and coverage are made up. There is no real payer: this project will only call its own mock.

## Run it on Kubernetes

Needs Docker, make, and **at least 8 GB of free disk**. kind and kubectl are fetched into `.cache/`, pinned and checksum-verified; your own kubectl and `~/.kube/config` are not touched.

```bash
make up
```

That creates a cluster (one control plane, two workers), builds and loads the image, writes random secrets to `.env` if there is none, deploys, and submits a sample batch.

```bash
make e2e
```

Submits 200 checks, waits, prints the invariant report and validates the stored results with HL7's validator (needs Java or a Java container).

```bash
make down
```

| In the cluster | What |
|---|---|
| `api` (2), `worker` (2) | the service; scale workers freely, the limit is shared |
| `postgres` (1) | the queue and results, on its own volume |
| `mock-payer` (1) | the payer and its request log, on its own volume |
| `migrate` | a job that creates the schema |

Manifests are in `deploy/`, with `local` and `ci` overlays. Changing the database password in `.env` needs `make down` first.

```bash
make chaos
```

Disturbs a running batch and checks the invariants (next section).

## Under chaos

`make chaos` submits a batch and, as it progresses: kills a worker pod outright and deletes another; scales workers to 10, to 1 and back to 3; deletes the Postgres pod; makes the payer fail half its requests for a while; and tries to evict both API pods (the second eviction is refused). Then it runs the invariant report, and checks that leases really expired and the payer really was degraded.

**Measured in GitHub Actions**, 3,000 checks, six runs each, on 2026-11-26:

| | Time | Payer requests a second | Invariants |
|---|---|---|---|
| Undisturbed | about four minutes | 13 to 14 | all six passed, every run |
| Under chaos | four and a half to five minutes | 12 to 13 | all six passed, every run |

The most the payer saw in any second was 17 to 19, against its limit of 20, and never more than 8 requests in flight against 10. No check was lost, none finished twice and none was billed twice.

**Against what the limit allows:** the limiter is set to 16 a second, below the payer's 20, to leave room for network jitter. An undisturbed batch took roughly a sixth to a fifth longer than 16 a second would. The likely reasons are calls that hang filling the in-flight cap, and the batch waiting on the last of them; an earlier run on a laptop showed both, but these runs weren't broken down.

**The margin is thinner than it looks.** The limiter never lets more than 18 requests start in a second, yet the payer has seen 19: requests that set off apart can arrive closer together. On a network with more delay than one machine's, the setting would need to come down.

These are shared CI machines and a mock payer whose failure rates are our own choice, so read the figures as the shape, not a benchmark.

## Run it with Compose

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
| `POST /exceptions/{check id}/requeue` | sends one round again; 409 if it isn't an exception |

Every endpoint needs the bearer token. Errors are RFC 9457 problem details; 503 means the database is unreachable.

Stop a worker with Ctrl-C or SIGTERM: it finishes the calls it has in flight (up to 40 s), then exits. A killed worker's checks are picked up when their leases expire.

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

Check the FHIR results with HL7's official validator (needs Java or Docker; the first run downloads about 200 MB into `.cache/`):

```bash
./scripts/validate_fhir.sh
```

It validates examples made by the real mapping code against R4, and makes sure a broken one is refused. It runs without a terminology server, so codes from outside the R4 packages aren't checked, and it warns about this project's own code systems and the missing narrative.
