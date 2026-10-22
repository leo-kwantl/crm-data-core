"""Mutation-check the shape pin on data's group filters.

The pin (`services/api/tests/test_scope_predicate_shape.py`) says three
things about every group filter in data's tree: the FORM of each
comparison (a population filter uses `=`, a row's own scope column uses
`IS NOT DISTINCT FROM`), the POPULATION each statement selects (which
family), and that no group filter is unclassified.

A check that cannot fail is not a check, so this script is the receipt:
each mutation breaks one of those three rules and the pin must FAIL on
every one of them. The file is restored BYTES (a text round-trip
rewrites CRLF to LF, and the tree then stops matching the commit).

    python scripts/mutation_check_scope_predicate.py
"""
from __future__ import annotations

import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
TARGET = ROOT / "pipelines" / "snapshot.py"
TEST = "services/api/tests/test_scope_predicate_shape.py"

EXTRA = '''

# A group filter nobody classified -- the pin must notice.
UNCLASSIFIED_GROUP_SQL = """
SELECT o.id FROM orders o
  JOIN customers c ON c.id = o.customer_id
  WHERE c.group_id = $1::uuid
"""
'''

# (name, pattern, replacement) -- the pattern must match exactly once.
MUTATIONS = [
    ("the brand's group conjunct dropped from the KPI population",
     r"[ \t]*AND \(\$3::uuid IS NULL OR b\.group_id = \$3::uuid\)\r?\n",
     ""),
    ("a scope-exact predicate loosened to '=' (the global scope drops out)",
     r"AND group_id IS NOT DISTINCT FROM \$4::uuid",
     "AND group_id = $4::uuid"),
    ("the customer's group read through a second parameter",
     r"c\.group_id = \$3::uuid",
     "c.group_id = $2::uuid"),
]


def run() -> int:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", TEST, "-q", "-p", "no:cacheprovider"],
        cwd=ROOT, capture_output=True, text=True)
    return proc.returncode


def main() -> int:
    original = TARGET.read_bytes()
    bad = 0
    try:
        for name, pattern, replacement in MUTATIONS:
            text = original.decode("utf-8")
            mutated, count = re.subn(pattern, replacement, text, count=1)
            if count != 1:
                print(f"[SKIP ] {name}: the anchor matched {count} times - {pattern!r}")
                bad += 1
                continue
            TARGET.write_bytes(mutated.encode("utf-8"))
            code = run()
            verdict = "FAILS (good)" if code != 0 else "PASSES (BAD)"
            print(f"[{verdict}] {name} (pytest exit {code})")
            if code == 0:
                bad += 1
        # the fourth mutation adds a group filter nobody declared
        TARGET.write_bytes(original + EXTRA.encode("utf-8"))
        code = run()
        verdict = "FAILS (good)" if code != 0 else "PASSES (BAD)"
        print(f"[{verdict}] a new group filter that nobody classified "
              f"(pytest exit {code})")
        if code == 0:
            bad += 1
    finally:
        TARGET.write_bytes(original)
    print("restored:", TARGET.read_bytes() == original)
    print("mutations that did not fail their check:", bad)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
