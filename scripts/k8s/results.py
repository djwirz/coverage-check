"""Print a batch's stored FHIR results, one per line. Piped to `python -` in an API pod."""

from __future__ import annotations

import json
import os
import sys

import psycopg


def main(batch_id: str) -> None:
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        for (result,) in conn.execute(
            "SELECT result FROM checks WHERE batch_id = %s ORDER BY position", (batch_id,)
        ):
            print(json.dumps(result))


if __name__ == "__main__":
    main(sys.argv[1])
