"""Is the system really being observed? Run on the host, through port-forwards (observed.sh).

Both targets scraped, every dashboard query with data, no alert firing after an ordinary
batch, and in Jaeger the trace of one check that was retried. Standard library only.
"""

from __future__ import annotations

import json
import sys
import time
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import urlopen

DASHBOARD = Path(__file__).parents[2] / "deploy/base/observability/grafana/coverage-check.json"
WAIT_SECONDS = 90


def get(url: str) -> Any:
    with urlopen(url, timeout=30) as response:  # noqa: S310 - a port-forward on this machine
        return json.load(response)


def problems(prometheus: str) -> list[str]:
    found = []
    targets = get(f"{prometheus}/api/v1/targets")["data"]["activeTargets"]
    up = sorted(t["labels"]["job"] for t in targets if t["health"] == "up")
    if up != ["engine", "payer"]:
        found.append(f"targets up: {up}")
    now = time.time()
    for panel in json.loads(DASHBOARD.read_text())["panels"]:
        if panel["title"] == "Alerts firing":
            continue
        for target in panel["targets"]:
            query = urlencode({"query": target["expr"], "start": now - 900, "end": now, "step": 5})
            if not get(f"{prometheus}/api/v1/query_range?{query}")["data"]["result"]:
                found.append(f"no data for {panel['title']}: {target['expr']}")
    firing = [
        a["labels"]["alertname"]
        for a in get(f"{prometheus}/api/v1/alerts")["data"]["alerts"]
        if a["state"] == "firing"
    ]
    if firing:
        found.append(f"alerts firing: {firing}")
    return found


def retried_trace(jaeger: str) -> list[str] | None:
    """The span names of one check that took a second attempt, from submit on."""
    now = datetime.now(UTC)
    query = urlencode(
        {
            "query.service_name": "coverage-check-worker",
            "query.start_time_min": (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "query.start_time_max": (now + timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "query.attributes": json.dumps({"attempt.number": "2"}),
            "query.search_depth": 20,
        }
    )
    traces: dict[str, list[str]] = defaultdict(list)
    for resource in get(f"{jaeger}/api/v3/traces?{query}")["result"].get("resourceSpans", []):
        for scope in resource["scopeSpans"]:
            for span in scope["spans"]:
                traces[span["traceId"]].append(span["name"])
    for names in traces.values():
        if (
            "check.submitted" in names
            and names.count("check.attempt") >= 2
            and names.count("payer.call") >= 2
        ):
            return sorted(names)
    return None


def main(prometheus: str, jaeger: str) -> None:
    # Scrapes and span export lag a few seconds behind the batch.
    deadline = time.monotonic() + WAIT_SECONDS
    while True:
        try:
            found = problems(prometheus)
            trace = retried_trace(jaeger)
            if trace is None:
                found.append("no trace of a retried check, from submit through both attempts")
        except OSError as error:
            found, trace = [f"could not ask: {error}"], None
        if not found:
            break
        if time.monotonic() > deadline:
            sys.exit("\n".join(found))
        time.sleep(5)
    print("targets up, every dashboard query has data, no alert firing")
    print(f"one retried check's trace: {', '.join(trace or [])}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
