"""Guard: the birth-date bound belongs to the person, not to the connection.

`db/migrations/017_customers_demographics.sql` declared
`CHECK (date_of_birth IS NULL OR date_of_birth <= CURRENT_DATE)`. CURRENT_DATE
renders in the *session* time zone, and this build has no tzdata — but it does
accept a numeric offset, e.g. `postgresql://...?options=-c timezone=+08:00`
(or `SET timezone` on the connection). Measured on the dev cluster: with the
session set that way, CURRENT_DATE was 2026-10-19 while the +08 business day was
2026-10-20, so the same birth date was accepted by one connection and refused by
another a day apart.

`050_dob_check_business_day.sql` re-declares the bound on the business day
(`AT TIME ZONE interval '8 hours'`, the same spelling `pipelines/snapshot.py`
uses as `BUSINESS_TZ_SQL`, for the same no-tzdata reason).

Two tests, because they fail for different reasons:

1. the constraint's *definition* carries the business-day form and does not
   mention CURRENT_DATE — deterministic, and the one that goes red if someone
   re-adds the column CHECK in a later migration;
2. the constraint's *behaviour* under a session whose date differs from the
   business day. This one skips when the two dates coincide: at that hour the
   old form would accept exactly what the new form accepts, so a pass would say
   nothing (it is vacuous, not green).

Connections come from `database_url` rather than the session `pool` fixture:
that pool is created on the loop of the first test that used it, and a module
whose async tests land on a fresh loop gets "attached to a different loop"
instead of an assertion (same reason `test_migration_drift.py` opens its own).
"""

from __future__ import annotations

from datetime import timedelta

import asyncpg
import pytest

from pipelines.snapshot import business_today

pytestmark = pytest.mark.asyncio

CHECK_NAME = "customers_date_of_birth_check"
PROBE_MEMBER = "M-909050"


async def test_bound_is_declared_on_the_business_day(database_url):
    conn = await asyncpg.connect(database_url)
    try:
        definition = await conn.fetchval(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint"
            " WHERE conname = $1 AND conrelid = 'customers'::regclass",
            CHECK_NAME)
    finally:
        await conn.close()

    assert definition, f"{CHECK_NAME} is missing from customers"
    lowered = definition.lower()
    # The bound must not be the session's calendar date...
    assert "current_date" not in lowered, definition
    # ...and must name the +08 offset explicitly. Postgres normalises the
    # literal, so accept the form it prints (`'08:00:00'::interval`) as well as
    # the one the migration writes.
    assert "time zone" in lowered, definition
    assert "'8 hours'" in lowered or "'08:00:00'" in lowered, definition


async def test_a_birth_date_on_the_business_day_is_accepted_whatever_the_session(
        database_url):
    expected = business_today()
    conn = await asyncpg.connect(database_url)
    tx = conn.transaction()
    await tx.start()
    try:
        # A POSIX-signed numeric offset, accepted here where zone *names* are
        # refused: its calendar date sits a day behind +08 for most of the UTC
        # day, which is what made CURRENT_DATE the wrong bound.
        await conn.execute("SET LOCAL timezone = '+08:00'")
        session_today = await conn.fetchval("SELECT CURRENT_DATE")
        if session_today == expected:
            pytest.skip(
                "session and business dates coincide right now, so the old"
                " CURRENT_DATE form would pass this too: vacuous, not green")

        customer = await conn.fetchval(
            "INSERT INTO customers (member_id, full_name) VALUES ($1, $2)"
            " RETURNING id", PROBE_MEMBER, "DOB bound probe")

        # The business day itself is a legal birth date under any session.
        await conn.execute(
            "UPDATE customers SET date_of_birth = $1 WHERE id = $2",
            expected, customer)

        # ... and the next business day is not, still under any session.
        with pytest.raises(asyncpg.exceptions.CheckViolationError) as refused:
            await conn.execute(
                "UPDATE customers SET date_of_birth = $1 WHERE id = $2",
                expected + timedelta(days=1), customer)
        assert "date_of_birth_check" in str(refused.value), str(refused.value)
    finally:
        await tx.rollback()
        await conn.close()
