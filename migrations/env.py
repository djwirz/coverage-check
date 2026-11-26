"""Alembic environment. Online migrations only, against DATABASE_URL."""

from alembic import context
from sqlalchemy import create_engine, make_url, pool

from coverage_check.config import require_env

url = make_url(require_env("DATABASE_URL")).set(drivername="postgresql+psycopg")
engine = create_engine(url, poolclass=pool.NullPool)

with engine.connect() as connection:
    context.configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()
