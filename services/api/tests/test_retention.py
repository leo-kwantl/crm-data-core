"""The 365-day retention rule: classified, indexed, capped, and never fatal.

The rule itself is `pipelines/retention.py`; these are the four properties a
delete that runs unattended needs, each expressed as something that fails when
it stops being true:

* every table is classified (a new table cannot be silently forgotten, and the
  append-only tables cannot be quietly aged by a later edit),
* every delete predicate is served by an index that **leads with the aged
  column** (`enable_seqscan = off`, and the column must appear in an
  `Index Cond`) — "an index exists" and "the planner can use it" are
  different answers, and only the second survives a 100x year,
* nothing younger than the window is touched, and one parent never deletes more
  than the cap in one tick,
* a failing parent is reported and skipped instead of aborting the sweep.
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

import asyncpg

from pipelines import retention

REPO = Path(__file__).resolve().parents[3]


def _run(coro):
    return asyncio.run(coro)


async def _connect(database_url: str):
    return await asyncpg.connect(database_url)


def test_every_public_table_is_classified(database_url: str) -> None:
    async def _tables():
        conn = await _connect(database_url)
        try:
            rows = await conn.fetch(
                "SELECT c.relname FROM pg_class c"
                " JOIN pg_namespace n ON n.oid = c.relnamespace"
                " WHERE c.relkind = 'r' AND n.nspname = 'public'"
                " ORDER BY 1")
            return {r["relname"] for r in rows}
        finally:
            await conn.close()

    live = _run(_tables())
    # The migrations are the schema's source of truth, and they carry the tables
    # a future change adds: a table declared in a migration but missing from the
    # test database (dev-only DDL, a gated branch) still has to be classified.
    declared = set(_declared_tables())
    unknown = (live | declared) - set(retention.ALL_TABLES)
    assert not unknown, (
        f"table(s) in neither the delete set nor the never-delete set: "
        f"{sorted(unknown)} -- decide, do not let a new table be forgotten")
    # A delete or cascade target that is absent is a silent no-op sweep; a
    # never-delete entry that is absent is only a stale declaration (dev-only
    # tables such as `report_runs` live in the bootstrap, not in the migrations
    # this fixture replays).
    missing_targets = (set(retention.AGED) | set(retention.CASCADE)) - live
    assert not missing_targets, (
        f"retention targets absent from the schema: {sorted(missing_targets)}")


def _declared_tables() -> list[str]:
    """Table names any migration creates, read from the files themselves."""
    import re
    pattern = re.compile(
        r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([a-z_][a-z0-9_]*)",
        re.I)
    names: set[str] = set()
    for path in sorted((REPO / "db" / "migrations").glob("*.sql")):
        names.update(pattern.findall(path.read_text(encoding="utf-8")))
    return sorted(names)


def test_each_predicate_uses_an_index(database_url: str) -> None:
    """Every age predicate must be led by the aged column, not merely indexed.

    "No Seq Scan under `enable_seqscan = off`" is too weak: with any index on
    the table the planner will happily Bitmap-Heap scan a different leading
    column and the test passes -- the failure that only bites once the table is
    big, which is the whole reason this sweep exists. So the plan has to show
    `Index Cond: (<aged column> < ...)`. That is also what separates a real
    index from a size accident: `rfm_snapshots` has no `scored_at` index of its
    own and still satisfies this (its `(mode, scored_at)` index leads far
    enough), while a table whose only index is on some other column fails.
    """
    async def _plans():
        conn = await _connect(database_url)
        try:
            out = {}
            for table, query in retention.predicates().items():
                await conn.execute("SET enable_seqscan = off")
                rows = await conn.fetch("EXPLAIN (FORMAT TEXT) " + query)
                await conn.execute("SET enable_seqscan = on")
                out[table] = "\n".join(r[0] for r in rows)
            return out
        finally:
            await conn.close()

    plans = _run(_plans())
    bad = {t: p for t, p in plans.items() if "Seq Scan" in p}
    assert not bad, (
        "retention predicates that would scan the whole table: "
        + ", ".join(f"{t} ({p.splitlines()[-1].strip()[:60]})"
                    for t, p in bad.items()))

    # The aged column must appear in an `Index Cond`, which is the planner
    # saying "this index decided the row set". `Index Cond: (created_at < ...)`
    # is the shape; a filter that only appears as `Filter:` is a post-scan
    # filter and does not count.
    aged = {t: retention.AGED[t] for t in plans}
    unled = {t: p for t, p in plans.items()
             if not re.search(rf"Index Cond:.*\b{re.escape(aged[t])}\b", p)}
    assert not unled, (
        "index does not lead with the aged column (indexed, but not by age): "
        + ", ".join(f"{t} needs `{aged[t]}` in an Index Cond" for t in unled))


def test_prune_keeps_the_window_and_reports_counts(database_url: str) -> None:
    old = "retention-old"
    new = "retention-new"

    async def _work():
        conn = await _connect(database_url)
        try:
            await conn.execute(
                "DELETE FROM pipeline_runs WHERE pipeline LIKE $1",
                "retention-%")
            await conn.execute(
                "INSERT INTO pipeline_runs (pipeline, job, as_of, created_at)"
                " VALUES ($1, 'j', current_date - $2::int,"
                "         now() - make_interval(days => $2::int))", old, 400)
            await conn.execute(
                "INSERT INTO pipeline_runs (pipeline, job, as_of, created_at)"
                " VALUES ($1, 'j', current_date, now())", new)
            counts = await retention.prune(conn)
            left = {r["pipeline"] for r in await conn.fetch(
                "SELECT pipeline FROM pipeline_runs WHERE pipeline LIKE $1",
                "retention-%")}
            await conn.execute(
                "DELETE FROM pipeline_runs WHERE pipeline LIKE $1",
                "retention-%")
            return counts, left
        finally:
            await conn.close()

    counts, left = _run(_work())
    assert old not in left, "a row older than the window survived"
    assert new in left, "a row inside the window was deleted"
    assert counts.get("pipeline_runs") == 1, counts


def test_one_parent_is_capped_per_tick(database_url: str) -> None:
    async def _work():
        conn = await _connect(database_url)
        try:
            await conn.execute(
                "DELETE FROM pipeline_runs WHERE pipeline LIKE $1",
                "retention-cap-%")
            await conn.executemany(
                "INSERT INTO pipeline_runs (pipeline, job, as_of, created_at)"
                " VALUES ($1, 'j', current_date - 400,"
                "         now() - interval '400 days')",
                [(f"retention-cap-{i}",) for i in range(7)])
            counts = await retention.prune(conn, cap=3)
            left = await conn.fetchval(
                "SELECT count(*) FROM pipeline_runs WHERE pipeline LIKE $1",
                "retention-cap-%")
            await conn.execute(
                "DELETE FROM pipeline_runs WHERE pipeline LIKE $1",
                "retention-cap-%")
            return counts, left
        finally:
            await conn.close()

    counts, left = _run(_work())
    assert counts.get("pipeline_runs") == 3, counts
    assert left == 4, f"cap 3 should leave 4 of 7 rows, {left} left"


def test_a_failing_parent_is_reported_not_raised() -> None:
    class Boom:
        async def execute(self, *a, **k):
            raise RuntimeError("boom")

    counts = _run(retention.prune(Boom()))
    assert counts, "the sweep reports every parent it attempted"
    assert set(counts.values()) == {-1}, (
        "a failing parent must be marked -1 and not stop the sweep")


def test_future_dated_covers_every_anchor_column_in_the_catalog(
        database_url: str) -> None:
    """The dated set is written out, and the catalog is what checks it.

    Deriving it from `AGED` was the bug this test exists for: AGED picks one
    column per table for retention (`pipeline_runs` -> `created_at`), so a name
    filter silently dropped `pipeline_runs.as_of` -- the single column whose
    future value is the worst failure in the set. The map is now literal, and
    this compares it against `information_schema` instead of against AGED, so
    the two lists can only agree by both being right.
    """
    async def _anchors():
        conn = await _connect(database_url)
        try:
            rows = await conn.fetch(
                "SELECT table_name, column_name FROM information_schema.columns"
                " WHERE table_schema = 'public'"
                "   AND column_name IN ('as_of', 'scored_at')"
                " ORDER BY 1")
            return {r["table_name"]: r["column_name"] for r in rows}
        finally:
            await conn.close()

    catalog = _run(_anchors())
    assert catalog, "no as_of/scored_at column found -- the query, not the map"
    missing = {t: c for t, c in catalog.items()
               if retention.FUTURE_DATED.get(t) != c}
    assert not missing, (
        f"table(s) dated by an anchor the guard never reads: {missing} "
        f"-- add each to FUTURE_DATED with the column it is dated by")
    stray = set(retention.FUTURE_DATED) - set(catalog) \
        - set(retention.FUTURE_DATED_EXTRA)
    assert not stray, (
        f"FUTURE_DATED names {sorted(stray)}, which carry no as_of/scored_at "
        f"and are not in FUTURE_DATED_EXTRA -- a typo here would make the "
        f"guard count a column that does not exist")
