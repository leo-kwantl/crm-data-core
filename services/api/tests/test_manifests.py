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


async def test_the_scopes_block_names_the_scopes_the_tick_dispatches(database_url: str):
    """057: the manifest's `scopes:` block names what the tick really does.

    The block promised a group leg -- `groups: ['*']` -- that the tick never
    offered any engine: `tick_scopes` returns one `(brand_id, NULL)` scope
    per brand and then the `(NULL, NULL)` hq roll-up, so a group scope was a
    scope no writer had ever been asked for. The block is now the truth about
    the dispatch, which is only worth anything if it agrees with the
    dispatcher -- so the test reads both.
    """
    import asyncpg
    import yaml

    from pipelines import schedule as sched

    pool = await asyncpg.create_pool(database_url)
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT pipeline, yaml FROM pipeline_manifests"
                " WHERE pipeline IN ('nightly', 'weekly')")
            dispatched = await sched.tick_scopes(conn)
            n_brands = await conn.fetchval("SELECT count(*) FROM brands")
    finally:
        await pool.close()

    docs = {r["pipeline"]: yaml.safe_load(r["yaml"])["pipelines"][r["pipeline"]]
            for r in rows}
    assert docs, "no manifest row to check"
    for name, doc in docs.items():
        assert doc["scopes"] == {"brands": ["*"], "hq": ["*"]}, (
            f"{name} declares scopes {doc['scopes']}; the block must name the"
            f" scopes the tick dispatches -- every brand and the hq roll-up --"
            f" and no group")
    # The block and the dispatcher must agree, or the manifest is a promise
    # about something the tick does not do.
    brand_legs = [b for b, g in dispatched if b is not None]
    assert all(g is None for _, g in dispatched), (
        f"the tick dispatches a group leg: {dispatched}; a group scope is the"
        f" pair (brand_id, group_id), and no writer produces one")
    assert len(brand_legs) == n_brands, (
        f"the tick dispatches {len(brand_legs)} brand legs for {n_brands}"
        f" brands; `brands: ['*']` promises every brand")
    assert dispatched[-1] == (None, None), "the hq roll-up must be last"

    # A re-run of the migration must change nothing: it is gated on the old
    # block's presence, and after the rewrite that block is gone.
    from services.api.tests.conftest import MIGRATIONS_DIR, UP_RE

    text = (MIGRATIONS_DIR / "057_manifest_scopes_brands_hq.sql").read_text(
        encoding="utf-8")
    up = UP_RE.search(text).group(1).strip()
    pool = await asyncpg.create_pool(database_url)
    try:
        async with pool.acquire() as conn:
            before = await conn.fetch(
                "SELECT pipeline, yaml, version FROM pipeline_manifests"
                " ORDER BY pipeline")
            await conn.execute(up)
            after = await conn.fetch(
                "SELECT pipeline, yaml, version FROM pipeline_manifests"
                " ORDER BY pipeline")
    finally:
        await pool.close()
    assert [dict(r) for r in before] == [dict(r) for r in after], (
        "re-applying 057 changed the manifests; the migration is not a no-op"
        " on its second run")


async def test_the_future_rows_guard_waits_for_every_other_nightly_job(
        database_url: str):
    """049's contract, derived: the guard runs last, so it waits for all.

    049's design -- "`future_rows_guard` runs last (it depends on every other
    nightly job) ... so a new writer shows up as a red tick on the night it
    lands". 051 added the `kpi` writer to the nightly manifest and did not
    add it to the guard's list, so a future-dated `kpi_daily_snapshots` row
    could land AFTER the guard had passed and the guard would stay green --
    the one failure 049 was written to end. 058 re-states the list.

    The contract is DERIVED, not hardcoded: a job added to the manifest
    breaks this test instead of rotting it.
    """
    import asyncpg
    import yaml

    pool = await asyncpg.create_pool(database_url)
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT pipeline, yaml FROM pipeline_manifests"
                " WHERE pipeline = 'nightly'")
    finally:
        await pool.close()
    assert rows, "no nightly manifest row to check"
    nightly = yaml.safe_load(rows[0]["yaml"])["pipelines"]["nightly"]
    jobs = nightly["jobs"]
    assert set(jobs["future_rows"]["depends_on"]) == set(jobs) - {"future_rows"}, (
        "the future_rows guard waits for "
        f"{sorted(jobs['future_rows']['depends_on'])}; it must wait for every"
        f" other nightly job ({sorted(set(jobs) - {'future_rows'})}) or a"
        " writer it does not wait for can leave a future-dated row after the"
        " guard has passed")

    # A re-run of the migration must change nothing: it is gated on the
    # guard's list still having the shape 055 left, and after the rewrite
    # that shape is gone.
    from services.api.tests.conftest import MIGRATIONS_DIR, UP_RE

    text = (MIGRATIONS_DIR / "058_manifest_future_rows_waits_for_kpi.sql").read_text(
        encoding="utf-8")
    up = UP_RE.search(text).group(1).strip()
    pool = await asyncpg.create_pool(database_url)
    try:
        async with pool.acquire() as conn:
            before = await conn.fetch(
                "SELECT pipeline, yaml, version FROM pipeline_manifests"
                " ORDER BY pipeline")
            await conn.execute(up)
            after = await conn.fetch(
                "SELECT pipeline, yaml, version FROM pipeline_manifests"
                " ORDER BY pipeline")
    finally:
        await pool.close()
    assert [dict(r) for r in before] == [dict(r) for r in after], (
        "re-applying 058 changed the manifests; the migration is not a no-op"
        " on its second run")
