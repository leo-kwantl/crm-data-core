"""Manifest scheduling invariants (041).

A job nobody schedules is a page nobody updates: `segments_behavior` sat in
no manifest while the behavior segment page read its rows, and the other two
segment modes sat in `weekly`, so an explain-vs-segment mismatch could 404
`/analytics/segments/explain` for up to seven days. 041 moves all three modes
to the nightly cron.

These read the manifests a clone actually ends up with after applying
`db/migrations` -- not the migration text -- so a seed-only schedule fails
here exactly as it would in production.
"""
from __future__ import annotations

import pytest

from pipelines.schedule import FETCH_MANIFESTS_SQL, parse_manifest

pytestmark = pytest.mark.asyncio

# The modes `run_segments_snapshot` documents and the segment pages read.
SEGMENT_MODES = {"purchase", "overall", "behavior"}


async def _manifests(conn) -> dict[str, dict[str, dict]]:
    out: dict[str, dict[str, dict]] = {}
    for row in await conn.fetch(FETCH_MANIFESTS_SQL):
        out.update(parse_manifest(row["yaml"]))
    return out


async def test_nightly_schedules_every_segment_mode(database_url: str):
    """All three segment modes run nightly, so a bad run heals in a day."""
    import asyncpg
    pool = await asyncpg.create_pool(database_url)
    try:
        async with pool.acquire() as conn:
            manifests = await _manifests(conn)
    finally:
        await pool.close()
    nightly = manifests.get("nightly", {})
    modes = {spec["params"].get("mode") for spec in nightly.values()
             if spec["engine"] == "segments"}
    assert modes == SEGMENT_MODES, (
        f"nightly schedules segment modes {sorted(m for m in modes if m)};"
        f" every mode the pages read must have a job: {sorted(SEGMENT_MODES)}")


async def test_every_dependency_name_resolves(database_url: str):
    """A `depends_on` naming a job that is not in the same manifest is a job
    that never becomes due -- exactly what folding segments out of `weekly`
    would have done to its `assoc`/`explain`/`matrix`."""
    import asyncpg
    pool = await asyncpg.create_pool(database_url)
    try:
        async with pool.acquire() as conn:
            manifests = await _manifests(conn)
    finally:
        await pool.close()
    dangling = [(name, job, dep) for name, jobs in manifests.items()
                for job, spec in jobs.items()
                for dep in spec["depends_on"] if dep not in jobs]
    assert not dangling, f"dangling depends_on: {dangling}"
