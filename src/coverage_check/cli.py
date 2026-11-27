"""The coverage-check command."""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
import threading
from collections.abc import Sequence
from pathlib import Path
from uuid import UUID

from prometheus_client import start_http_server

from coverage_check import logs, metrics, report, tracing, worker
from coverage_check.config import WorkerSettings, require_env, worker_settings_from_env
from coverage_check.seed import sample_batch


async def _work(settings: WorkerSettings) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for name in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(name, stop.set)
    tracing.configure("worker")
    try:
        await worker.run(settings, stop)
    finally:
        tracing.shutdown()


def _serve_metrics(database_url: str, port: int) -> None:
    stop = threading.Event()
    for name in (signal.SIGTERM, signal.SIGINT):
        signal.signal(name, lambda *_: stop.set())
    server, _ = start_http_server(port, registry=metrics.registry(database_url))
    stop.wait()
    server.shutdown()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="coverage-check")
    commands = parser.add_subparsers(dest="command", required=True)
    seed = commands.add_parser("seed", help="write a sample batch from the synthetic members")
    seed.add_argument("--count", type=int, help="number of checks (default: one per member)")
    seed.add_argument("--out", type=Path, help="file to write (default: standard output)")
    commands.add_parser("worker", help="run a worker until SIGTERM or SIGINT")
    measuring = commands.add_parser("metrics", help="serve Prometheus metrics from the database")
    measuring.add_argument("--port", type=int, default=9000)
    reporting = commands.add_parser("report", help="check the invariants for a finished batch")
    reporting.add_argument("batch", type=UUID)
    args = parser.parse_args(argv)

    if args.command == "worker":
        logs.configure()
        asyncio.run(_work(worker_settings_from_env()))
        return 0
    if args.command == "metrics":
        logs.configure()
        _serve_metrics(require_env("DATABASE_URL"), args.port)
        return 0
    if args.command == "report":
        settings = worker_settings_from_env()
        try:
            findings = report.build(
                settings.database_url, settings.payer_url, args.batch, settings.max_attempts
            )
        except LookupError as error:
            parser.error(str(error))
        sys.stdout.write(report.render(findings))
        return 0 if all(f.passed for f in findings) else 1

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
