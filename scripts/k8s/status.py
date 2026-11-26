"""Print how many of a batch's checks are final, and whether it is done. Piped to an API pod."""

from __future__ import annotations

import json
import os
import sys
from urllib.request import Request, urlopen


def main(batch_id: str) -> None:
    token = os.environ["COVERAGE_CHECK_API_TOKEN"]
    request = Request(
        f"http://127.0.0.1:8000/batches/{batch_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urlopen(request, timeout=30) as response:  # noqa: S310
        batch = json.load(response)
    states = batch["states"]
    print(states["done"] + states["exception"], "done" if batch["done"] else "running")


if __name__ == "__main__":
    main(sys.argv[1])
