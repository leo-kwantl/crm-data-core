"""The shared-database guard: refusal message, and an audit that actually fires.

The audit is only worth having if it catches a table whose count moved, so this
test injects a row, watches the diff name it, and then balances it out again to
prove ordinary traffic does not look like drift. The refusal test pins the
message to the variable names a reader needs (`DATABASE_URL` and the opt-in),
so it cannot degrade into "something is wrong" with no way to proceed.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from services.api.tests import shared_db_guard
from services.api.tests.conftest import MIGRATIONS_DIR, _up_sql


@pytest_asyncio.fixture()
async def db_pool(database_url: str):  # type: ignore[no-untyped-def]
    """Function-loop pool, migrated idempotently (no cross-loop session pool)."""
    import asyncpg

    own = await asyncpg.create_pool(database_url)
    async with own.acquire() as conn:
        if await conn.fetchval("SELECT to_regclass('public.customers')") is None:
            for mig in sorted(MIGRATIONS_DIR.glob("*.sql")):
                sql = _up_sql(mig)
                if sql:
                    try:
                        await conn.execute(sql)
                    except Exception as exc:
                        if "already exists" not in str(exc).lower():
                            raise
    yield own
    await own.close()


def test_a_supplied_url_is_refused_until_the_run_opts_in() -> None:
    problem = shared_db_guard.shared_url_problem(
        {"DATABASE_URL": "postgresql://postgres@127.0.0.1:59531/postgres"}
    )
    assert problem is not None
    # names the hazard and both ways out
    assert "DATABASE_URL" in problem
    assert shared_db_guard.ALLOW_ENV in problem
    assert "unset DATABASE_URL" in problem
    # the default path (no external URL) and the opt-in both proceed
    assert shared_db_guard.shared_url_problem({}) is None
    assert (
        shared_db_guard.shared_url_problem(
            {"DATABASE_URL": "x", shared_db_guard.ALLOW_ENV: "1"}
        )
        is None
    )
    # anything other than the documented value is not an opt-in
    assert (
        shared_db_guard.shared_url_problem(
            {"DATABASE_URL": "x", shared_db_guard.ALLOW_ENV: "true"}
        )
        is not None
    )


@pytest.mark.asyncio
async def test_the_audit_names_the_table_a_test_forgot_to_clean(db_pool) -> None:  # type: ignore[no-untyped-def]
    async with db_pool.acquire() as conn:
        before = await shared_db_guard.table_counts(conn)
        try:
            await conn.execute(
                "INSERT INTO customers (member_id, full_name)"
                " VALUES ('M-777777', 'Audit Probe')"
            )
            moved = shared_db_guard.diff(
                before, await shared_db_guard.table_counts(conn)
            )
        finally:
            await conn.execute("DELETE FROM customers WHERE member_id = 'M-777777'")
        assert ("customers", before["customers"], before["customers"] + 1) in moved
        assert "customers" in shared_db_guard.describe(moved)
        # a write that cleans up after itself is not drift
        assert shared_db_guard.diff(before, await shared_db_guard.table_counts(conn)) == []
