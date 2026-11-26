"""The engine's side of the payer protocol (ADR 0006: a small client interface)."""

from __future__ import annotations

import asyncio

import httpx2

from coverage_check.models import CheckRequest
from coverage_check.policy import Answered, Failed, Observation


class PayerClient:
    def __init__(self, base_url: str, timeout_seconds: float) -> None:
        self._timeout = timeout_seconds
        self._http = httpx2.AsyncClient(base_url=base_url, timeout=timeout_seconds)

    async def close(self) -> None:
        await self._http.aclose()

    async def ask(self, request: CheckRequest, key: str) -> Observation:
        try:
            # httpx2's timeouts limit each wait, not the whole call, so bound that too:
            # the slot and the lease are sized from it.
            async with asyncio.timeout(self._timeout):
                response = await self._http.post(
                    "/eligibility",
                    content=request.model_dump_json(),
                    headers={"Idempotency-Key": key, "Content-Type": "application/json"},
                )
        except (httpx2.ConnectError, httpx2.ConnectTimeout, httpx2.PoolTimeout):
            return Failed("connection", sent=False)
        except (httpx2.TimeoutException, TimeoutError):
            return Failed("timeout", sent=True)
        except httpx2.HTTPError:
            return Failed("connection", sent=True)
        return Answered(response.status_code, response.text, response.headers.get("Retry-After"))
