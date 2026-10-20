"""The debris purge names its rows; it never guesses.

Three rules this file guards, because each one is a decision someone made and
a later edit could quietly undo:

1. Every family is a NAMED predicate -- a value (``--member M-900123``) or a
   name pattern (``--staff``, ``--user``, ``--store-name``). There is no
   "everything else" mode: a purge by exclusion cannot tell a test's leftover
   from a person's own fixture.
2. The target comes from ``DATABASE_URL`` only. A purge aimed at the wrong
   cluster is the one action in this repo that re-running does not fix.
3. A store that still has orders, stock or staff is NOT debris. The script
   reports it and leaves it alone -- a live store is a human's decision.
"""

from __future__ import annotations

import asyncio
import importlib.util
import pathlib
import sys

import pytest

DATA_ROOT = pathlib.Path(__file__).resolve().parents[3]
SCRIPT = DATA_ROOT / "scripts" / "clean_test_debris.py"
TEST_STORE_CODE = "DEBRIS-T-01"
TEST_MEMBER_ID = "M-900321"


def _module():
    """Load the script by path, the way a test is allowed to read it.

    Module scope must stay side-effect free: a connection or a raise at
    import breaks the suite from a file nobody thinks of as test-adjacent.
    """
    spec = importlib.util.spec_from_file_location("clean_test_debris", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_shape_families_are_named_patterns():
    """Each shape family names the column it matches and the pattern it uses."""
    module = _module()
    families = {flag: (table, column, pattern)
                for flag, table, column, pattern in module.PATTERN_FAMILIES}
    assert families["staff"] == ("staff", "full_name", "Test Staff %")
    assert families["user"] == ("users", "username", "u-%")
    assert families["store_name"] == ("stores", "name", "Store %")


def test_no_default_dsn(monkeypatch):
    """Unset DATABASE_URL is refused, not defaulted to some remembered cluster."""
    module = _module()
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(SystemExit) as exc:
        module.dsn()
    assert "DATABASE_URL" in str(exc.value)


def test_nothing_named_refuses(monkeypatch):
    """No family named = no run. There is no 'everything else' mode."""
    module = _module()
    monkeypatch.setattr(sys, "argv", ["clean_test_debris.py"])
    with pytest.raises(SystemExit) as exc:
        asyncio.run(module.main())
    assert "nothing named" in str(exc.value)


def test_store_with_orders_is_reported_not_deleted(
        database_url: str, monkeypatch, capsys):
    """A store that still has orders is reported, and survives the purge."""
    import asyncpg

    module = _module()

    async def setup():
        conn = await asyncpg.connect(database_url)
        try:
            brand = await conn.fetchval(
                "SELECT id FROM brands ORDER BY code LIMIT 1")
            store = await conn.fetchval(
                "INSERT INTO stores (code, name, brand_id)"
                " VALUES ($1, 'Debris Test Store', $2) RETURNING id",
                TEST_STORE_CODE, brand)
            customer = await conn.fetchval(
                "INSERT INTO customers (member_id, full_name)"
                " VALUES ($1, 'Debris Test Customer') RETURNING id",
                TEST_MEMBER_ID)
            await conn.execute(
                "INSERT INTO orders (customer_id, store_id, ordered_at,"
                " total_cents, status) VALUES ($1, $2, now(), 100,"
                " 'completed')", customer, store)
        finally:
            await conn.close()

    async def teardown():
        conn = await asyncpg.connect(database_url)
        try:
            await conn.execute(
                "DELETE FROM orders WHERE customer_id IN (SELECT id FROM"
                " customers WHERE member_id = $1)", TEST_MEMBER_ID)
            await conn.execute(
                "DELETE FROM customers WHERE member_id = $1", TEST_MEMBER_ID)
            await conn.execute(
                "DELETE FROM stores WHERE code = $1", TEST_STORE_CODE)
        finally:
            await conn.close()

    asyncio.run(setup())
    try:
        monkeypatch.setenv("DATABASE_URL", database_url)
        monkeypatch.setattr(sys, "argv", [
            "clean_test_debris.py", "--store", TEST_STORE_CODE, "--apply"])
        asyncio.run(module.main())
        out = capsys.readouterr().out
        assert "SKIPPED" in out
        assert TEST_STORE_CODE in out

        async def still_there():
            conn = await asyncpg.connect(database_url)
            try:
                return await conn.fetchval(
                    "SELECT count(*) FROM stores WHERE code = $1",
                    TEST_STORE_CODE)
            finally:
                await conn.close()

        assert int(asyncio.run(still_there())) == 1
    finally:
        asyncio.run(teardown())
