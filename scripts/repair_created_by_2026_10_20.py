"""Attribute or remove rows written before `created_by` described a writer.

Ticket #88. The manager's ruling splits the 36 `created_by IS NULL` rows into
two different things, and this script must not merge them again:

1. **Probe-authored rows are artefacts and go.** `created_by IN
   ('provenance-probe', 'cost-probe', 'probe', 'ml-rehearsal', 'crm-ml')` in
   `clv_snapshots`, `churn_snapshots`, `assoc_batches`, `rec_batches`. Nothing
   downstream reads them and a probe in a rendered table is how a rehearsal
   became a red row.
2. **NULL rows in `churn_snapshots`, `rfm_snapshots`, `segment_runs`,
   `explain_batches` are history and stay** -- eleven NULL churn snapshots is
   most of churn's history, so they are back-stamped to the single literal
   `'pre-provenance'`: "written before the column described a writer". A true
   statement, greppable, and NULL stops reading as "history".
3. **The NULL `clv_snapshots` rows are checked one at a time, not as a block.** The
   ruling calls all four fossils; measured 2026-10-20 only **one** is: `as_of
   2026-10-15` declares `customer_count` 505 over 498 rows (the #85 defect). The
   other three declare exactly what they hold (940/940, 940/940, 498/498) --
   deleting them would destroy real history to satisfy a provenance rule, which
   the ruling forbids in the same breath. So: **fossils are deleted, accurate
   NULL clv rows are back-stamped** like the other four tables, and the script
   prints which is which. `--clv-null all` deletes every NULL clv row (the
   ruling as written); the default is the measurement.

`'pre-provenance'` is a back-stamp for history only. No writer may emit it:
ml's `require_producer` keeps refusing every name that is not a real producer,
and `services/api/tests/test_created_by_repair.py` fails if the literal ever
appears in a writer path.

Two rows in `assoc_batches` are NULL. The first ruling named four tables for
back-stamping because ml's sweep listed four; the owner then ruled them in too
-- two real assoc batches are history, and a row that cannot name its author is
a fact about our records, not a reason to erase them.

    python scripts/repair_created_by_2026_10_20.py            # dry run
    python scripts/repair_created_by_2026_10_20.py --apply    # do it

Idempotent and re-runnable: every DML statement matches rows by state
(`created_by IN (probes)` / `created_by IS NULL`), so a second run measures
zero in every bucket and prints "nothing to do". The expected counts are
constants below and a run that measures a different number aborts instead of
repairing something that was not reviewed -- `--allow-drift` is for a cluster
that legitimately changed since the review, and it prints the drift.

Every row removed is written to a JSON dump before the DELETE, children
included, so a wrong guess is recoverable (same convention as
`scripts/clean_test_debris.py`).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib

import asyncpg

DSN = os.environ.get("DATABASE_URL", "postgresql://postgres@127.0.0.1:59531/postgres")

PROBE_AUTHORS = ("provenance-probe", "cost-probe", "probe", "ml-rehearsal", "crm-ml")
BACKSTAMP_LITERAL = "pre-provenance"

# Measured 2026-10-20 on the dev cluster and reviewed by the manager.
EXPECTED_PROBE_DELETE = {
    "clv_snapshots": 5,      # ml-rehearsal 2, cost-probe 2, provenance-probe 1
    "churn_snapshots": 3,    # ml-rehearsal 2, provenance-probe 1
    "assoc_batches": 5,      # probe 3, crm-ml 2
    "rec_batches": 1,        # ml-rehearsal 1
}
EXPECTED_BACKSTAMP = {
    "churn_snapshots": 11,
    "rfm_snapshots": 4,
    "segment_runs": 10,
    "explain_batches": 5,
    "assoc_batches": 2,      # added by the ruling's second pass: history, not debris
}
EXPECTED_CLV_NULL = 4            # measured; 1 fossil + 3 accurate
UNASSIGNED = ()                   # none left: deliver every NULL row it measured

# parent -> (child table, child fk column). All FKs are ON DELETE CASCADE, so
# the parent DELETE removes the children; they are only dumped, children-first.
CHILDREN = {
    "clv_snapshots": ("clv_customer_scores", "snapshot_id"),
    "churn_snapshots": ("churn_customer_scores", "snapshot_id"),
    "rfm_snapshots": ("rfm_customer_scores", "snapshot_id"),
    "segment_runs": ("segment_assignments", "run_id"),
    "assoc_batches": ("assoc_rules", "batch_id"),
    "explain_batches": ("explain_rows", "batch_id"),
    "rec_batches": ("rec_items", "batch_id"),
}


async def measure(conn):
    """{table: {'probe': n, 'null': n}} plus the clv fossil check."""
    out = {}
    for table in sorted(set(EXPECTED_PROBE_DELETE) | set(EXPECTED_BACKSTAMP)
                        | set(UNASSIGNED)):
        probe = await conn.fetchval(
            f"SELECT count(*) FROM {table} WHERE created_by = ANY($1::text[])",
            list(PROBE_AUTHORS))
        null = await conn.fetchval(
            f"SELECT count(*) FROM {table} WHERE created_by IS NULL")
        out[table] = {"probe": probe, "null": null}

    clv = await conn.fetch(
        """
        SELECT s.id, s.as_of, s.customer_count,
               (SELECT count(*) FROM clv_customer_scores c
                 WHERE c.snapshot_id = s.id) AS rows_present
          FROM clv_snapshots s
         WHERE s.created_by IS NULL
         ORDER BY s.as_of
        """)
    fossils = [dict(r) for r in clv if r["customer_count"] != r["rows_present"]]
    keepers = [dict(r) for r in clv if r["customer_count"] == r["rows_present"]]
    return out, fossils, keepers


def drift(measured):
    problems = []
    for table, want in EXPECTED_PROBE_DELETE.items():
        got = measured[table]["probe"]
        if got != want:
            problems.append(f"{table}: probe-authored {got}, expected {want}")
    for table, want in EXPECTED_BACKSTAMP.items():
        got = measured[table]["null"]
        if got != want:
            problems.append(f"{table}: NULL {got}, expected {want}")
    return problems


async def dump_rows(conn, table, where, params):
    rows = await conn.fetch(f"SELECT * FROM {table} WHERE {where}", params)
    payload = {"table": table, "rows": [dict(r) for r in rows]}
    child = CHILDREN.get(table)
    if child and rows:
        child_table, fk = child
        ids = [r["id"] for r in rows]
        kids = await conn.fetch(
            f"SELECT * FROM {child_table} WHERE {fk} = ANY($1::uuid[])", ids)
        payload["children"] = {"table": child_table, "rows": [dict(r) for r in kids]}
    return payload


async def apply(conn, dumps):
    counts = {"deleted": {}, "backstamped": {}, "children_cascaded": {}}
    for table in EXPECTED_PROBE_DELETE:
        result = await conn.execute(
            f"DELETE FROM {table} WHERE created_by = ANY($1::text[])",
            list(PROBE_AUTHORS))
        counts["deleted"][table] = int(result.split()[-1])
        child = CHILDREN.get(table)
        if child:
            counts["children_cascaded"][child[0]] = len(
                dumps[table].get("children", {}).get("rows", []))
    for table in EXPECTED_BACKSTAMP:
        result = await conn.execute(
            f"UPDATE {table} SET created_by = $1 WHERE created_by IS NULL",
            BACKSTAMP_LITERAL)
        counts["backstamped"][table] = int(result.split()[-1])
    return counts


async def log_row(conn, counts, fossils, keepers, blank_left):
    """One row in the run ledger; a re-run APPENDS its counts, never replaces them.

    The first run is the one that deleted 15 batches and 9,474 children; a
    later pass that touches two rows must not overwrite that record -- so
    `runs` is a list and `latest` names the most recent pass.
    """
    entry = {
        "counts": counts,
        "clv_null_deleted": [f["as_of"].isoformat() for f in fossils],
        "clv_null_backstamped": [k["as_of"].isoformat() for k in keepers],
        "backstamp_literal": BACKSTAMP_LITERAL,
        "assoc_null_left_untouched": {t: 0 for t in UNASSIGNED},
    }
    upstream = {
        "repair": "scripts/repair_created_by_2026_10_20.py",
        "ticket": 88,
        "latest": entry,
        "runs": [entry],
    }
    await conn.execute(
        """
        INSERT INTO pipeline_runs (pipeline, job, as_of, status, upstream,
                                   started_at, finished_at)
        VALUES ('repair', 'created_by_2026_10_20',
                (now() AT TIME ZONE interval '8 hours')::date,
                'succeeded', $1::jsonb, now(), now())
        ON CONFLICT (pipeline, job, as_of,
                     COALESCE(brand_id, '00000000-0000-0000-0000-000000000000'::uuid),
                     COALESCE(group_id, '00000000-0000-0000-0000-000000000000'::uuid))
        DO UPDATE SET upstream = jsonb_set(
                        jsonb_set(pipeline_runs.upstream, '{latest}',
                                  EXCLUDED.upstream->'latest'),
                        '{runs}',
                        COALESCE(pipeline_runs.upstream->'runs', '[]'::jsonb)
                          || (EXCLUDED.upstream->'runs')),
                      finished_at = EXCLUDED.finished_at
        """,
        json.dumps(upstream, default=str))


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", default=DSN)
    ap.add_argument("--apply", action="store_true",
                    help="perform the repair (default: measure and report only)")
    ap.add_argument("--allow-drift", action="store_true",
                    help="proceed when the cluster holds a different count than "
                         "the reviewed one (prints the drift either way)")
    ap.add_argument("--clv-null", choices=("fossil-only", "all"),
                    default="fossil-only",
                    help="fossil-only (default): delete the count-mismatched clv "
                         "rows and back-stamp the accurate ones; all: delete "
                         "every created_by IS NULL clv snapshot")
    ap.add_argument("--dump", default=".pi/repair-created-by-2026-10-20.json")
    args = ap.parse_args()

    conn = await asyncpg.connect(args.dsn)
    try:
        measured, fossils, keepers = await measure(conn)
        problems = drift(measured)
        print("measured:")
        for table, m in measured.items():
            print(f"  {table:18} probe-authored={m['probe']:2}  null={m['null']:2}")
        print(f"  clv_snapshots NULL rows: {len(fossils) + len(keepers)}"
              f" ({len(fossils)} fossil, {len(keepers)} count-matching)")
        for f in fossils:
            print(f"    fossil {f['as_of']}: declared {f['customer_count']},"
                  f" rows {f['rows_present']}")
        for k in keepers:
            print(f"    KEPT   {k['as_of']}: declared {k['customer_count']} ="
                  f" rows {k['rows_present']} (history, not a fossil)")
        blank_left = {t: measured[t]["null"] for t in UNASSIGNED}
        to_delete = fossils + (keepers if args.clv_null == "all" else [])
        to_backstamp_clv = [] if args.clv_null == "all" else keepers
        if not any(m["probe"] or (m["null"] and t not in UNASSIGNED)
                   for t, m in measured.items()) and not fossils:
            # Every bucket zero: this repair already ran (or never had debris).
            print("nothing to do")
            return 0
        if problems:
            print("DRIFT from the reviewed counts:")
            for p in problems:
                print(f"  {p}")
            if not args.allow_drift:
                print("aborting: repair with --allow-drift only if the drift is expected")
                return 3

        dumps = {}
        for table in EXPECTED_PROBE_DELETE:
            dumps[table] = await dump_rows(
                conn, table, "created_by = ANY($1::text[])", list(PROBE_AUTHORS))
        if to_delete:
            dumps["clv_snapshots_null"] = await dump_rows(
                conn, "clv_snapshots", "id = ANY($1::uuid[])",
                [f["id"] for f in to_delete])

        if not args.apply:
            print(f"dry run: would delete "
                  f"{sum(EXPECTED_PROBE_DELETE.values())} probe-authored + "
                  f"{len(to_delete)} NULL clv rows, back-stamp "
                  f"{sum(EXPECTED_BACKSTAMP.values()) + len(to_backstamp_clv)} "
                  f"NULL rows to {BACKSTAMP_LITERAL!r}")
            print(f"would dump {len(dumps)} table(s) to {args.dump}")
            return 0

        path = pathlib.Path(args.dump)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(dumps, default=str, indent=2), encoding="utf-8")

        async with conn.transaction():
            if to_delete:
                ids = [f["id"] for f in to_delete]
                await conn.execute(
                    "DELETE FROM clv_snapshots WHERE id = ANY($1::uuid[])", ids)
            counts = await apply(conn, dumps)
            if to_backstamp_clv:
                result = await conn.execute(
                    "UPDATE clv_snapshots SET created_by = $1 "
                    "WHERE created_by IS NULL", BACKSTAMP_LITERAL)
                counts["backstamped"]["clv_snapshots"] = int(result.split()[-1])
            counts["deleted"]["clv_snapshots_null"] = len(to_delete)
            await log_row(conn, counts, to_delete, to_backstamp_clv, blank_left)
        print("applied:", json.dumps(counts, default=str))
        print(f"rows removed were dumped to {path}")
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
