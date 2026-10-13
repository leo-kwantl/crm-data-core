"""Tests for customer journey events (append-only timeline).

Covers: append + newest-first list order (+ cursor paging), header fields,
404 on unknown customer, 422 on blank kind/summary, 405 on PUT/DELETE,
append-only repo check, cascade FK verified read-only via pg catalog
(no destructive test).
"""

from __future__ import annotations

import itertools
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

import asyncio
import sys

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from services.api.tests.auth_helper import AUTH_H as STAFF
_counter = itertools.count(201)

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture()
async def db_pool(database_url: str):
    """Function-loop pool; migrates idempotently (no cross-loop session-pool dep)."""
    import re

    import asyncpg

    own = await asyncpg.create_pool(database_url)
    async with own.acquire() as conn:
        exists = await conn.fetchval("SELECT to_regclass('public.customers')")
        if exists is None:
            up_re = re.compile(r"--\s*migrate:up(.*?)(?=--\s*migrate:down|\Z)", re.S | re.I)
            mig_dir = Path(__file__).resolve().parents[3] / "db" / "migrations"
            for mig in sorted(mig_dir.glob("*.sql")):
                m = up_re.search(mig.read_text(encoding="utf-8"))
                if m and m.group(1).strip():
                    try:
                        await conn.execute(m.group(1).strip())
                    except Exception as exc:
                        if "already exists" not in str(exc).lower():
                            raise
    yield own
    await own.close()


@pytest_asyncio.fixture()
async def client(db_pool) -> AsyncIterator:  # shadows conftest client
    """App client wired to the session pgembed DB (dependency override)."""
    from httpx import ASGITransport, AsyncClient

    from services.api import deps as deps_mod
    from services.api.app import create_app

    app = create_app()

    async def _override() -> AsyncIterator:
        yield db_pool

    app.dependency_overrides[deps_mod.get_pool] = _override
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def _make_customer(db_pool) -> tuple[str, str, str]:
    n = next(_counter) % 900000 + 100000
    member = f"M-{n:06d}"
    async with db_pool.acquire() as conn:
        for _ in range(3):  # retry on unlikely member_id collision
            try:
                row = await conn.fetchrow(
                    "INSERT INTO customers (member_id, full_name) VALUES ($1, $2)"
                    " RETURNING id",
                    member,
                    "Event Test User",
                )
                assert row is not None
                return str(row["id"]), member, "Event Test User"
            except Exception:
                n = (n + 1) % 900000 + 100000
                member = f"M-{n:06d}"
    raise AssertionError("could not create customer")


async def test_append_and_list_newest_first(client, db_pool) -> None:
    cid, member, name = await _make_customer(db_pool)
    kinds = ["visit", "note", "complaint"]
    for kind in kinds:
        r = await client.post(
            f"/api/v1/customers/{cid}/events",
            headers=STAFF,
            json={"kind": kind, "summary": f"{kind} summary"},
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["customer_id"] == cid
        assert body["kind"] == kind
        assert body["source"] == "manual"  # default
        assert body["payload"] == {}
        from services.api.tests.auth_helper import ADMIN_STAFF_CODE

        assert body["created_by"] == ADMIN_STAFF_CODE  # session staff_code default
        assert body["occurred_at"] and body["created_at"]
    r = await client.get(f"/api/v1/customers/{cid}/events", headers=STAFF)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["customer_id"] == cid
    assert body["customer_member_id"] == member
    assert body["customer_name"] == name
    assert [e["kind"] for e in body["data"]] == list(reversed(kinds))
    assert body["next_cursor"] is None
    assert r.headers.get("cache-control") == "no-store"


async def test_list_cursor_paging(client, db_pool) -> None:
    cid, _, _ = await _make_customer(db_pool)
    for i in range(3):
        r = await client.post(
            f"/api/v1/customers/{cid}/events",
            headers=STAFF,
            json={"kind": "note", "summary": f"note {i}"},
        )
        assert r.status_code == 201, r.text
    page1 = (await client.get(
        f"/api/v1/customers/{cid}/events", headers=STAFF, params={"limit": 2}
    )).json()
    assert len(page1["data"]) == 2
    assert page1["next_cursor"]
    page2 = (await client.get(
        f"/api/v1/customers/{cid}/events",
        headers=STAFF,
        params={"limit": 2, "cursor": page1["next_cursor"]},
    )).json()
    assert len(page2["data"]) == 1
    assert page2["next_cursor"] is None
    # Pages tile the full newest-first timeline without overlap.
    assert [e["summary"] for e in page1["data"]] == ["note 2", "note 1"]
    assert [e["summary"] for e in page2["data"]] == ["note 0"]
    r = await client.get(
        f"/api/v1/customers/{cid}/events",
        headers=STAFF,
        params={"cursor": "not-a-cursor"},
    )
    assert r.status_code == 400


async def test_unknown_customer_404_and_auth(client) -> None:
    fake = str(uuid.uuid4())
    r = await client.get(f"/api/v1/customers/{fake}/events", headers=STAFF)
    assert r.status_code == 404
    r = await client.post(
        f"/api/v1/customers/{fake}/events",
        headers=STAFF,
        json={"kind": "note", "summary": "hello"},
    )
    assert r.status_code == 404
    cid_like = str(uuid.uuid4())
    r = await client.get(f"/api/v1/customers/{cid_like}/events")
    assert r.status_code == 401


async def test_blank_kind_summary_422(client, db_pool) -> None:
    cid, _, _ = await _make_customer(db_pool)
    for payload in (
        {"kind": "", "summary": "ok"},
        {"kind": "   ", "summary": "ok"},
        {"kind": "note", "summary": ""},
        {"kind": "note", "summary": "   "},
    ):
        r = await client.post(
            f"/api/v1/customers/{cid}/events", headers=STAFF, json=payload
        )
        assert r.status_code == 422, (payload, r.text)


async def test_no_put_delete_405(client, db_pool) -> None:
    cid, _, _ = await _make_customer(db_pool)
    r = await client.put(
        f"/api/v1/customers/{cid}/events", headers=STAFF, json={"summary": "x"}
    )
    assert r.status_code == 405
    r = await client.delete(f"/api/v1/customers/{cid}/events", headers=STAFF)
    assert r.status_code == 405


async def test_append_only_and_cascade_catalog(client, db_pool) -> None:
    # Repo layer never updates/deletes event rows (migration also REVOKEs them).
    repo_src = (
        Path(__file__).resolve().parents[1] / "repos" / "customer_events.py"
    ).read_text(encoding="utf-8").lower()
    assert "update customer_events" not in repo_src
    assert "delete from customer_events" not in repo_src
    # Cascade verified read-only via the catalog (no destructive test):
    # customer_events.customer_id FK must be ON DELETE CASCADE.
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT confdeltype FROM pg_constraint
            WHERE conrelid = 'customer_events'::regclass
              AND contype = 'f'
            """
        )
    assert row is not None and bytes(row["confdeltype"]).decode() == "c"
    # Timeline index exists for (customer_id, occurred_at DESC) paging.
    async with db_pool.acquire() as conn:
        idx = await conn.fetchval(
            "SELECT to_regclass('public.customer_events_customer_idx')"
        )
    assert idx == "customer_events_customer_idx"
