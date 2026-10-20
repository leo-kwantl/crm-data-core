"""Tests for purchase history + product lookup (spec: purchase-history).

Covers: order list newest-first + cursor paging, empty-list 200, order
detail line items, product q search + detail, 405 on all writes.
"""

from __future__ import annotations

import itertools
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

from services.api.tests.auth_helper import AUTH_H as STAFF

# The demo seed owns `M-000101`..`M-000108`, `S101`.. and the `FRM-/CLT-/CSE-1xx`
# SKUs (`db/seeds/demo.sql`), so this module's rows start in the 9-series: a fixed
# id in the demo's own range makes the *first* run fail on any demo-seeded cluster
# (`duplicate key ... customers_member_id_key ... M-000101`), which is what a
# full-directory run against `.pgembed` + `demo.sql` did on 2026-10-20.
_counter = itertools.count(900101)

#: Matched by `_purge` below and by nothing else: 900100-900199 is this module.
_MEMBER_RE = r"^M-9001[0-9]{2}$"
_SKU_RE = r"^(FRM|CLT|CSE)-9001[0-9]{2}$"
_STORE_RE = r"^S9001[0-9]{2}$"


@pytest_asyncio.fixture(scope="module", autouse=True)
async def _purge_own_rows(database_url: str) -> AsyncIterator[None]:
    """Delete what this module seeded, on pass *and* on failure.

    Matched by the predicate that created the rows (`_counter`), not by unique
    names per run: the ids are fixed, so a second run on the same cluster has to
    find them gone. Deleted children first -- `order_items` keeps no cascade to
    `orders` in every environment, and a leftover item fails the next run on a
    foreign key rather than on an assertion.
    """
    import asyncpg

    async def _purge() -> None:
        pool = await asyncpg.create_pool(database_url)
        try:
            async with pool.acquire() as conn:
                ids = [r["id"] for r in await conn.fetch(
                    "SELECT id FROM customers WHERE member_id ~ $1",
                    _MEMBER_RE)]
                if ids:
                    await conn.execute(
                        "DELETE FROM order_items WHERE order_id IN"
                        " (SELECT id FROM orders WHERE customer_id = ANY($1::uuid[]))",
                        ids)
                    await conn.execute(
                        "DELETE FROM orders WHERE customer_id = ANY($1::uuid[])",
                        ids)
                    await conn.execute(
                        "DELETE FROM customers WHERE id = ANY($1::uuid[])", ids)
                await conn.execute(
                    "DELETE FROM products WHERE sku ~ $1", _SKU_RE)
                await conn.execute(
                    "DELETE FROM stores WHERE code ~ $1", _STORE_RE)
        finally:
            await pool.close()

    await _purge()
    yield
    await _purge()


@pytest_asyncio.fixture()
async def db_pool(database_url: str):
    """Function-loop pool; migrates idempotently (no cross-loop session-pool dep)."""
    import re

    import asyncpg

    own = await asyncpg.create_pool(database_url)
    async with own.acquire() as conn:
        exists = await conn.fetchval("SELECT to_regclass('public.customers')")
        if exists is None:
            from pathlib import Path
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


async def seed_customer_with_orders(db_pool, n_orders: int = 3) -> dict:
    """One store + 3 products + one customer + n_orders (explicit ordered_at)."""
    n = next(_counter)
    async with db_pool.acquire() as conn:
        store_id = await conn.fetchval(
            "INSERT INTO stores (code, name, brand_id) VALUES"
            " ($1, $2, (SELECT id FROM brands WHERE code = 'DEMO')) RETURNING id",
            f"S{n}", f"Store {n}",
        )
        p_frame = await conn.fetchval(
            "INSERT INTO products (sku, name, category, price_cents)"
            " VALUES ($1, $2, $3, $4) RETURNING id",
            f"FRM-{n}", "Titanium Frame", "frames", 12900,
        )
        p_cloth = await conn.fetchval(
            "INSERT INTO products (sku, name, category, price_cents)"
            " VALUES ($1, $2, $3, $4) RETURNING id",
            f"CLT-{n}", "Lens Cloth", "accessories", 500,
        )
        p_case = await conn.fetchval(
            "INSERT INTO products (sku, name, category, price_cents)"
            " VALUES ($1, $2, $3, $4) RETURNING id",
            f"CSE-{n}", "Frame Case", "accessories", 1500,
        )
        customer_id = await conn.fetchval(
            "INSERT INTO customers (member_id, full_name) VALUES ($1, $2) RETURNING id",
            f"M-{n:06d}", f"Buyer {n}",
        )
        order_ids: list[str] = []
        for i in range(n_orders):
            oid = await conn.fetchval(
                "INSERT INTO orders (customer_id, store_id, ordered_at, total_cents, status)"
                " VALUES ($1, $2, now() - ($3 || ' days')::interval, $4, 'completed')"
                " RETURNING id",
                customer_id, store_id, str(i), 13400,
            )
            await conn.execute(
                "INSERT INTO order_items (order_id, product_id, qty, unit_price_cents)"
                " VALUES ($1, $2, 1, 12900), ($1, $3, 1, 500)",
                oid, p_frame, p_cloth,
            )
            order_ids.append(str(oid))
    return {
        "customer_id": str(customer_id),
        "order_ids": order_ids,  # index 0 = newest
        "p_frame": str(p_frame),
        "p_case": str(p_case),
    }


