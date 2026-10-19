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


# The surfaces a brand-scoped session reads, each written by its own engine.
BRAND_SURFACES = {
    "ld8_snapshot", "churn_snapshot", "clv_snapshot", "forecast_snapshot",
}


async def test_nightly_writes_every_brand_scoped_surface(database_url: str):
    """045: every surface the brand session reads is refreshed by the tick.

    `forecast` sat only in `weekly`, so the demo brand's newest batch could be
    seven days old while `churn`/`clv`/`rfm` refreshed nightly -- the rank a
    brand sees changed daily while its forecast did not. The brand scope is
    half of the fix: without `brands: ['*']` the tick writes one hq roll-up
    and the brand session still 404s.
    """
    import asyncpg
    import yaml

    pool = await asyncpg.create_pool(database_url)
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT pipeline, yaml FROM pipeline_manifests"
                " WHERE pipeline IN ('nightly', 'weekly')")
    finally:
        await pool.close()
    docs = {r["pipeline"]: yaml.safe_load(r["yaml"])["pipelines"][r["pipeline"]]
            for r in rows}
    nightly = docs["nightly"]
    assert "*" in nightly["scopes"]["brands"], (
        "nightly does not declare a brand scope, so the fan-out writes one hq"
        " row and every brand-scoped page stays empty")
    engines = {spec["engine"] for spec in nightly["jobs"].values()}
    assert BRAND_SURFACES <= engines, (
        f"nightly writes {sorted(engines)}; missing"
        f" {sorted(BRAND_SURFACES - engines)}")
    # Same job in both crons must carry the same params: a nightly run with a
    # different horizon would flip the batch's `horizon_days` every night.
    assert (nightly["jobs"]["forecast"]["params"]
            == docs["weekly"]["jobs"]["forecast"]["params"]), (
        "the nightly and weekly forecast jobs disagree on params")
