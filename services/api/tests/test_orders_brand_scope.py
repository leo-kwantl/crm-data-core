"""Brand scoping on the global order list (app's cross-brand leak, 78fa78d).

The global order list is global only for a group-wide or HQ session: a
brand-scoped session reads ITS brand's orders. App closed the leak in its own
runtime and pinned it with a two-brand browser e2e; data carries the same
runtime (`services/api/routers/orders.py` + `repos/orders.py`, re-mirrored
from app's HEAD), so the receipt lives HERE too: two brands, one order each,
and a brand-A session that must see no trace of brand B.

`POST /orders/search` is app-only (`services/api/routers/orders_search.py`
has no copy in this tree), so the search half of the leak is app's receipt,
not this file's.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

from services.api.tests.auth_helper import make_session

# Outside `db/seeds/demo.sql`'s space (M-000101..108, S101, FRM-/CLT-/CSE-1xx)
# and outside `test_orders.py`'s own 9001xx range: 9002xx is this module.
_MEMBER_RE = r"^M-9002[0-9]{2}$"
_SKU_RE = r"^(FRM|CLT|CSE)-9002[0-9]{2}$"
_STORE_RE = r"^S9002[0-9]{2}$"
_BRAND_RE = r"^BRD-9002[0-9]{2}$"
_TOKEN_A = "test-brand-scope-a-token"
_TOKEN_B = "test-brand-scope-hq-token"
_STAFF_A = "S-BRAND-A"
_STAFF_HQ = "S-BRAND-HQ"


@pytest_asyncio.fixture(autouse=True)
async def _purge_own_rows(db_pool) -> AsyncIterator[None]:
    """Delete what this module seeded, on pass *and* on failure.

    Matched by the predicates that created the rows. Children first: the
    brand is the head of the chain (brand -> store -> order -> items), and a
    leftover row fails the next run on a foreign key rather than on an
    assertion. It runs on `db_pool`, not on a bare URL: that pool is what
    migrates the schema, and a purge before the migration finds no table.
    """

    async def _purge() -> None:
        async with db_pool.acquire() as conn:
            ids = [r["id"] for r in await conn.fetch(
                "SELECT id FROM customers WHERE member_id ~ $1", _MEMBER_RE)]
            if ids:
                await conn.execute(
                    "DELETE FROM customer_contacts"
                    " WHERE customer_id = ANY($1::uuid[])", ids)
                await conn.execute(
                    "DELETE FROM order_items WHERE order_id IN"
                    " (SELECT id FROM orders WHERE customer_id = ANY($1::uuid[]))",
                    ids)
                await conn.execute(
                    "DELETE FROM orders WHERE customer_id = ANY($1::uuid[])", ids)
                await conn.execute(
                    "DELETE FROM customers WHERE id = ANY($1::uuid[])", ids)
            await conn.execute(
                "DELETE FROM sessions WHERE staff_code IN ($1, $2)",
                _STAFF_A, _STAFF_HQ)
            await conn.execute("DELETE FROM users WHERE username IN ($1, $2)",
                               "test-brandscope-a", "test-brandscope-hq")
            await conn.execute("DELETE FROM staff WHERE staff_code IN ($1, $2)",
                               _STAFF_A, _STAFF_HQ)
            await conn.execute("DELETE FROM products WHERE sku ~ $1", _SKU_RE)
            await conn.execute("DELETE FROM stores WHERE code ~ $1", _STORE_RE)
            await conn.execute("DELETE FROM brands WHERE code ~ $1", _BRAND_RE)

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
async def client(db_pool) -> AsyncIterator:
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


@pytest_asyncio.fixture()
async def two_brands(db_pool) -> dict:
    """Two brands, a store + customer + order in each. One order per brand."""
    out: dict = {}
    async with db_pool.acquire() as conn:
        group_id = await conn.fetchval("SELECT id FROM groups WHERE code = 'DEMO'")
        for tag in ("a", "b"):
            n = 900200 + (0 if tag == "a" else 1)
            brand_id = await conn.fetchval(
                "INSERT INTO brands (group_id, code, name)"
                " VALUES ($1, $2, $3) RETURNING id",
                group_id, f"BRD-{n}", f"Brand {tag.upper()}",
            )
            store_id = await conn.fetchval(
                "INSERT INTO stores (code, name, brand_id)"
                " VALUES ($1, $2, $3) RETURNING id",
                f"S{n}", f"Store {tag.upper()}", brand_id,
            )
            sku = f"FRM-{n}"
            await conn.execute(
                "INSERT INTO products (sku, name, category, price_cents)"
                " VALUES ($1, $2, 'frames', 12900)", sku, "Titanium Frame")
            product_id = await conn.fetchval(
                "SELECT id FROM products WHERE sku = $1", sku)
            customer_id = await conn.fetchval(
                "INSERT INTO customers (member_id, full_name)"
                " VALUES ($1, $2) RETURNING id", f"M-{n:06d}", f"Buyer {tag.upper()}")
            order_id = await conn.fetchval(
                "INSERT INTO orders (customer_id, store_id, ordered_at, total_cents, status)"
                " VALUES ($1, $2, now(), 12900, 'completed') RETURNING id",
                customer_id, store_id)
            await conn.execute(
                "INSERT INTO order_items (order_id, product_id, qty, unit_price_cents)"
                " VALUES ($1, $2, 1, 12900)", order_id, product_id)
            out[f"brand_{tag}"] = str(brand_id)
            out[f"order_{tag}"] = str(order_id)
    return out


@pytest.mark.asyncio
async def test_a_brand_session_reads_only_its_brand_orders(client, db_pool, two_brands):
    """The leak's shape: a brand-A session must not see brand B's order."""
    headers = await make_session(
        db_pool, "brandscope-a", role="sa", staff_code=_STAFF_A,
        token=_TOKEN_A, brand_id=two_brands["brand_a"],
    )
    r = await client.get("/api/v1/orders", headers=headers)
    assert r.status_code == 200, r.text
    ids = {row["id"] for row in r.json()["data"]}
    assert two_brands["order_a"] in ids
    assert two_brands["order_b"] not in ids


@pytest.mark.asyncio
async def test_an_hq_session_still_reads_every_brand(client, db_pool, two_brands):
    """The list is global only for a session that is not brand-pinned."""
    headers = await make_session(
        db_pool, "brandscope-hq", role="admin", staff_code="S-BRAND-HQ",
        token=_TOKEN_B,
    )
    r = await client.get("/api/v1/orders", headers=headers)
    assert r.status_code == 200, r.text
    ids = {row["id"] for row in r.json()["data"]}
    assert two_brands["order_a"] in ids
    assert two_brands["order_b"] in ids
