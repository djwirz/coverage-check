# 13. Where telemetry comes from, and where traces are kept

Date: 2026-11-26
Status: Accepted

## Context
Spec §7 lists metrics, one trace per check, a dashboard and alerts, with Prometheus, Grafana and the OpenTelemetry collector in the cluster. Left open: who counts what, how a trace joins up across processes and retries, and where traces are stored (the spec names no store). Two facts shape it. The chaos run kills workers, and a counter inside a killed process loses whatever wasn't scraped yet. And the alert that matters most, "any one-second window over the limit", can't be seen by scraping a counter every few seconds.

## Options considered
Engine metrics:
1. **Read from the database** by one small process, on each scrape.
2. **Counters in each worker**, scraped from every pod. Needs an HTTP port on workers, Prometheus access to the Kubernetes API to find them, and loses counts on every kill.
3. **Pushed from workers** through the collector. No port, but the same loss.

The payer's side:
1. **The mock payer counts**, and serves its numbers on a port of their own.
2. **The workers report** what they sent and what was refused. A worker can't tell a random 429 from an over-limit one, and it isn't an independent witness.

Trace store:
1. **Jaeger v2**, all in one, in memory, with its own UI.
2. **Grafana Tempo**, read in Grafana. More configuration.
3. **None:** the collector logs spans. Nothing to look a check up in.

## Decision
- **Engine metrics: option 1.** `coverage-check metrics` reads one snapshot of the database per scrape. Workers get no port and no metric code.
- **Payer metrics: option 1.** The mock serves them on port 9100, which only Prometheus may reach; it still can't reach `/eligibility`. It also keeps the most requests it saw in any one second lately, and counts refusals as they happen, so "over the limit" can't fall between scrapes.
- **Traces:** OpenTelemetry in the API and workers, sent to the collector, kept in **Jaeger** and read in Jaeger's UI. Each check stores a `traceparent` at submit (migration 0003), so every claim, attempt, retry and requeue continues one trace.
- **Grafana shows metrics only.** Jaeger 2.21 removed the old HTTP API, and Grafana 13.3's Jaeger data source got a 404 from it. The ways round were an old Jaeger or an experimental Grafana flag that doesn't cover search.
- **Alerts are Prometheus rules with no Alertmanager.** On a local cluster there is nobody to notify; an alert shows as firing in Prometheus and on the dashboard.

## Why
The database already records every claim, attempt and outcome in the same transaction as the work, so its numbers survive any pod and can't disagree with the invariant report. The payer is the one place that knows what reached it. A trace store and UI in one container is the least that makes one check's story readable.

## Consequences
- Metrics are only as detailed as the tables. Something the database doesn't record, such as time spent waiting for the limiter, has no metric.
- Each scrape scans the attempts and checks tables. Fine at thousands of rows; a long-running system would need running totals or a cut-off.
- The counters are derived, so they can only be trusted to rise while rows aren't deleted. A requeued exception leaves the final counts and the time-to-final histogram until it finishes again.
- A worker killed outright never sends the span of the attempt it was on. The trace shows the next attempt, with a gap in the attempt numbers; the database still has the row.
- Nothing is kept: Prometheus history and traces go when their pods do.
- If the collector is away, spans are dropped and checks carry on.
- A real payer would give no metrics of its own. "Over the limit" would then come from the 429s the workers receive.
- One more database user would be better than sharing the service's, which can write. The metrics process opens its transaction read-only, which is a habit, not a permission.
- Sending Grafana to Jaeger, or alerts to a person, is a later decision.
