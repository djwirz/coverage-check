"""Submit, idempotency, reads and errors, against real Postgres."""

from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from starlette.testclient import TestClient

from coverage_check.batches import submit_batch
from coverage_check.seed import sample_batch

CHECK = {
    "member_id": "member-1",
    "given": ["Ada"],
    "family": "Example",
    "birth_date": "1990-01-31",
    "payer": "Example Payer",
    "service_date": "2026-11-01",
}


def batch_body(*checks: dict[str, Any]) -> dict[str, Any]:
    return {"checks": list(checks) or [CHECK]}


def post(client: TestClient, body: Any, key: str = "key-1") -> Any:
    return client.post("/batches", json=body, headers={"Idempotency-Key": key})


def count(db: psycopg.Connection[Any], table: str) -> int:
    row = db.execute(
        psycopg.sql.SQL("SELECT count(*) FROM {}").format(psycopg.sql.Identifier(table))
    ).fetchone()
    assert row is not None
    return int(row[0])


def test_submit_creates_queued_checks_in_order(client: TestClient) -> None:
    response = post(client, sample_batch().model_dump(mode="json"))
    assert response.status_code == 201
    batch = response.json()
    assert response.headers["Location"] == f"/batches/{batch['id']}"
    assert batch["total"] == 35
    assert batch["done"] is False
    assert batch["states"] == {"queued": 35, "leased": 0, "done": 0, "exception": 0}
    assert batch["outcomes"] == {"covered": 0, "not_covered": 0}
    assert [c["position"] for c in batch["checks"]] == list(range(35))
    assert client.get(f"/batches/{batch['id']}").json() == batch


def test_check_returns_what_was_submitted(client: TestClient) -> None:
    batch = post(client, batch_body()).json()
    check_id = batch["checks"][0]["id"]
    assert client.get(f"/checks/{check_id}").json() == {
        **CHECK,
        "id": check_id,
        "batch_id": batch["id"],
        "position": 0,
        "state": "queued",
        "outcome": None,
        "exception_reason": None,
        "result": None,
    }


def test_resubmitting_creates_nothing_new(client: TestClient, db: psycopg.Connection[Any]) -> None:
    first = post(client, batch_body(CHECK, CHECK))
    # The same request written differently: other key order.
    reordered = {k: CHECK[k] for k in reversed(CHECK)}
    again = post(client, batch_body(reordered, CHECK))
    assert (first.status_code, again.status_code) == (201, 200)
    assert again.json() == first.json()
    assert (count(db, "batches"), count(db, "checks")) == (1, 2)


def test_same_key_with_a_different_body_is_a_conflict(
    client: TestClient, db: psycopg.Connection[Any]
) -> None:
    post(client, batch_body())
    response = post(client, batch_body({**CHECK, "member_id": "member-2"}))
    assert response.status_code == 409
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["status"] == 409
    assert (count(db, "batches"), count(db, "checks")) == (1, 1)


def test_a_new_key_is_a_new_batch(client: TestClient, db: psycopg.Connection[Any]) -> None:
    assert post(client, batch_body(), "a").status_code == 201
    assert post(client, batch_body(), "b").status_code == 201
    assert count(db, "batches") == 2


def test_many_submits_at_once_make_one_batch(
    client: TestClient, db: psycopg.Connection[Any]
) -> None:
    body = sample_batch().model_dump(mode="json")
    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(lambda _: post(client, body), range(8)))
    assert sorted(r.status_code for r in responses) == [200] * 7 + [201]
    assert len({r.json()["id"] for r in responses}) == 1
    assert (count(db, "batches"), count(db, "checks")) == (1, 35)


def test_a_submit_waits_for_an_uncommitted_one_with_the_same_key(database_url: str) -> None:
    """The race made to happen: the second submit arrives before the first has committed."""

    async def race() -> None:
        connect = psycopg.AsyncConnection.connect
        async with await connect(database_url) as first, await connect(database_url) as second:
            async with first.transaction():
                first_id, created = await submit_batch(first, "key", sample_batch(1))
                assert created
                waiting = asyncio.create_task(submit_batch(second, "key", sample_batch(1)))
                done, _ = await asyncio.wait({waiting}, timeout=0.5)
                assert not done
            assert await waiting == (first_id, False)

    asyncio.run(race())


def test_exceptions_lists_only_exceptions_oldest_first(
    client: TestClient, db: psycopg.Connection[Any]
) -> None:
    batch = post(client, batch_body(CHECK, CHECK, CHECK)).json()
    assert client.get("/exceptions").json() == {"exceptions": []}
    newer, older, _ = (c["id"] for c in batch["checks"])
    # Workers don't exist yet, so put two checks into the state they will leave them in.
    for check_id, age in ((newer, "1 minute"), (older, "1 hour")):
        db.execute(
            "UPDATE checks SET state = 'exception', exception_reason = 'member_not_found',"
            " updated_at = now() - %s::interval WHERE id = %s",
            (age, check_id),
        )
    listed = client.get("/exceptions").json()["exceptions"]
    assert [c["id"] for c in listed] == [older, newer]
    assert listed[0]["exception_reason"] == "member_not_found"
    assert [c["id"] for c in client.get("/exceptions?limit=1").json()["exceptions"]] == [older]
    assert client.get(f"/batches/{batch['id']}").json()["states"]["exception"] == 2


