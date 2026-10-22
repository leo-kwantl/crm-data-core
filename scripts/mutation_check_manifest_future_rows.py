"""Mutation-check the manifest's future-rows guard list (data).

Each mutation turns ONE part of `058_manifest_future_rows_waits_for_kpi.sql`
back to its old behaviour and asserts that the check FAILS. A check that
cannot fail is not a check: this script is the receipt.

    python scripts/mutation_check_manifest_future_rows.py
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

try:
    from _teeth_preamble import announce
except ImportError:  # pragma: no cover - run as `python -m scripts....`
    from scripts._teeth_preamble import announce

ROOT = pathlib.Path(__file__).resolve().parents[1]
TARGET = ROOT / "db" / "migrations" / "058_manifest_future_rows_waits_for_kpi.sql"
TEST = ("services/api/tests/test_manifests.py::"
        "test_the_future_rows_guard_waits_for_every_other_nightly_job")

MUTATIONS = [
    ("the migration does not run (the guard keeps waiting for eight jobs)",
     [("   WHERE yaml ~ v_pat;", "   WHERE false;")]),
    ("the migration waits for a job that does not exist",
     [("        - kpi\\2\\3$re$;", "        - kpi_daily\\2\\3$re$;")]),
    ("the migration touches another job's list instead of the guard's",
     [("      future_rows:\\r?\\n        engine: future_rows_guard\\r?\\n"
       "        depends_on:\\r?\\n        - apply",
       "      rfm:\\r?\\n        engine: ld8_snapshot\\r?\\n"
       "        depends_on:\\r?\\n        - apply")]),
]


def run() -> int:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", TEST, "-q", "-p", "no:cacheprovider"],
        cwd=ROOT, capture_output=True, text=True)
    return proc.returncode


def main() -> int:
    announce("the manifest's future-row waits", ROOT, TEST)
    original = TARGET.read_text(encoding="utf-8")
    bad = 0
    try:
        for name, edits in MUTATIONS:
            text = original
            missing = [old for old, _ in edits if old not in text]
            if missing:
                print(f"[SKIP ] {name}: the anchor is gone - {missing[0]!r}")
                bad += 1
                continue
            for old, new in edits:
                text = text.replace(old, new, 1)
            TARGET.write_text(text, encoding="utf-8")
            code = run()
            verdict = "FAILS (good)" if code != 0 else "PASSES (BAD)"
            print(f"[{verdict}] {name} (pytest exit {code})")
            if code == 0:
                bad += 1
    finally:
        TARGET.write_text(original, encoding="utf-8")
    print("restored:", TARGET.read_text(encoding="utf-8") == original)
    print("mutations that did not fail their check:", bad)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
