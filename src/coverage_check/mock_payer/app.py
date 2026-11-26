"""The mock payer service. Run with: uvicorn --factory coverage_check.mock_payer.app:create_app

One process only: the limits are counted in memory.

A request is in flight from when it arrives until the payer has finished with it. A caller
that gives up changes nothing, as with a real payer (ADR 0009).
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections import deque
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, Query, Request, Response
from pydantic import ValidationError

from coverage_check.config import require_env
from coverage_check.mock_payer.members import Directory
from coverage_check.mock_payer.profile import Profile, load_profile
from coverage_check.mock_payer.store import Store
from coverage_check.models import CheckRequest
from coverage_check.protocol import Error, ErrorReply

MALFORMED_BODY = '{"result": "act'
MAX_KEY_LENGTH = 255


def _reply(status: int, body: str, headers: dict[str, str] | None = None) -> Response:
    return Response(body, status, headers, media_type="application/json")


def _error(status: int, error: Error, headers: dict[str, str] | None = None) -> Response:
    return _reply(status, ErrorReply(error=error).model_dump_json(), headers)


class Payer:
    def __init__(self, profile: Profile, store: Store, clock: Callable[[], float]) -> None:
        self.profile = profile
        self.store = store
        self.clock = clock
        self.directory = Directory()
        self.recent: deque[float] = deque()  # arrival times in the last second
        self.in_flight = 0
        self.pending_keys: set[str] = set()

    async def handle(self, key: str | None, raw: bytes) -> Response:
        try:
            check = CheckRequest.model_validate_json(raw)
        except ValidationError:
            check = None
        canonical = raw if check is None else check.model_dump_json().encode()
        body_sha256 = hashlib.sha256(canonical).hexdigest()

        # No await from here to the in-flight count, so two requests can't both take a place.
        now = self.clock()
        request_id = self.store.received(key, body_sha256, now)
        status: int | None = None
        behaviour = "aborted"
        replay = False
        counted = False
        claimed: str | None = None
        try:
            # The same rule as invariant 1: no one-second window holds more than the limit.
            while self.recent and self.recent[0] <= now - 1:
                self.recent.popleft()
            self.recent.append(now)
            retry_after = {"Retry-After": str(self.profile.retry_after_seconds)}
            if len(self.recent) > self.profile.rate_limit_per_second:
                behaviour, response = "rate_limit", _error(429, "rate_limited", retry_after)
            elif self.in_flight >= self.profile.max_in_flight:
                behaviour, response = "in_flight_limit", _error(429, "rate_limited", retry_after)
            else:
                self.in_flight += 1
                counted = True
                if check is None or not key or len(key) > MAX_KEY_LENGTH:
                    behaviour, response = "invalid_request", _error(400, "invalid_request")
                elif key in self.pending_keys:
                    # Even if the answer is decided: this key is still being handled.
                    behaviour, response = "in_progress", _error(409, "in_progress")
                elif (stored := self.store.answer(key)) is not None:
                    if stored[0] != body_sha256:
                        behaviour, response = "key_reused", _error(422, "key_reused")
                    else:
                        behaviour, replay = "replay", True
                        response = _reply(200, stored[1])
                else:
                    self.pending_keys.add(key)
                    claimed = key
                    behaviour, response = await self._serve(request_id, key, body_sha256, check)
            status = response.status_code
            return response
        finally:
            if counted:
                self.in_flight -= 1
            if claimed is not None:
                self.pending_keys.discard(claimed)
            self.store.replied(request_id, self.clock(), status, behaviour, replay)

    async def _serve(
        self, request_id: int, key: str, body_sha256: str, check: CheckRequest
    ) -> tuple[str, Response]:
        profile = self.profile
        draw = profile.draw(body_sha256, self.store.next_occurrence(request_id, body_sha256))
        await asyncio.sleep(draw.latency_seconds)
        match draw.fault:
            case "throttle":
                retry_after = {"Retry-After": str(profile.retry_after_seconds)}
                return draw.fault, _error(429, "rate_limited", retry_after)
            case "server_error":
                return draw.fault, _error(draw.server_status, "server_error")
            case "malformed":
                return draw.fault, _reply(200, MALFORMED_BODY)
            case "timeout":
                await asyncio.sleep(profile.timeout_seconds)
                return draw.fault, _error(504, "timed_out")
            case "slow":
                await asyncio.sleep(profile.slow_seconds)
        reply = self.directory.answer(check).model_dump_json()
        self.store.bill(request_id, key, body_sha256, reply)
        if draw.fault == "late":
            # Answered and billed already; the caller just doesn't hear in time.
            await asyncio.sleep(profile.late_seconds)
        return draw.fault, _reply(200, reply)


def create_app(
    profile: Profile | None = None,
    db_path: Path | None = None,
    clock: Callable[[], float] = time.time,
) -> FastAPI:
    profile = profile or load_profile(Path(require_env("MOCK_PAYER_PROFILE")))
    store = Store(db_path or Path(require_env("MOCK_PAYER_DB")))
    payer = Payer(profile, store, clock)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        store.close()

    app = FastAPI(title="mock-payer", lifespan=lifespan)

    @app.post("/eligibility")
    async def eligibility(request: Request) -> Response:
        # Read by hand, so that bad requests are logged and counted like any other.
        raw = await request.body()
        return await payer.handle(request.headers.get("Idempotency-Key"), raw)

    @app.get("/requests")
    async def requests(
        after: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=10_000)] = 1000,
    ) -> dict[str, list[dict[str, Any]]]:
        return {"requests": store.log(after, limit)}

    @app.get("/limits")
    async def limits() -> dict[str, int]:
        return {
            "rate_limit_per_second": profile.rate_limit_per_second,
            "max_in_flight": profile.max_in_flight,
        }

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app