def test_batch_is_done_when_every_check_is_final(
    client: TestClient, db: psycopg.Connection[Any]
) -> None:
    batch = post(client, batch_body(CHECK, CHECK)).json()
    first, second = (c["id"] for c in batch["checks"])
    db.execute(
        "UPDATE checks SET state = 'done', outcome = 'covered', result = '{}' WHERE id = %s",
        (first,),
    )
    db.execute(
        "UPDATE checks SET state = 'exception', exception_reason = 'x' WHERE id = %s", (second,)
    )
    read = client.get(f"/batches/{batch['id']}").json()
    assert read["done"] is True
    assert read["outcomes"] == {"covered": 1, "not_covered": 0}


@pytest.mark.parametrize("path", ["/batches/{}", "/checks/{}"])
def test_unknown_id_is_404(client: TestClient, path: str) -> None:
    response = client.get(path.format(uuid4()))
    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"


@pytest.mark.parametrize("authorization", ["", "Bearer wrong", "Basic test-token", "Bearer é"])
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/batches"),
        ("GET", f"/batches/{uuid4()}"),
        ("GET", f"/checks/{uuid4()}"),
        ("GET", "/exceptions"),
    ],
)
def test_every_endpoint_needs_the_token(
    client: TestClient,
    db: psycopg.Connection[Any],
    method: str,
    path: str,
    authorization: str,
) -> None:
    response = client.request(
        method,
        path,
        json=batch_body() if method == "POST" else None,
        headers=[(b"authorization", authorization.encode()), (b"idempotency-key", b"k")],
    )
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert count(db, "batches") == 0


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_there_are_no_open_docs_pages(client: TestClient, path: str) -> None:
    assert client.get(path).status_code == 404


@pytest.mark.parametrize(
    "check",
    [
        {**CHECK, "coverage_state": "covered"},
        {**CHECK, "member_id": ""},
        {**CHECK, "member_id": "a\u0000b"},
        {**CHECK, "family": "\ud800"},
        {**CHECK, "given": []},
        {**CHECK, "birth_date": "SECRET-VALUE"},
        {k: v for k, v in CHECK.items() if k != "service_date"},
    ],
    ids=["expected-answer", "empty", "nul", "surrogate", "no-given", "bad-date", "missing"],
)
def test_invalid_checks_are_refused_without_echoing_them(
    client: TestClient, db: psycopg.Connection[Any], check: dict[str, Any]
) -> None:
    # json.dumps escapes the surrogate, which httpx's own encoder can't.
    response = client.post(
        "/batches",
        content=json.dumps(batch_body(check)),
        headers={"Idempotency-Key": "k", "Content-Type": "application/json"},
    )
    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"
    assert "SECRET-VALUE" not in response.text
    assert response.json()["errors"][0]["loc"][0] == "body"
    assert count(db, "batches") == 0


@pytest.mark.parametrize("size", [0, 5001])
def test_batch_size_is_capped(client: TestClient, size: int) -> None:
    assert post(client, {"checks": [CHECK] * size}).status_code == 422


@pytest.mark.parametrize("headers", [{}, {"Idempotency-Key": ""}, {"Idempotency-Key": "k" * 256}])
def test_idempotency_key_is_required(client: TestClient, headers: dict[str, str]) -> None:
    assert client.post("/batches", json=batch_body(), headers=headers).status_code == 422


def make_exception(db: psycopg.Connection[Any], check_id: str) -> None:
    db.execute(
        "UPDATE checks SET state = 'exception', exception_reason = 'payer_unavailable',"
        " result = '{}' WHERE id = %s",
        (check_id,),
    )


def test_requeue_sends_an_exception_round_again(
    client: TestClient, db: psycopg.Connection[Any]
) -> None:
    batch = post(client, batch_body()).json()
    check_id = batch["checks"][0]["id"]
    make_exception(db, check_id)
    response = client.post(f"/exceptions/{check_id}/requeue")
    assert response.status_code == 200
    check = response.json()
    assert (check["state"], check["exception_reason"], check["result"]) == ("queued", None, None)
    assert db.execute("SELECT cycle FROM checks").fetchone() == (1,)
    assert client.get("/exceptions").json() == {"exceptions": []}
    assert client.get(f"/batches/{batch['id']}").json()["done"] is False


def test_only_an_exception_can_be_requeued(client: TestClient) -> None:
    check_id = post(client, batch_body()).json()["checks"][0]["id"]
    response = client.post(f"/exceptions/{check_id}/requeue")
    assert response.status_code == 409
    assert response.headers["content-type"] == "application/problem+json"
    assert client.post(f"/exceptions/{uuid4()}/requeue").status_code == 404


def test_of_many_requeues_at_once_one_succeeds(
    client: TestClient, db: psycopg.Connection[Any]
) -> None:
    check_id = post(client, batch_body()).json()["checks"][0]["id"]
    make_exception(db, check_id)
    with ThreadPoolExecutor(max_workers=8) as pool:
        statuses = list(
            pool.map(lambda _: client.post(f"/exceptions/{check_id}/requeue").status_code, range(8))
        )
    assert sorted(statuses) == [200] + [409] * 7
    assert db.execute("SELECT cycle FROM checks").fetchone() == (1,)


def test_requeue_needs_the_token(client: TestClient) -> None:
    response = client.post(f"/exceptions/{uuid4()}/requeue", headers={"Authorization": "Bearer no"})
    assert response.status_code == 401
