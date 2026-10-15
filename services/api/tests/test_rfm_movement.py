"""LD8 time-aware RFM storage tests (data owns DDL, ml owns engines).

Pure: window/pair enums, sentinel direction, flow pct required in
0..1 over both-window customers, ENTERED/EXITED must be NULL pct
(outside the denominator by construction). DB: snapshot windows +
rules v2 coexist with v1 history; sizes + movement round-trip;
re-run is a no-op via the grain PKs.
"""
from __future__ import annotations

from datetime import date

import pytest

from pipelines.snapshot import (
    ENTERED,
    EXITED,
    validate_movement_row,
    validate_segment_size,
)

pytestmark = pytest.mark.asyncio


def test_size_ok_and_rejects():
    out = validate_segment_size({
        "window_key": "mom", "segment": "champions", "n": 40,
        "median_r": 4.0, "median_f": 3.5, "median_m": 4.5})
    assert out["window_key"] == "mom" and out["n"] == 40
    with pytest.raises(ValueError, match="window_key"):
        validate_segment_size({**{"window_key": "mom", "segment": "x",
                                  "n": 1, "median_r": 1.0,
                                  "median_f": 1.0, "median_m": 1.0},
                               "window_key": "last_week"})
    with pytest.raises(ValueError, match="numeric"):
        validate_segment_size({"window_key": "mom", "segment": "x", "n": 1,
                               "median_r": "high", "median_f": 1.0,
                               "median_m": 1.0})


def test_movement_flow_and_sentinels():
    flow = validate_movement_row({
        "pair": "current_x_mom", "from_segment": "at_risk",
        "to_segment": "champions", "n": 12, "pct_of_from": 0.3})
    assert flow["pct_of_from"] == 0.3
    with pytest.raises(ValueError, match="both-window"):
        validate_movement_row({
            "pair": "current_x_mom", "from_segment": "a",
            "to_segment": "b", "n": 1, "pct_of_from": None})
    entered = validate_movement_row({
        "pair": "current_x_yoy", "from_segment": ENTERED,
        "to_segment": "new", "n": 25, "pct_of_from": None})
    assert entered["pct_of_from"] is None
    with pytest.raises(ValueError, match="outside the flow pct"):
        validate_movement_row({
            "pair": "current_x_yoy", "from_segment": ENTERED,
            "to_segment": "new", "n": 25, "pct_of_from": 0.5})
    with pytest.raises(ValueError, match="sentinel direction"):
        validate_movement_row({
            "pair": "current_x_yoy", "from_segment": EXITED,
            "to_segment": "champions", "n": 1,
            "pct_of_from": None})
    with pytest.raises(ValueError, match="pair"):
        validate_movement_row({
            "pair": "current_x_lifetime", "from_segment": "a",
            "to_segment": "b", "n": 1, "pct_of_from": 0.5})


async def test_ld8_db_roundtrip(database_url: str):
    """Windowed snapshots + v2 rows sit beside v1; sizes/movement
    round-trip; re-run no-ops on the grain PKs."""
    import re
    from pathlib import Path

    import asyncpg

    from pipelines.snapshot import (
        MOVEMENT_FOR_PAIR_SQL,
        MOVEMENT_UPSERT_SQL,
        SEGMENT_SIZE_UPSERT_SQL,
        SIZES_FOR_WINDOW_SQL,
    )

    pool = await asyncpg.create_pool(database_url)
    assert pool is not None
    try:
        async with pool.acquire() as conn:
            for name in ("010_analytics_rfm.sql",
                         "031_rfm_time_aware.sql"):
                if await conn.fetchval(
                        "SELECT to_regclass($1)",
                        "public." + ("rfm_snapshots" if name.startswith(
                            "010") else "rfm_segment_movement")) is None:
                    text = (Path(__file__).resolve().parents[3]
                            / "db" / "migrations" / name).read_text(
                                encoding="utf-8")
                    m = re.search(
                        r"--\s*migrate:up(.*?)(?=--\s*migrate:down|\Z)",
                        text, re.S | re.I)
                    assert m is not None
                    try:
                        await conn.execute(m.group(1).strip())
                    except Exception as exc:
                        if "already exists" not in str(exc).lower():
                            raise
            day = date(2026, 10, 15)
            # v1 history untouched; v2 windowed rows beside it.
            await conn.execute(
                "INSERT INTO rfm_snapshots (mode, window_from, window_to,"
                " window_key, rules_version, customer_count)"
                " VALUES ('rolling_365', $1::date, $1::date, 'current',"
                " 'v2', 100) ON CONFLICT DO NOTHING", day)
            n_v2 = await conn.fetchval(
                "SELECT COUNT(*) FROM rfm_snapshots"
                " WHERE window_key = 'current'"
                " AND rules_version = 'v2'")
            assert int(n_v2) >= 1
            size = validate_segment_size({
                "window_key": "current", "segment": "champions", "n": 40,
                "median_r": 4.0, "median_f": 3.5, "median_m": 4.5})
            for _ in range(2):
                await conn.execute(
                    SEGMENT_SIZE_UPSERT_SQL, day, size["window_key"],
                    size["segment"], size["n"], size["median_r"],
                    size["median_f"], size["median_m"])
            assert await conn.fetchval(
                "SELECT COUNT(*) FROM rfm_segment_sizes"
                " WHERE as_of = $1::date", day) == 1
            rows = [
                validate_movement_row({
                    "pair": "current_x_mom", "from_segment": "at_risk",
                    "to_segment": "champions", "n": 12,
                    "pct_of_from": 0.3}),
                validate_movement_row({
                    "pair": "current_x_mom",
                    "from_segment": ENTERED, "to_segment": "new",
                    "n": 25, "pct_of_from": None}),
                validate_movement_row({
                    "pair": "current_x_mom", "from_segment": "dormant",
                    "to_segment": EXITED, "n": 7,
                    "pct_of_from": None}),
            ]
            for _ in range(2):
                for row in rows:
                    await conn.execute(
                        MOVEMENT_UPSERT_SQL, day, row["pair"],
                        row["from_segment"], row["to_segment"],
                        row["n"], row["pct_of_from"])
            got = await conn.fetch(MOVEMENT_FOR_PAIR_SQL, day,
                                   "current_x_mom")
            assert len(got) == 3
            by_from = {r["from_segment"]: r for r in got}
            assert by_from["at_risk"]["pct_of_from"] == 0.3
            assert by_from[ENTERED]["pct_of_from"] is None
            assert by_from["dormant"]["to_segment"] == EXITED
            sizes = await conn.fetch(
                SIZES_FOR_WINDOW_SQL, day, "current")
            assert len(sizes) == 1 and sizes[0]["n"] == 40
            await conn.execute(
                "DELETE FROM rfm_segment_movement"
                " WHERE as_of = $1::date", day)
            await conn.execute(
                "DELETE FROM rfm_segment_sizes"
                " WHERE as_of = $1::date", day)
            await conn.execute(
                "DELETE FROM rfm_snapshots"
                " WHERE window_from = $1::date"
                " AND rules_version = 'v2'", day)
    finally:
        await pool.close()
