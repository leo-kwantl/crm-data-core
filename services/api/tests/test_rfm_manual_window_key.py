"""059: manual RFM snapshots carry an honest window_key (data owns DDL).

A v1 row that omits window_key (every app-POSTed snapshot does) must land as
manual:<from>:<to> derived from its own bounds -- never 'current' beside the
nightly series -- so same-range freezes pair and cross-range pairs refuse at
the mode + rules_version + window_key guard. v2 LD8 rows and the four named
keys are untouched; the trigger is INSERT-only.
"""
from __future__ import annotations

from datetime import date

import asyncpg
import pytest

pytestmark = pytest.mark.asyncio

MARK = "059-manual-key-probe"


async def _insert(conn: asyncpg.Connection, mode: str,
                  start: date, end: date, version: str = "v1",
                  key: str | None = None) -> dict:
    if key is None:
        row = await conn.fetchrow(
            "INSERT INTO rfm_snapshots (mode, window_from, window_to,"
            " rules_version, created_by)"
            " VALUES ($1, $2::date, $3::date, $4, $5)"
            " RETURNING id, mode, window_key, window_from, window_to,"
            " rules_version",
            mode, start, end, version, MARK)
    else:
        row = await conn.fetchrow(
            "INSERT INTO rfm_snapshots (mode, window_from, window_to,"
            " window_key, rules_version, created_by)"
            " VALUES ($1, $2::date, $3::date, $4, $5, $6)"
            " RETURNING id, mode, window_key, window_from, window_to,"
            " rules_version",
            mode, start, end, key, version, MARK)
    assert row is not None
    return dict(row)


async def test_manual_key_derived_from_bounds(database_url: str) -> None:
    conn = await asyncpg.connect(database_url)
    try:
        same_a = await _insert(conn, "custom",
                               date(2026, 9, 1), date(2026, 9, 15))
        assert same_a["window_key"] == "manual:2026-09-01:2026-09-15"
        same_b = await _insert(conn, "custom",
                               date(2026, 9, 1), date(2026, 9, 15))
        assert same_b["window_key"] == same_a["window_key"]
        other = await _insert(conn, "custom",
                              date(2026, 9, 16), date(2026, 9, 30))
        assert other["window_key"] == "manual:2026-09-16:2026-09-30"
        assert other["window_key"] != same_a["window_key"]
        ytd = await _insert(conn, "year_to_date",
                            date(2026, 1, 1), date(2026, 9, 15))
        assert ytd["window_key"] == "manual:2026-01-01:2026-09-15"
    finally:
        await conn.execute(
            "DELETE FROM rfm_snapshots WHERE created_by = $1", MARK)
        await conn.close()


async def test_named_keys_untouched_and_junk_rejected(
        database_url: str) -> None:
    conn = await asyncpg.connect(database_url)
    try:
        for key in ("current", "mom", "yoy", "lifetime"):
            row = await _insert(conn, "custom",
                                date(2026, 9, 1), date(2026, 9, 15),
                                version="v2", key=key)
            assert row["window_key"] == key
        for version in ("v1", "v2"):
            with pytest.raises(asyncpg.exceptions.CheckViolationError):
                await _insert(conn, "custom",
                              date(2026, 9, 1), date(2026, 9, 15),
                              version=version, key="last_week")
    finally:
        await conn.execute(
            "DELETE FROM rfm_snapshots WHERE created_by = $1", MARK)
        await conn.close()
