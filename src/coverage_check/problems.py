"""Errors as RFC 9457 problem details. Input values are never echoed back."""

from __future__ import annotations

from collections.abc import Mapping
from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from psycopg import OperationalError
from starlette.exceptions import HTTPException

MEDIA_TYPE = "application/problem+json"
MAX_ERRORS = 20


def problem(
    status: int,
    detail: str,
    headers: Mapping[str, str] | None = None,
    **extra: Any,
) -> JSONResponse:
    body = {
        "type": "about:blank",
        "title": HTTPStatus(status).phrase,
        "status": status,
        "detail": detail,
        **extra,
    }
    return JSONResponse(body, status_code=status, headers=headers, media_type=MEDIA_TYPE)


def install(app: FastAPI) -> None:
    @app.exception_handler(HTTPException)
    async def http_error(_: Request, exc: HTTPException) -> JSONResponse:
        return problem(exc.status_code, str(exc.detail), headers=exc.headers)

    @app.exception_handler(OperationalError)
    async def database_unavailable(_: Request, __: OperationalError) -> JSONResponse:
        # Includes the pool running out of time. The error's own text is not for callers.
        return problem(503, "The database is not available. Try again.", {"Retry-After": "5"})

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_: Request, exc: RequestValidationError) -> JSONResponse:
        # Only where and why: "input" and "ctx" can carry what the caller sent.
        errors = [
            {"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]}
            for e in exc.errors()[:MAX_ERRORS]
        ]
        return problem(422, "The request is not valid.", errors=errors)
