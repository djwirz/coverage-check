"""Submit a sample batch from inside an API pod, and print its id.

Piped to `python -` in the pod. Arguments: number of checks, idempotency key, and optionally
how many seconds to wait for the batch to finish.
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any
from urllib.request import Request, urlopen

from coverage_check.seed import sample_batch

API = "http://127.0.0.1:8000"


def call(path: str, data: bytes | None = None, headers: dict[str, str] | None = None) -> Any:
    token = os.environ["COVERAGE_CHECK_API_TOKEN"]
    request = Request(  # noqa: S310 - a fixed http URL in this pod
        API + path,
        data=data,
        headers={"Authorization": f"Bearer {token}", **(headers or {})},
    )
    with urlopen(request, timeout=60) as response:  # noqa: S310
        return json.load(response)


def main(count: int, key: str, wait_seconds: float = 0) -> None:
    started = time.monotonic()
    batch = call(
        "/batches",
        sample_batch(count).model_dump_json().encode(),
        {"Idempotency-Key": key, "Content-Type": "application/json"},
    )
    deadline = time.monotonic() + wait_seconds
    while wait_seconds and not batch["done"]:
        if time.monotonic() > deadline:
            sys.exit(f"not done after {wait_seconds:.0f} s: {batch['states']}")
        time.sleep(2)
        batch = call(f"/batches/{batch['id']}")
    if batch["done"]:
        print(batch["states"], batch["outcomes"], file=sys.stderr)
        if wait_seconds:
            took = time.monotonic() - started
            print(f"{count} checks finished in {took:.0f} s", file=sys.stderr)
    print(batch["id"])


if __name__ == "__main__":
    main(int(sys.argv[1]), sys.argv[2], float(sys.argv[3]) if len(sys.argv) > 3 else 0)
