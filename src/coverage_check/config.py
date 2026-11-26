"""Settings from the environment. Secrets are never defaulted."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Settings:
    database_url: str = field(repr=False)
    api_token: str = field(repr=False)


def require_env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise RuntimeError(f"{name} is not set (see .env.example)")
    return value


def settings_from_env() -> Settings:
    return Settings(
        database_url=require_env("DATABASE_URL"),
        api_token=require_env("COVERAGE_CHECK_API_TOKEN"),
    )
