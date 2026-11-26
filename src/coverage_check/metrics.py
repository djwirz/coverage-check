"""What the database knows, as Prometheus metrics. Run with: coverage-check metrics

Read on each scrape, and not counted in the workers: a killed worker takes its counts with
it, and the database has already recorded every claim and attempt (ADR 0013).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

import psycopg
from prometheus_client import CollectorRegistry
from prometheus_client.core import (
    CounterMetricFamily,
    GaugeMetricFamily,
    HistogramMetricFamily,
    Metric,
)
from prometheus_client.registry import Collector

log = logging.getLogger("coverage_check.metrics")
CONNECT_TIMEOUT_SECONDS = 3
STATES = ("queued", "leased", "done", "exception")
SECONDS_TO_FINAL = (1, 5, 10, 30, 60, 120, 300, 600, 1800)

# An attempt whose lease ran out is first marked lease_expired, and renamed to late:<class> if
# its reply arrives after all. Counting both as one thing keeps the counters from going down.
_EXPIRED = "(outcome_class = 'lease_expired' OR outcome_class LIKE 'late:%')"


def _read(conn: psycopg.Connection[Any]) -> Iterable[Metric]:
    states = dict(conn.execute("SELECT state, count(*) FROM checks GROUP BY state").fetchall())
    checks = GaugeMetricFamily("coverage_check_checks", "Checks by state.", labels=["state"])
    for state in STATES:
        checks.add_metric([state], states.get(state, 0))
    yield checks

    final = GaugeMetricFamily(
        "coverage_check_final_checks",
        "Final checks by result, and for exceptions the reason.",
        labels=["result", "reason"],
    )
    for result, reason, count in conn.execute(
        "SELECT coalesce(outcome, 'exception'), coalesce(exception_reason, ''), count(*)"
        " FROM checks WHERE state IN ('done', 'exception') GROUP BY 1, 2"
    ):
        final.add_metric([result, reason], count)
    yield final

    slots = GaugeMetricFamily(
        "coverage_check_payer_slots_in_use", "Payer calls in flight.", labels=["payer_api"]
    )
    cap = GaugeMetricFamily(
        "coverage_check_payer_slots_cap", "The engine's in-flight cap.", labels=["payer_api"]
    )
    rate = GaugeMetricFamily(
        "coverage_check_limiter_rate_per_second", "The limiter's rate.", labels=["payer_api"]
    )
    for api, per_second, allowed, used in conn.execute(
        "SELECT b.payer_api, b.rate_per_second, b.max_in_flight,"
        " (SELECT count(*) FROM limiter_slots s WHERE s.payer_api = b.payer_api"
        "   AND s.expires_at > clock_timestamp())"
        " FROM limiter_buckets b"
    ):
        rate.add_metric([api], float(per_second))
        cap.add_metric([api], allowed)
        slots.add_metric([api], used)
    yield from (slots, cap, rate)

    attempts = CounterMetricFamily(
        "coverage_check_attempts", "Finished attempts by outcome.", labels=["outcome"]
    )
    for outcome, count in conn.execute(
        "SELECT regexp_replace(outcome_class, '^late:', ''), count(*) FROM attempts"
        " WHERE outcome_class <> 'lease_expired' GROUP BY 1"
    ):
        attempts.add_metric([outcome], count)
    yield attempts

    retries = CounterMetricFamily(
        "coverage_check_retries",
        "Attempts that repeat an earlier one, by why.",
        labels=["reason"],
    )
    for reason, count in conn.execute(
        "SELECT CASE WHEN p.outcome_class = 'lease_expired' OR p.outcome_class LIKE 'late:%'"
        "   THEN 'lease_expired' ELSE p.outcome_class END, count(*)"
        " FROM attempts a JOIN attempts p ON p.check_id = a.check_id"
        "   AND p.number = a.number - 1 AND p.cycle = a.cycle"
        " WHERE p.outcome_class IS NOT NULL GROUP BY 1"
    ):
        retries.add_metric([reason], count)
    yield retries

    row = conn.execute(
        f"SELECT count(*) FILTER (WHERE {_EXPIRED}),"  # noqa: S608 - a constant
        " count(*) FILTER (WHERE outcome_class LIKE 'late:%') FROM attempts"
    ).fetchone()
    assert row is not None  # noqa: S101 - aggregates always return a row
    yield CounterMetricFamily(
        "coverage_check_lease_expiries", "Attempts whose lease ran out.", value=row[0]
    )
    yield CounterMetricFamily(
        "coverage_check_late_replies", "Replies that came after the lease was lost.", value=row[1]
    )

    # A claim sets updated_at, and nothing else touches a leased check.
    row = conn.execute(
        "SELECT coalesce(extract(epoch FROM clock_timestamp() - min(updated_at)), 0)"
        " FROM checks WHERE state = 'leased'"
    ).fetchone()
    assert row is not None  # noqa: S101 - aggregates always return a row
    yield GaugeMetricFamily(
        "coverage_check_oldest_lease_age_seconds",
        "How long the longest-held lease has been held. 0 when nothing is leased.",
        value=float(row[0]),
    )

    buckets = ", ".join(f"count(*) FILTER (WHERE seconds <= {le})" for le in SECONDS_TO_FINAL)
    row = conn.execute(
        f"SELECT {buckets}, count(*), coalesce(sum(seconds), 0) FROM ("  # noqa: S608 - constants
        " SELECT extract(epoch FROM updated_at - created_at) AS seconds FROM checks"
        " WHERE state IN ('done', 'exception')) AS final"
    ).fetchone()
    assert row is not None  # noqa: S101 - aggregates always return a row
    *counts, total, seconds = row
    # A requeued check leaves this and comes back later, so it can briefly go down.
    yield HistogramMetricFamily(
        "coverage_check_seconds_to_final",
        "Seconds from submit to final state.",
        buckets=[
            *((str(float(le)), n) for le, n in zip(SECONDS_TO_FINAL, counts, strict=True)),
            ("+Inf", total),
        ],
        sum_value=float(seconds),
    )


class DatabaseCollector(Collector):
    def __init__(self, database_url: str) -> None:
        self._url = database_url

    def describe(self) -> Iterable[Metric]:
        return []  # so registering doesn't query the database

    def collect(self) -> Iterable[Metric]:
        try:
            with psycopg.connect(self._url, connect_timeout=CONNECT_TIMEOUT_SECONDS) as conn:
                # One snapshot, so the numbers agree with each other.
                conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
                conn.read_only = True
                found = list(_read(conn))
        except psycopg.Error as error:
            log.warning("database_unavailable", extra={"error": type(error).__name__})
            found = []
        up = GaugeMetricFamily("coverage_check_database_up", "Whether the last read worked.")
        up.add_metric([], 1 if found else 0)
        yield up
        yield from found


def registry(database_url: str) -> CollectorRegistry:
    new = CollectorRegistry()
    new.register(DatabaseCollector(database_url))
    return new
