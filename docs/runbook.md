# Runbook

What each alert means, how to check, and what to do. The rules are in [alerts.yml](../deploy/base/observability/alerts.yml); why the numbers come from where they do is [ADR 0013](adr/0013-where-telemetry-comes-from.md). There is no Alertmanager: an alert shows as firing in Prometheus and on the dashboard, and nothing is sent anywhere.

This is the local kind cluster. Nothing here touches any other.

## Where to look first

```bash
make dashboard
```

Dashboard on <http://127.0.0.1:3000>, alerts on <http://127.0.0.1:9090/alerts>. Ctrl-C stops it.

```bash
make traces
```

Jaeger on <http://127.0.0.1:16686>. Search service `coverage-check-worker` with a tag such as `check.id=<id>` or `attempt.number=2`.

The commands below use this, from the repo's root, so that they can only reach this cluster:

```bash
k() { .cache/bin/kubectl-v1.37.3 --kubeconfig kubeconfig-coverage-check --context kind-coverage-check --namespace coverage-check "$@"; }
sql() { k exec statefulset/postgres -- psql -U coverage_check -d coverage_check -c "$1"; }
```

Worker logs are one JSON object per line with `batch_id`, `check_id`, `attempt_id` and `trace_id`:

```bash
k logs -l app.kubernetes.io/name=worker --tail=100 --prefix
```

## PayerRateNearLimit

**What it means.** For a minute the payer has had more than 85% of its limit. The limiter is set to 80% (16 of 20), so something differs from the design. Nothing is broken yet. The margin is thin: with 16 a second set, the payer has seen 19 in one second.

**How to check.** The first dashboard panel. Compare `limiter setting` with `payer limit`.

```bash
sql "SELECT payer_api, rate_per_second, burst, max_in_flight FROM limiter_buckets"
```

- The payer's limit went down: the setting no longer fits it.
- The limiter's rate went up: someone changed `worker.env`.
- Neither: something besides the workers is calling the payer on this account. The NetworkPolicy should make that impossible.

**What to do.** Lower `WORKER_RATE_PER_SECOND` in `deploy/base/worker.env` so that burst plus rate is well under the payer's limit, in a pull request. Changing a limit is a decision ([ADR 0010](adr/0010-one-payer-key-per-check.md)).

## PayerOverLimit

**What it means.** The payer refused a request for being over its rate limit or in-flight cap. **Invariant 1 is broken.** A real payer might now throttle or suspend the account. The alert stays up for five minutes after the last refusal.

**How to check.** The dashboard's first two panels, for which limit was crossed. In Prometheus:

```
increase(mock_payer_requests_total{behaviour=~"rate_limit|in_flight_limit"}[15m])
```

Then the invariant report for the batch that was running, which reads the payer's own log:

```bash
make report BATCH=<batch id>
```

**What to do.** Stop the traffic first: `k scale deployment/worker --replicas=0`. Checks stay queued and nothing is lost. Then find out why: a payer limit lower than configured (`curl` its `/limits` from a worker), a changed `worker.env`, or a bug in the limiter. Start again with `k scale deployment/worker --replicas=2`.

## ExceptionsRising

**What it means.** More than 20 checks in two minutes became exceptions for reasons that aren't about the member: usually `payer_unavailable` or `malformed_response`. "Member not found" and "several matches" don't count.

**It can be late.** Retries wait behind checks that haven't been tried yet, so during a large batch a failing payer shows in the attempts and retries panels well before it shows here.

**How to check.**

```bash
sql "SELECT exception_reason, count(*) FROM checks WHERE state = 'exception' AND updated_at > now() - interval '10 minutes' GROUP BY 1"
sql "SELECT outcome_class, http_status, count(*) FROM attempts WHERE started_at > now() - interval '10 minutes' GROUP BY 1, 2 ORDER BY 3 DESC"
```

**What to do.** If the payer is down, wait for it, then requeue the exceptions (`POST /exceptions/{check id}/requeue`), which asks afresh under a new key. If replies are malformed or `unexpected_status`, the bodies are kept in `attempts.response_body`: the payer's protocol changed, and that needs code.

## LeaseStuck

**What it means.** A check has been leased for more than 135 seconds, three lease lengths. A lease that runs out is taken back by the next claim, so an old one means **nothing is claiming**. Invariant 3 is at risk until a worker does.

**How to check.**

```bash
k get pods -l app.kubernetes.io/name=worker
sql "SELECT count(*), min(updated_at), max(lease_expires_at) FROM checks WHERE state = 'leased'"
```

- No workers, or none ready: look at `k describe` and the logs.
- Workers ready but not claiming: look for `database_unavailable` or `worker_error` in their logs.

**What to do.** Get a worker running (`k scale deployment/worker --replicas=2`, or `k rollout restart deployment/worker`). Don't edit lease columns by hand: the next claim puts expired leases back by itself.

## TelemetryMissing

**What it means.** For a minute Prometheus couldn't scrape the engine's or the payer's numbers, or the metrics process couldn't read the database. **The other four alerts are blind meanwhile.** The `job` label says which side.

**How to check.**

```bash
k get pods -l 'app.kubernetes.io/name in (metrics, mock-payer, postgres, prometheus)'
k logs deployment/metrics --tail=20
```

`database_unavailable` there means Postgres, not the metrics process.

**What to do.** Fix what's down. Afterwards, run the invariant report for any batch that ran in the gap: it reads the database and the payer's log, not Prometheus.

## Firing each alert on purpose

```bash
make alerts
```

Takes about ten minutes. It changes no manifest: the mock payer's test controls and replica counts do it, and are put back when the script ends, even if it fails. Its batches break the invariants on purpose, so don't read their reports as evidence. CI runs it on every pull request, after the end-to-end and chaos runs; the log shows how long each alert took.

| Alert | How it is forced |
|---|---|
| TelemetryMissing | the metrics Deployment scaled to 0 |
| PayerRateNearLimit | a payer with no faults and a limit of 17, with a batch of 3,000 |
| PayerOverLimit | the payer's limit lowered to 8 during that batch |
| ExceptionsRising | a payer that fails every request, with a batch of 150 |
| LeaseStuck | every worker force-deleted during slow calls, then scaled to 0 |
