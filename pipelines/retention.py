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
