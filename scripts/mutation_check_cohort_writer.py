"""Mutation-check the cohort writer's new checks (data D2).

Each mutation turns ONE part of the writer back to its old behaviour and
asserts that the matching check FAILS. A check that cannot fail is not a
check: this script is the receipt.

    python scripts/mutation_check_cohort_writer.py
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

try:
    from _teeth_preamble import (announce, read_target, require_byte_baseline,
                                 write_mutation)
except ImportError:  # pragma: no cover - run as `python -m scripts....`
    from scripts._teeth_preamble import (announce, read_target,
                                         require_byte_baseline, write_mutation)

ROOT = pathlib.Path(__file__).resolve().parents[1]
TARGET = ROOT / "pipelines" / "snapshot.py"

MUTATIONS = [
    ("frozen cells (DO UPDATE -> keep the old values)",
     [("retained = EXCLUDED.retained,",
       "retained = cohort_snapshots.retained,")],
     "test_writer_re_measures_its_cells_and_their_money"),
    ("no delete of the stale cells",
     [("AND NOT EXISTS (",
       "AND false AND NOT EXISTS (")],
     "test_stale_cells_are_deleted_in_this_granularity_and_scope_only"),
    ("the writer stops writing the population rule",
     [("     retention_rate, retained_cents, brand_id, group_id,"
       " population_rule)",
       "     retention_rate, retained_cents, brand_id, group_id)"),
      ("         retention_rate, retained_cents, $3::uuid, $4::uuid,\n"
       "         '{POPULATION_RULE}'",
       "         retention_rate, retained_cents, $3::uuid, $4::uuid)"),
      ("                population_rule = EXCLUDED.population_rule,\n",
       "")],
     "test_the_row_names_the_population_it_describes"),
    ("a customer sits in more than one cohort",
     [("date_trunc($1::text, MIN(ordered_at) {BUSINESS_TZ_SQL})::date"
       " AS cohort",
       "date_trunc($1::text, ordered_at {BUSINESS_TZ_SQL})::date AS cohort")],
     "test_the_cohorts_are_disjoint"),
]


def run(test: str) -> int:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest",
         f"services/api/tests/test_cohort_writer.py::{test}",
         "-q", "-p", "no:cacheprovider"],
        cwd=ROOT, capture_output=True, text=True)
    return proc.returncode


def main() -> int:
    announce("the cohort writer's cells", ROOT, "per-mutation (MUTATIONS)")
    if not require_byte_baseline(ROOT, ["pipelines/snapshot.py"]):
        return 2
    original, original_bytes = read_target(TARGET)
    bad = 0
    try:
        for name, edits, test in MUTATIONS:
            text = original
            missing = [old for old, _ in edits if old not in text]
            if missing:
                print(f"[SKIP ] {name}: the anchor is gone - {missing[0]!r}")
                bad += 1
                continue
            for old, new in edits:
                text = text.replace(old, new, 1)
            write_mutation(TARGET, text, original_bytes)
            code = run(test)
            verdict = "FAILS (good)" if code != 0 else "PASSES (BAD)"
            print(f"[{verdict}] {name} -> {test} (pytest exit {code})")
            if code == 0:
                bad += 1
    finally:
        TARGET.write_bytes(original_bytes)
    print("restored:", TARGET.read_bytes() == original_bytes)
    print("mutations that did not fail their check:", bad)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
