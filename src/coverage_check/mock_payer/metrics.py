"""What the payer saw, for Prometheus. Served on its own port (ADR 0013).

A scrape every few seconds can't see a one-second window, so the payer, which counts them
anyway, keeps the largest it saw lately.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable, Iterable
from typing import get_args

from prometheus_client import CollectorRegistry, Counter
from prometheus_client.core import GaugeMetricFamily, Metric
from prometheus_client.registry import Collector

from coverage_check.mock_payer.profile import Fault

# Requests turned away at the door: the caller was over a limit (invariant 1).
REFUSALS = ("rate_limit", "in_flight_limit")
BEHAVIOURS = (
    *REFUSALS,
    *get_args(Fault),
    "invalid_request",
    "in_progress",
    "key_reused",
    "replay",
    "aborted",
)
PEAK_MEMORY_SECONDS = 15


class PayerMetrics(Collector):
    def __init__(
        self, state: Callable[[], tuple[int, int, int]], clock: Callable[[], float]
    ) -> None:
        """`state` gives the requests in flight, the rate limit and the in-flight cap."""
        self._state = state
        self._clock = clock
        self._windows: deque[tuple[float, int]] = deque()
        # Scrapes are served from another thread.
        self._lock = threading.Lock()
        self.registry = CollectorRegistry()
        self.requests = Counter(
            "mock_payer_requests",
            "Requests by what the payer did with them.",
            ["behaviour"],
            registry=self.registry,
        )
        # At zero from the start, or the first refusal would not show as an increase.
        for behaviour in BEHAVIOURS:
            self.requests.labels(behaviour)
        self.registry.register(self)

    def window(self, now: float, requests_in_last_second: int) -> None:
        with self._lock:
            self._windows.append((now, requests_in_last_second))
            self._forget(now)

    def _forget(self, now: float) -> None:
        while self._windows and self._windows[0][0] <= now - PEAK_MEMORY_SECONDS:
            self._windows.popleft()

    def describe(self) -> Iterable[Metric]:
        return []

    def collect(self) -> Iterable[Metric]:
        with self._lock:
            self._forget(self._clock())
            peak = max((count for _, count in self._windows), default=0)
        in_flight, rate_limit, max_in_flight = self._state()
        for name, text, value in (
            ("mock_payer_in_flight", "Requests being worked on.", in_flight),
            ("mock_payer_rate_limit_per_second", "The payer's rate limit.", rate_limit),
            ("mock_payer_max_in_flight", "The payer's in-flight cap.", max_in_flight),
            (
                "mock_payer_peak_requests_in_one_second",
                f"The most in any one second over the last {PEAK_MEMORY_SECONDS} seconds.",
                peak,
            ),
        ):
            yield GaugeMetricFamily(name, text, value=value)
