"""Mutation-check the demo seed's legacy backfill (data).

Each mutation turns ONE part of `db/seeds/demo.sql`'s backfill back to its
old behaviour and asserts that the checks FAIL. A check that cannot fail is
not a check: this script is the receipt.

    python scripts/mutation_check_demo_seed_backfill.py
"""
from __future__ import annotations

import pathlib
import re
import subprocess
import sys

try:
    from _teeth_preamble import (announce, read_target, require_byte_baseline,
                                 write_mutation)
except ImportError:  # pragma: no cover - run as `python -m scripts....`
    from scripts._teeth_preamble import (announce, read_target,
                                         require_byte_baseline, write_mutation)

ROOT = pathlib.Path(__file__).resolve().parents[1]
TARGET = ROOT / "db" / "seeds" / "demo.sql"
TEST = "services/api/tests/test_demo_seed.py"

START = "WITH demo_keys"
END = "  AND c.rn = 1;"


def _drop_backfill(text: str) -> str:
    """Remove the whole backfill statement."""
    start = text.index(START)
    end = text.index(END, start) + len(END)
    return text[:start] + text[end:]


def _key_every_row(text: str) -> str:
    """Backfill every matching row, not only the earliest."""
    return text.replace("  AND c.rn = 1;", "  AND c.rn >= 1;", 1)


def _wrong_amount(text: str) -> str:
    """One demo order's amount does not match the seed's row."""
    return re.sub(r"(\('c0000000-0000-0000-0000-000000000005',\s*)7900(\s*,\s*'DEMO-O-010'\))",
                  r"\g<1>7901\g<2>", text, count=1)


MUTATIONS = [
    ("the backfill is gone (the legacy re-seed adds 15 more demo orders)",
     _drop_backfill),
    ("the backfill keys every matching row (a legacy double seed violates "
     "the partial unique index)", _key_every_row),
    ("one demo order's amount does not match (that order is added again)",
     _wrong_amount),
]


def run() -> int:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", TEST, "-q", "-p", "no:cacheprovider"],
        cwd=ROOT, capture_output=True, text=True)
    return proc.returncode


def main() -> int:
    announce("the demo seed's backfilled rows", ROOT, TEST)
    if not require_byte_baseline(ROOT, ["db/seeds/demo.sql"]):
        return 2
    original, original_bytes = read_target(TARGET)
    bad = 0
    try:
        for name, mutate in MUTATIONS:
            try:
                text = mutate(original)
            except (ValueError, re.error) as exc:
                print(f"[SKIP ] {name}: the anchor is gone - {exc}")
                bad += 1
                continue
            if text == original:
                print(f"[SKIP ] {name}: the mutation changed nothing")
                bad += 1
                continue
            write_mutation(TARGET, text, original_bytes)
            code = run()
            verdict = "FAILS (good)" if code != 0 else "PASSES (BAD)"
            print(f"[{verdict}] {name} (pytest exit {code})")
            if code == 0:
                bad += 1
    finally:
        TARGET.write_bytes(original_bytes)
    print("restored:", TARGET.read_bytes() == original_bytes)
    print("mutations that did not fail their check:", bad)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
