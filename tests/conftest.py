"""A real Postgres from Testcontainers, migrated once and emptied between tests."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from starlette.testclient import TestClient
from testcontainers.community.postgres import PostgresContainer

from coverage_check.api import create_app
from coverage_check.config import Settings

ROOT = Path(__file__).parent.parent
TOKEN = "test-token"  # noqa: S105 - only ever used by these tests


def migrate(url: str, revision: str = "head") -> None:
    before = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url
    try:
        config = Config(ROOT / "alembic.ini")
        if revision == "base":
            command.downgrade(config, revision)
        else:
            command.upgrade(config, revision)
    finally:
        if before is None:
            del os.environ["DATABASE_URL"]
        else:
            os.environ["DATABASE_URL"] = before


@pytest.fixture(scope="session")
def postgres_url() -> Iterator[str]:
    # The same major version as compose.yaml.
    with PostgresContainer("postgres:18", driver=None) as postgres:
        yield postgres.get_connection_url()


@pytest.fixture(scope="session")
def migrated_url(postgres_url: str) -> str:
    migrate(postgres_url)
    return postgres_url


@pytest.fixture
def database_url(migrated_url: str) -> str:
    with psycopg.connect(migrated_url) as conn:
        conn.execute("TRUNCATE batches, checks, attempts, limiter_buckets, limiter_slots")
    return migrated_url


@pytest.fixture
def db(database_url: str) -> Iterator[psycopg.Connection[Any]]:
    with psycopg.connect(database_url, autocommit=True) as conn:
        yield conn


@pytest.fixture
def client(database_url: str) -> Iterator[TestClient]:
    app = create_app(Settings(database_url=database_url, api_token=TOKEN))
    with TestClient(app, headers={"Authorization": f"Bearer {TOKEN}"}) as client:
        yield client
