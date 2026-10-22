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

WHERE EACH MUTATION LIVES. The pin's subject is BOTH `pipelines/**` and
`db/queries/*.sql`, and a statement moves between them: the KPI trend
read was a literal in `pipelines/snapshot.py` until the shared-file wave
(`60cda08`) moved it to `db/queries/kpi_daily.sql`, and this script's
scope-exact mutation kept its old anchor -- so it matched nothing, the
run reported `[SKIP ]`, and the check had stopped checking. A mutation
is therefore NAMED with the file it mutates, and an anchor that matches
zero times is a failure, not a skip: an anchor that no longer matches
means the statement moved, and the pin has to be re-pointed at it.

    python scripts/mutation_check_scope_predicate.py
"""
from __future__ import annotations

import pathlib
import re
import subprocess
import sys

try:
    from _teeth_preamble import announce, require_byte_baseline
except ImportError:  # pragma: no cover - run as `python -m scripts....`
    from scripts._teeth_preamble import announce, require_byte_baseline

ROOT = pathlib.Path(__file__).resolve().parents[1]
TARGET = ROOT / "pipelines" / "snapshot.py"
# The shared file the KPI wave moved the trend read into. The pin reads
# it, so a mutation here is a mutation the pin must catch.
SHARED = ROOT / "db" / "queries" / "kpi_daily.sql"
TEST = "services/api/tests/test_scope_predicate_shape.py"

EXTRA = '''

# A group filter nobody classified -- the pin must notice.
UNCLASSIFIED_GROUP_SQL = """
SELECT o.id FROM orders o
  JOIN customers c ON c.id = o.customer_id
  WHERE c.group_id = $1::uuid
"""
'''

# (name, target, pattern, replacement) -- the pattern must match exactly
# once in THAT file. `target` is "pipelines" or "shared".
MUTATIONS = [
    ("the brand's group conjunct dropped from the KPI population",
     "pipelines",
     r"[ \t]*AND \(\$3::uuid IS NULL OR b\.group_id = \$3::uuid\)\r?\n",
     ""),
    # The scope-exact predicate of the KPI trend read, in the file that
    # now holds it (the shared one). Loosened to '=', the global row --
    # the row with a NULL scope -- drops out of the read.
    ("a scope-exact predicate loosened to '=' (the global scope drops out)",
     "shared",
     r"AND group_id IS NOT DISTINCT FROM \$4::uuid",
     "AND group_id = $4::uuid"),
    ("the customer's group read through a second parameter",
     "pipelines",
     r"c\.group_id = \$3::uuid",
     "c.group_id = $2::uuid"),
]


def run() -> int:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", TEST, "-q", "-p", "no:cacheprovider"],
        cwd=ROOT, capture_output=True, text=True)
    return proc.returncode


def main() -> int:
    announce("the scope predicate's shape", ROOT, TEST,
             targets=["pipelines/snapshot.py", "db/queries/kpi_daily.sql"])
    if not require_byte_baseline(
            ROOT, ["pipelines/snapshot.py", "db/queries/kpi_daily.sql"]):
        return 2
    files = {"pipelines": TARGET, "shared": SHARED}
    originals = {key: path.read_bytes() for key, path in files.items()}
    bad = 0
    try:
        for name, target, pattern, replacement in MUTATIONS:
            path = files[target]
            original = originals[target]
            text = original.decode("utf-8")
            mutated, count = re.subn(pattern, replacement, text, count=1)
            if count != 1:
                # An anchor that no longer matches means the statement
                # MOVED: the check has stopped checking, which is a
                # failure, not a skip.
                print(f"[STALE] {name}: the anchor matched {count} times in "
                      f"{path.name} - {pattern!r}")
                bad += 1
                continue
            path.write_bytes(mutated.encode("utf-8"))
            code = run()
            verdict = "FAILS (good)" if code != 0 else "PASSES (BAD)"
            print(f"[{verdict}] {name} (pytest exit {code})")
            if code == 0:
                bad += 1
        # the fourth mutation adds a group filter nobody declared
        TARGET.write_bytes(originals["pipelines"] + EXTRA.encode("utf-8"))
        code = run()
        verdict = "FAILS (good)" if code != 0 else "PASSES (BAD)"
        print(f"[{verdict}] a new group filter that nobody classified "
              f"(pytest exit {code})")
        if code == 0:
            bad += 1
    finally:
        for key, path in files.items():
            path.write_bytes(originals[key])
    for key, path in files.items():
        print(f"restored: {path.name}", path.read_bytes() == originals[key])
    print("mutations that did not fail their check:", bad)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
