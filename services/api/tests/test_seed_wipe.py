"""Reseed wipe-list guard (scripts/seed_reseed_1200.py).

Regression for the "torn cohort ladder" (2026-10-15): `cohort_snapshots`
was missing from `ANALYTICS_TABLES`, so `--wipe-analytics` left the
previous dataset's snapshot rows in place and the app's /cohort page
mixed two datasets (stray legacy months + a member count that matched
neither dataset).

Rule enforced here: every migration-created table whose name looks like
job output must be consciously classified -- either regenerable (listed
in ANALYTICS_TABLES, so a reseed clears it) or explicitly kept in
NEVER_WIPE. A new snapshot/batch table that lands in neither fails.

Pure unit (no DB): reads the constants out of the seeder with `ast`.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SEEDER = REPO / "scripts" / "seed_reseed_1200.py"
BOOTSTRAP = REPO / "scripts" / "bootstrap_db.py"
MIGRATIONS = REPO / "db" / "migrations"

# Substrings that mark a table as (probably) regenerable job output.
JOB_OUTPUT_HINTS = ("snapshot", "batch", "score", "item", "rule", "row",
                    "assignment", "matrix", "fit", "param", "receipt",
                    "movement", "size", "run", "job", "staging",
                    "quarantine", "manifest")

# Reference / dataset tables: bootstrap_db.py owns them (TRUNCATE
# ... CASCADE), so they are neither analytics nor job output.
DATASET_TABLES = {
    "brands", "campaign_members", "campaigns", "consent_events",
    "consents", "customer_access_logs", "customer_contacts",
    "customer_custom_data", "customer_events", "customers",
    "event_custom_data", "groups", "inventory", "membership_tiers",
    "order_custom_data", "order_items", "orders", "product_custom_data",
    "products", "reference_values", "staff", "stores", "users",
}

CREATE_TABLE_RE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([a-z_][a-z0-9_]*)",
    re.I)


def _module_lists() -> tuple[list[str], list[str]]:
    """Read ANALYTICS_TABLES / NEVER_WIPE literals out of the seeder."""
    tree = ast.parse(SEEDER.read_text(encoding="utf-8"))
    out: dict[str, list[str]] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for tgt in node.targets:
            if isinstance(tgt, ast.Name) and tgt.id in (
                    "ANALYTICS_TABLES", "NEVER_WIPE"):
                out[tgt.id] = list(ast.literal_eval(node.value))
    assert set(out) == {"ANALYTICS_TABLES", "NEVER_WIPE"}, sorted(out)
    return out["ANALYTICS_TABLES"], out["NEVER_WIPE"]


def _migration_tables() -> set[str]:
    """Tables created by the migrations plus the bootstrap ledger."""
    tables: set[str] = set()
    for path in [*sorted(MIGRATIONS.glob("*.sql")), BOOTSTRAP]:
        tables.update(m.group(1).lower()
                      for m in CREATE_TABLE_RE.finditer(
                          path.read_text(encoding="utf-8")))
    assert "cohort_snapshots" in tables, "migration scan found nothing"
    assert "customers" in tables
    assert "schema_migrations" in tables, "bootstrap scan found nothing"
    return tables


def test_wipe_lists_are_real_tables_and_disjoint() -> None:
    analytics, never = _module_lists()
    tables = _migration_tables()
    unknown = sorted(set(analytics) - tables)
    assert unknown == [], (
        f"ANALYTICS_TABLES names no known table: {unknown}")
    unknown = sorted(set(never) - tables)
    assert unknown == [], f"NEVER_WIPE names no migration table: {unknown}"
    both = sorted(set(analytics) & set(never))
    assert both == [], f"table in both wipe lists: {both}"
    assert len(analytics) == len(set(analytics)), "duplicate wipe entry"


def test_every_job_output_table_is_classified() -> None:
    analytics, never = _module_lists()
    classified = set(analytics) | set(never) | DATASET_TABLES
    unclassified = sorted(
        t for t in _migration_tables()
        if any(h in t for h in JOB_OUTPUT_HINTS) and t not in classified)
    assert unclassified == [], (
        "new job-output table(s) need a decision: add to ANALYTICS_TABLES "
        f"(regenerable -> reseed clears it) or NEVER_WIPE: {unclassified}")


def test_dataset_tables_are_not_wiped_by_the_seed() -> None:
    analytics, never = _module_lists()
    overlap = sorted(DATASET_TABLES & (set(analytics) | set(never)))
    assert overlap == [], (
        f"dataset tables must not be in a reseed wipe list: {overlap}")


@pytest.mark.parametrize("table", [
    # The tables whose absence caused the torn-ladder bug.
    "cohort_snapshots", "kpi_daily_snapshots", "clv_snapshots",
    "churn_snapshots", "rfm_snapshots",
    # App /cohort + KPI readers depend on these being dataset-scoped.
    "clv_customer_scores", "churn_customer_scores", "rfm_customer_scores",
])
def test_snapshot_tables_are_wiped(table: str) -> None:
    analytics, _ = _module_lists()
    assert table in analytics
