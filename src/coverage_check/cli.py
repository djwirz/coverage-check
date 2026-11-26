"""The coverage-check command."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from coverage_check.seed import sample_batch


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="coverage-check")
    commands = parser.add_subparsers(dest="command", required=True)
    seed = commands.add_parser("seed", help="write a sample batch from the synthetic members")
    seed.add_argument("--count", type=int, help="number of checks (default: one per member)")
    seed.add_argument("--out", type=Path, help="file to write (default: standard output)")
    args = parser.parse_args(argv)

    try:
        batch = sample_batch(args.count)
    except ValueError as error:
        parser.error(str(error))
    text = batch.model_dump_json(indent=2) + "\n"
    if args.out is None:
        sys.stdout.write(text)
    else:
        args.out.write_text(text)
        print(f"Wrote {len(batch.checks)} checks to {args.out}", file=sys.stderr)
    return 0