@pytest.mark.asyncio
async def test_list_orders_newest_first_with_cursor(client, db_pool):
    seed = await seed_customer_with_orders(db_pool, 3)
    r = await client.get(
        f"/api/v1/customers/{seed['customer_id']}/orders",
        params={"limit": 2}, headers=STAFF,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert [o["id"] for o in body["data"]] == seed["order_ids"][:2]
    assert body["data"][0]["item_count"] == 2
    assert body["data"][0]["total_cents"] == 13400
    assert body["next_cursor"]
    assert r.headers.get("cache-control") == "no-store"

    r2 = await client.get(
        f"/api/v1/customers/{seed['customer_id']}/orders",
        params={"limit": 2, "cursor": body["next_cursor"]}, headers=STAFF,
    )
    assert r2.status_code == 200, r2.text
    body2 = r2.json()
    assert [o["id"] for o in body2["data"]] == seed["order_ids"][2:]
    assert body2["next_cursor"] is None


@pytest.mark.asyncio
async def test_list_orders_empty_returns_200(client, db_pool):
    seed = await seed_customer_with_orders(db_pool, 0)
    r = await client.get(
        f"/api/v1/customers/{seed['customer_id']}/orders", headers=STAFF
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"data": [], "next_cursor": None}


@pytest.mark.asyncio
async def test_order_detail_line_items(client, db_pool):
    seed = await seed_customer_with_orders(db_pool, 1)
    r = await client.get(f"/api/v1/orders/{seed['order_ids'][0]}", headers=STAFF)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "completed"
    assert body["store_code"].startswith("S")
    items = body["items"]
    assert len(items) == 2
    frame = next(i for i in items if i["product_name"] == "Titanium Frame")
    assert frame["qty"] == 1 and frame["unit_price_cents"] == 12900
    assert r.headers.get("cache-control") == "no-store"


@pytest.mark.asyncio
async def test_order_detail_404(client):
    r = await client.get(f"/api/v1/orders/{uuid.uuid4()}", headers=STAFF)
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_product_search_and_detail(client, db_pool):
    seed = await seed_customer_with_orders(db_pool, 0)
    r = await client.get("/api/v1/products", params={"q": "frame"})
    assert r.status_code == 200, r.text
    names = {p["name"] for p in r.json()["data"]}
    assert "Titanium Frame" in names and "Frame Case" in names
    assert "Lens Cloth" not in names

    r2 = await client.get(f"/api/v1/products/{seed['p_case']}")
    assert r2.status_code == 200, r2.text
    assert r2.json()["sku"].startswith("CSE-")

    r3 = await client.get(f"/api/v1/products/{uuid.uuid4()}")
    assert r3.status_code == 404


@pytest.mark.asyncio
async def test_order_writes_return_405(client, db_pool):
    seed = await seed_customer_with_orders(db_pool, 1)
    oid = seed["order_ids"][0]
    cid = seed["customer_id"]
    for method, path in [
        ("post", "/api/v1/orders"),
        ("put", "/api/v1/orders"),
        ("delete", "/api/v1/orders"),
        ("post", f"/api/v1/orders/{oid}"),
        ("put", f"/api/v1/orders/{oid}"),
        ("delete", f"/api/v1/orders/{oid}"),
        ("post", f"/api/v1/customers/{cid}/orders"),
        ("delete", f"/api/v1/customers/{cid}/orders"),
    ]:
        r = await client.request(method, path, headers=STAFF, json={})
        assert r.status_code == 405, (method, path, r.status_code)
