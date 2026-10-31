#!/usr/bin/env python
"""SCV review gate: a feature-bearing DDL change must carry an SCV decision.

Every migration under db/migrations/ that creates a table or adds a column
on a feature-bearing table must do one of:
  1. touch customer_single_view itself (a column and its refresh logic), or
  2. carry a `-- scv-review: <decision>` marker line, e.g.
       -- scv-review: waived, pipeline watermark only, no customer attribute
       -- scv-review: covered, recommendations column added in this file
The marker is the review record: "no new field needed" is a decision too,
and it must be written down, not assumed.

Run with no args in a pre-commit hook (inspects staged files). Pass explicit
paths to check anything else (used by the self-test below and by reviewers).
Exit 0 = all clear, 1 = a file needs an SCV decision.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MIGRATIONS = REPO / "db" / "migrations"

# Tables whose columns are (or feed) customer attributes: the SCV reads them
# directly, or an ml job turns them into scores the SCV embeds. A change here
# is a candidate new SCV field until the marker says otherwise.
FEATURE_TABLES = frozenset({
    "customers", "customer_contacts", "consents", "consent_events",
    "customer_custom_data", "customer_access_logs",
    "orders", "order_items", "products", "stores", "brands", "groups",
    "reference_values", "tiers",
    "rfm_snapshots", "rfm_customer_scores",
    "clv_snapshots", "clv_customer_scores",
    "churn_snapshots", "churn_customer_scores",
    "segment_runs", "segment_assignments",
    "rec_batches", "rec_items", "assoc_batches", "assoc_rules",
    "explain_batches", "explain_rows", "forecast_batches", "forecast_rows",
    "cohort_snapshots", "kpi_daily_snapshots", "btyd_params",
})

# Pure machinery: new tables/columns here can never become customer
# attributes, so they pass without a marker. Anything NOT listed in either
# set is treated as feature-bearing (a new table is a feature until proven
# machinery) — add it here deliberately, with the reason, if it is plumbing.
INFRA_TABLES = frozenset({
    "schema_migrations", "pipeline_runs", "pipeline_manifests",
    "upload_jobs", "staging_rows", "quarantine_rows",
    "connectors", "connector_sync_state", "model_training_runs",
})

CREATE_RE = re.compile(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)", re.I)
ADD_COL_RE = re.compile(r"ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(\w+).*?ADD\s+COLUMN", re.I | re.S)
MARKER_RE = re.compile(r"--\s*scv-review\s*:\s*(.+)", re.I)
SCV_RE = re.compile(r"customer_single_view", re.I)


def check_file(path: Path) -> list[str]:
    """Return a list of violations (empty = clear)."""
    text = path.read_text(encoding="utf-8")
    up = text.split("-- migrate:down")[0]
    if SCV_RE.search(up):
        return []
    marker = MARKER_RE.search(up)
    if marker and marker.group(1).strip():
        return []
    targets = set(CREATE_RE.findall(up)) | set(ADD_COL_RE.findall(up))
    feature = {t for t in targets
               if t in FEATURE_TABLES or t not in INFRA_TABLES}
    if not feature:
        return []
    return [f"{path.name}: touches {sorted(feature)} with no SCV decision "
            f"(add the column+refresh to customer_single_view, or record "
            f"`-- scv-review: <decision>` in the file)"]


def staged_migrations() -> list[Path]:
    out = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM",
         "--", "db/migrations/"],
        cwd=REPO, capture_output=True, text=True)
    return [REPO / p for p in out.stdout.split() if p.endswith(".sql")]


def main(argv: list[str]) -> int:
    files = [Path(a) for a in argv[1:]] if len(argv) > 1 else staged_migrations()
    violations = [v for f in files for v in check_file(f)]
    for v in violations:
        print(f"scv-review: {v}", file=sys.stderr)
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
