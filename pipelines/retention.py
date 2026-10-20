"""Age-based housekeeping for the derivation ledgers (the 365-day rule).

Measured before this was written (live cluster, 2026-10-19): the public schema
was 43 MB, one nightly tick wrote 18,487 rows (~3.2 MB), and at 100x the demo
customer base that rate projects to ~115 GB/year. At today's size nothing needs
deleting -- this exists so the first 100x-scale `DELETE` is not one 20 GB
transaction, and so no table is forgotten by omission.

Three properties, each one a deliberate decision:

* **After the fan-out, never before.** Housekeeping ahead of the busy part
  holds IO and locks exactly when the night's scores need them.
* **Its failure never fails the tick.** A retention bug must not cost a night
  of analytics; the error is reported in the run summary where someone sees it.
* **Capped per parent per tick.** `ctid IN (SELECT ... LIMIT n)` keeps one
  transaction bounded; the rest waits for the next tick.

Deletion is one statement per *parent*: every child row hangs off its batch /
run / snapshot with `ON DELETE CASCADE` (verified: deleting one
`churn_snapshots` row removed its 505 `churn_customer_scores`), so `CASCADE` is
a list of things never targeted directly rather than a list of things to clean.
One child can hang off two parents: `explain_batches` dies when its
`segment_runs` run ages *or* when its own `created_at` ages, and those are not
the same clock, so an explain batch's life is the shorter of the two.

`rfm_snapshots` has no index on `scored_at` and does not need one: 75 rows and
about +14 a night, and the planner already reports `Index Cond: (scored_at <
...)` off the existing `(mode, scored_at)` index. Do not "fix" that by buying
an index for a 75-row table.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# One window, and it is the read surface's own: `rfm_segment_movement` compares
# windows across a year and the explain pages replay a segment run, so 365 is
# what the product reads, not a round number.
RETENTION_DAYS = 365

# Bounds one transaction. At the measured 18.5k rows/night this is never hit
# today; on a first run over years of history it turns a 20 GB delete into
# however many ticks it takes.
CAP_PER_PARENT = 200_000

# table -> time column. Aged by the analytic key where one exists (it leads the
# unique index, so the delete is an index scan), else by write time.
AGED: dict[str, str] = {
    "churn_snapshots": "as_of",
    "clv_snapshots": "as_of",
    "segment_runs": "as_of",
    "forecast_batches": "as_of",
    "rfm_snapshots": "scored_at",
    "rfm_segment_movement": "as_of",
    "rfm_segment_sizes": "as_of",
    "btyd_params": "as_of",
    "assoc_batches": "created_at",
    "explain_batches": "created_at",
    "rec_batches": "created_at",
    "cohort_snapshots": "created_at",
    "backtest_outcomes": "created_at",
    "pipeline_runs": "created_at",
}

# Children: reachable only through their parent's cascade, never targeted.
CASCADE: dict[str, str] = {
    "churn_customer_scores": "churn_snapshots",
    "clv_customer_scores": "clv_snapshots",
    "rfm_customer_scores": "rfm_snapshots",
    "segment_assignments": "segment_runs",
    "rec_items": "rec_batches",
    "explain_rows": "explain_batches",
    "forecast_rows": "forecast_batches",
    "assoc_rules": "assoc_batches",
    "quarantine_rows": "upload_jobs",
    # ml artifacts that hang off a segment run (verified CASCADE at the FK);
    # they are never aged directly, they leave with the run.
    "cluster_feature_matrix": "segment_runs",
    "cluster_feature_matrix_long": "segment_runs",
    "seg_surrogate_fits": "segment_runs",
}

# Everything else, with the reason it is out of scope. Enumerated rather than
# commented out: a table that is in neither set fails the test, so a new table
# cannot be silently forgotten and a later edit cannot quietly age
# `customer_access_logs` or `consent_events` (append-only by policy).
NEVER: dict[str, str] = {
    # business records -- archival is a different conversation, never a tick
    "orders": "business record",
    "order_items": "business record",
    # the capture path of the recommendation attribution (migration 053): a
    # token names what was served, and a token that no longer resolves is a
    # disagreement that quarantines the order line - so the token has to
    # outlive every order that can still name it. Aging it would quarantine a
    # valid order, which is a worse loss than the rows it saves. The growth
    # this table carries is the price of a token that stays resolvable; a
    # token-expiry policy on the serving side is what would make it ageable.
    "rec_clicks": "serving ledger (a token must stay resolvable)",
    "order_recommendations": "business fact (the money a recommendation earned)",
    "customers": "master data, soft-deleted",
    "customer_contacts": "master data",
    "customer_events": "ingestion ledger",
    "consents": "master data",
    "consent_events": "append-only by policy",
    "customer_access_logs": "append-only audit by policy",
    "landing_receipts": "ingestion ledger",
    "staging_rows": "ingestion ledger",
    "upload_jobs": "ingestion ledger (its quarantine rows cascade)",
    "connector_sync_runs": "ingestion ledger",
    "connectors": "configuration",
    # deterministic near-static state
    "brands": "master data",
    "groups": "master data",
    "stores": "master data",
    "products": "master data",
    "inventory": "master data",
    "membership_tiers": "master data",
    "staff": "master data",
    "users": "master data",
    "sessions": "auth state",
    "reference_values": "configuration",
    "pipeline_manifests": "configuration (versioned rows are history)",
    "schema_migrations": "migration ledger",
    "report_definitions": "configuration",
    "campaigns": "master data",
    "campaign_members": "campaign membership",
    # custom-field payloads: bounded by the rows they decorate
    "customer_custom_data": "master data payload",
    "product_custom_data": "master data payload",
    "order_custom_data": "business record payload",
    "event_custom_data": "ingestion ledger payload",
    # ml artifacts whose ledger rows are the provenance and are small
    "model_training_runs": "provenance ledger (cited by id in announcements)",
    "kpi_daily_snapshots": "daily KPI ledger",
    "report_runs": "report ledger",
}

ALL_TABLES: dict[str, str] = {**AGED, **CASCADE, **NEVER}

# The aged tables whose time column is an analytic anchor rather than a write
# timestamp: a row dated after the tick's own day is a rehearsal, and it wins
# every `as_of DESC` reader (`created_at` cannot be in the future, `as_of` can).
# Tables whose rows are *dated* by a business anchor, and which column carries
# it. Written out, never derived from `AGED`: a derived list inherits the purpose
# of its source, and these are two different questions -- AGED asks "what ages
# out, and by which timestamp", this asks "what must never be dated ahead".
# `pipeline_runs` is the case that proves it: it ages out by `created_at` and is
# therefore unreachable from a name filter over AGED, yet its `as_of` is the
# column whose future value is the worst failure in the set -- a `succeeded`
# claim for a day that has not happened makes the real tick skip the job and
# leaves the page empty instead of wrong. Caught by the drift test below,
# which compares this map against the catalog rather than against AGED.
FUTURE_DATED: dict[str, str] = {
    "btyd_params": "as_of",
    "churn_snapshots": "as_of",
    "clv_snapshots": "as_of",
    "forecast_batches": "as_of",
    "pipeline_runs": "as_of",
    "rfm_segment_movement": "as_of",
    "rfm_segment_sizes": "as_of",
    "rfm_snapshots": "scored_at",
    "segment_runs": "as_of",
}

# Deliberately NOT here, with the measurement that decided it: `orders`.
#
# `orders.ordered_at` is an anchor the KPI and cohort buckets key on, so it
# looks like it belongs. It is not in this map because a future value is legal
# for one status: a *pending* (scheduled / pre-ordered) order is dated ahead on
# purpose, and `test_apply.py`'s `E-SCHEDULED` case (2099, `pending`, kept by
# design) is the row that would make this guard fire on correct data. The
# completed-order hole it was raised for is already closed at ingest:
# `pipelines/upload.py` quarantines a future-dated `completed` order as
# `future_dated` (migration 043) rather than storing it, which is a better
# place for the rule -- it names the row, not just the count. Re-adding `orders`
# here without that status distinction turns the nightly tick red on every
# scheduled order in the book.
FUTURE_DATED_EXTRA: frozenset[str] = frozenset()

# Anchors stored as an INSTANT, not a day. A `> today` bound is wrong for them:
# `today` is a date and `timestamptz > date` promotes the date to a timestamp at
# the session's zone, so the bound lands at the *start* of the as_of day and
# every row the as_of tick writes (00:04 the same night) counts as "future".
# Measured 2026-10-20 on the dev cluster: 28 clean rfm rows written by the
# 2026-10-20 tick were named as future-dated. The correct bound is the next
# business midnight, exclusive -- the same instant form crm-cdp-app's
# `test_observed_anchor.py` pins.
FUTURE_DATED_INSTANT: frozenset[str] = frozenset({"rfm_snapshots"})

# Periods a row COVERS, as (table, column) rather than one column per table: a
# single row can carry both ends of a window, and both are claims about which
# days exist. The instant map above could not see these -- the 2026-10-19 script
# wrote 42 `rfm_snapshots` whose `scored_at` was *today* (so the instant guard
# reported nothing) and whose `window_to` was four days ahead, and every brand's
# page served that future window because it orders by `window_to DESC`. A future
# `as_of` was already refused at dispatch; a future *period* had no guard at all.
FUTURE_DATED_PERIOD: frozenset[tuple[str, str]] = frozenset({
    ("kpi_daily_snapshots", "snapshot_date"),
    ("rfm_snapshots", "window_from"),
    ("rfm_snapshots", "window_to"),
})

# The same question for period bounds stored as an INSTANT (ml's backtest
# windows, `timestamptz`): the next-midnight rule from FUTURE_DATED_INSTANT
# applies per column here, because a plain `> $1::date` would flag the window
# that closes today -- the bug 872ceb6 fixed, one table over. No allowlist: a
# backtest window ahead of today measures days that do not exist.
FUTURE_DATED_PERIOD_INSTANT: frozenset[tuple[str, str]] = frozenset({
    ("backtest_outcomes", "window_from"),
    ("backtest_outcomes", "window_to"),
})

# Deliberately future, and the reason is the whole entry: without it, the sweep
# "everything dated ahead is a rehearsal" would be false on this table and the
# guard would be noise, which is the state that gets it switched off.
FUTURE_PERIOD_ALLOWED: dict[tuple[str, str], str] = {
    ("forecast_rows", "period_start"):
        "a forecast is FOR future weeks -- the coming days are the payload, "
        "not a claim about the past",
}

# The one literal for the business day, next to the reason it exists; the
# `date`-typed anchors need no zone (a day is a day).
BUSINESS_DAY = "interval '8 hours'"


async def future_rows(conn, today) -> dict[str, int]:
    """Rows dated after `today`, per anchored table.

    Detection, not prevention: the writers refuse a future `as_of` (the tick in
    `guard_as_of`, ml's CLI before it connects), so a non-zero count here means
    a path nobody owns yet wrote one -- and the tick must say so out loud,
    naming the table and the count, rather than leaving the row to outrank the
    live number on a page.

    `today` is a business day (`date`). Both predicates cast it explicitly: a
    bare `$1` is an unknown whose parse depends on the session zone, which is
    how the instant case drifted a day.
    """
    found: dict[str, int] = {}
    for table, column in FUTURE_DATED.items():
        if table in FUTURE_DATED_INSTANT:
            sql = (f'SELECT count(*) FROM "{table}" WHERE "{column}" >= '
                   f"((($1::date) + 1)::timestamp AT TIME ZONE {BUSINESS_DAY})")
        else:
            sql = (f'SELECT count(*) FROM "{table}" '
                   f'WHERE "{column}" > $1::date')
        n = await conn.fetchval(sql, today)
        if n:
            found[table] = int(n)
    # `table.column` keys so a period bound is distinguishable from the instant
    # anchor of the same table (`rfm_snapshots` appears in both maps).
    for table, column in sorted(FUTURE_DATED_PERIOD):
        if (table, column) in FUTURE_PERIOD_ALLOWED:
            continue
        n = await conn.fetchval(
            f'SELECT count(*) FROM "{table}" WHERE "{column}" > $1::date',
            today)
        if n:
            found[f"{table}.{column}"] = int(n)
    for table, column in sorted(FUTURE_DATED_PERIOD_INSTANT):
        bound = f"((($1::date) + 1)::timestamp AT TIME ZONE {BUSINESS_DAY})"
        n = await conn.fetchval(
            f'SELECT count(*) FROM "{table}" WHERE "{column}" >= {bound}',
            today)
        if n:
            found[f"{table}.{column}"] = int(n)
    return found

def _delete_sql(table: str, column: str) -> str:
    """The capped age-delete for one parent.

    Built per call rather than held as a module constant: the drift test
    PREPAREs every module-level SQL string against the database, and a
    template with `{table}` placeholders is not a statement -- `predicates()`
    below is what makes each concrete one resolve (the EXPLAIN test runs them
    all).
    """
    return (f"DELETE FROM {table} WHERE ctid IN ("
            f" SELECT ctid FROM {table}"
            f" WHERE {column} < now() - make_interval(days => $1::int)"
            f" LIMIT $2::int)")


def predicates(days: int = RETENTION_DAYS) -> dict[str, str]:
    """The exact statement each parent gets, for a test to EXPLAIN."""
    return {
        table: (f"SELECT ctid FROM {table}"
                f" WHERE {column} < now() - make_interval(days => {days})"
                f" LIMIT {CAP_PER_PARENT}")
        for table, column in AGED.items()
    }


async def prune(conn, days: int = RETENTION_DAYS,
                cap: int = CAP_PER_PARENT) -> dict[str, int]:
    """Delete rows older than `days` per parent, capped; return per-table counts."""
    deleted: dict[str, int] = {}
    for table, column in AGED.items():
        try:
            tag = await conn.execute(_delete_sql(table, column), days, cap)
        except Exception:  # noqa: BLE001 -- one parent failing must not stop the rest
            log.warning("retention: %s failed", table, exc_info=True)
            deleted[table] = -1
            continue
        # `execute` returns the command tag (`DELETE 3`); `fetchval` on a
        # DELETE without RETURNING returns None and would report every sweep
        # as zero rows deleted -- which is how this bug showed up.
        n = int(tag.rsplit(" ", 1)[-1] or 0)
        if n:
            deleted[table] = n
    return deleted
