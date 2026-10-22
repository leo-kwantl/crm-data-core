"""Mutation-check the manifest scopes restatement (data, A114).

Each mutation turns ONE part of `057_manifest_scopes_brands_hq.sql` back to
its old behaviour and asserts that the check FAILS. A check that cannot fail
is not a check: this script is the receipt.

    python scripts/mutation_check_manifest_scopes.py
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
TARGET = ROOT / "db" / "migrations" / "057_manifest_scopes_brands_hq.sql"
TEST = ("services/api/tests/test_manifests.py::"
        "test_the_scopes_block_names_the_scopes_the_tick_dispatches")

MUTATIONS = [
    ("the migration does not run (the block keeps its group leg)",
     [("   WHERE yaml ~ v_pat;", "   WHERE false;")]),
    ("the migration restates the block but keeps the group key",
     [("v_rep constant text := $re$\\1\\2      hq:\\2      - '*'\\3$re$;",
       "v_rep constant text := $re$\\1\\2      groups:\\2      - '*'\\3$re$;")]),
]


def run() -> int:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", TEST, "-q", "-p", "no:cacheprovider"],
        cwd=ROOT, capture_output=True, text=True)
    return proc.returncode


def main() -> int:
    announce("the manifest's scopes", ROOT, TEST)
    if not require_byte_baseline(ROOT, ["db/migrations/057_manifest_scopes_brands_hq.sql"]):
        return 2
    original, original_bytes = read_target(TARGET)
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
