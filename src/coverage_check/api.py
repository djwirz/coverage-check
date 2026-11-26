"""The batch API (spec §4). Run with: uvicorn --factory coverage_check.api:create_app"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from coverage_check import batches, problems
from coverage_check.config import Settings, settings_from_env
from coverage_check.models import Batch, BatchRequest, Check, Exceptions

bearer = HTTPBearer(auto_error=False)
DATABASE_WAIT_SECONDS = 5
READY_WAIT_SECONDS = 2


def require_token(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> None:
    expected: str = request.app.state.settings.api_token
    if credentials is None or not secrets.compare_digest(
        credentials.credentials.encode(), expected.encode()
    ):
        raise HTTPException(
            401, "A valid bearer token is required.", {"WWW-Authenticate": "Bearer"}
        )


async def connection(request: Request) -> AsyncIterator[AsyncConnection[Any]]:
    async with request.app.state.pool.connection() as conn:
        yield conn


# "function" scope commits and returns the connection before the response is sent.
Connection = Annotated[AsyncConnection[Any], Depends(connection, scope="function")]
IdempotencyKey = Annotated[
    str, Header(alias="Idempotency-Key", min_length=1, max_length=255, pattern=r"^[\x20-\x7e]+$")
]


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or settings_from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with AsyncConnectionPool(
            settings.database_url,
            open=False,
            timeout=DATABASE_WAIT_SECONDS,
            # Replace connections that a restarted database has dropped.
            check=AsyncConnectionPool.check_connection,
        ) as pool:
            await pool.wait()
            app.state.pool = pool
            yield

    # No docs pages: they would be the only routes without a token.
    app = FastAPI(
        title="coverage-check",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings
    problems.install(app)
    # Everything that touches data is on this router, behind the token.
    protected = APIRouter(dependencies=[Depends(require_token)])

    # For Kubernetes probes: no token, and nothing to learn from them.
    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz(request: Request) -> dict[str, str]:
        # A database that can't be reached becomes a 503 (problems.py).
        async with request.app.state.pool.connection(timeout=READY_WAIT_SECONDS) as conn:
            await conn.execute("SELECT 1")
        return {"status": "ok"}

    @protected.post(
        "/batches",
        status_code=201,
        responses={200: {"description": "Same key and body"}, 409: {"description": "Key reused"}},
    )
    async def submit(
        body: BatchRequest, key: IdempotencyKey, conn: Connection, response: Response
    ) -> Batch:
        try:
            batch_id, created = await batches.submit_batch(conn, key, body)
        except batches.IdempotencyConflictError:
            raise HTTPException(
                409, "This Idempotency-Key was already used for a different batch."
            ) from None
        batch = await batches.get_batch(conn, batch_id)
        assert batch is not None  # noqa: S101 - just written or found
        response.status_code = 201 if created else 200
        response.headers["Location"] = f"/batches/{batch_id}"
        return batch

    @protected.get("/batches/{batch_id}")
    async def read_batch(batch_id: UUID, conn: Connection) -> Batch:
        batch = await batches.get_batch(conn, batch_id)
        if batch is None:
            raise HTTPException(404, "No such batch.")
        return batch

    @protected.get("/checks/{check_id}")
    async def read_check(check_id: UUID, conn: Connection) -> Check:
        check = await batches.get_check(conn, check_id)
        if check is None:
            raise HTTPException(404, "No such check.")
        return check

    @protected.get("/exceptions")
    async def read_exceptions(
        conn: Connection, limit: Annotated[int, Query(ge=1, le=1000)] = 100
    ) -> Exceptions:
        return Exceptions(exceptions=await batches.list_exceptions(conn, limit))

    @protected.post(
        "/exceptions/{check_id}/requeue", responses={409: {"description": "Not an exception"}}
    )
    async def requeue(check_id: UUID, conn: Connection) -> Check:
        try:
            check = await batches.requeue(conn, check_id)
        except batches.NotAnExceptionError:
            raise HTTPException(409, "This check is not in the exception queue.") from None
        if check is None:
            raise HTTPException(404, "No such check.")
        return check

    app.include_router(protected)
    return app
