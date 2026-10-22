"""Mutation-check the shared statement's serving form.

The rule (manager root `86152f1`): a `db/queries/<resource>.sql` statement
IS the runtime's statement -- the shared file carries the serving form,
and a runtime that inlines a copy of a named statement is token-equal to
it. The pin (`services/api/tests/test_shared_statement_serving_form.py`)
compares the two texts.

A check that cannot fail is not a check, so this script is the receipt:
each mutation breaks the rule in one of its two directions -- the file
drifts from the runtime, or the runtime drifts from the file -- and the
pin must FAIL on every one of them. The files are restored BYTES (a text
round-trip rewrites CRLF to LF, and the tree then stops matching the
commit).

    python scripts/mutation_check_shared_statement.py
"""
from __future__ import annotations

import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
TEST = "services/api/tests/test_shared_statement_serving_form.py"

# (file, name, pattern, replacement) -- the pattern must match exactly once.
MUTATIONS = [
    (
        "db/queries/customers.sql",
        "the shared customer list drops the brand scope the runtime serves",
        r"s\.brand_id = \$7::uuid",
        "s.brand_id = $8::uuid",
    ),
    (
        "db/queries/orders.sql",
        "the shared order list drops the brand scope the runtime serves",
        r"s\.brand_id = \$9::uuid",
        "s.brand_id = $4::uuid",
    ),
    (
        "db/queries/identity.sql",
        "the shared staff list drops the brand scope the runtime serves",
        r"s\.brand_id = \$5::uuid",
        "s.brand_id = $6::uuid",
    ),
    (
        "services/api/repos/customers.py",
        "the runtime's copy of the customer list drifts from the shared file",
        r"s\.brand_id = \$7::uuid",
        "s.brand_id = $8::uuid",
    ),
    # --- the folds of 2026-10-22: each one is pinned by the same check --
    (
        "db/queries/customers.sql",
        "the shared customer read drops the linked_customer_id the runtime serves",
        r"created_at, updated_at, group_id, linked_customer_id,",
        "created_at, updated_at, group_id,",
    ),
    (
        "db/queries/customers.sql",
        "the shared customer insert loses the jsonb cast the runtime serves",
        r"VALUES \(\$1, \$2, \$3, \$4::jsonb, \$5, \$6, \$7, \$8, \$9\)",
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)",
    ),
    (
        "db/queries/customers.sql",
        "the shared attribute merge drops the jsonb_strip_nulls the runtime serves",
        r"jsonb_strip_nulls\(COALESCE\(attributes, '\{\}'::jsonb\) \|\| \$2::jsonb\)",
        "COALESCE(attributes, '{}'::jsonb) || $2::jsonb",
    ),
    (
        "db/queries/orders.sql",
        "the shared order header drops the brand scope the runtime serves",
        r"WHERE o\.id = \$1\s+AND \(\$2::uuid IS NULL OR s\.brand_id = \$2::uuid\);",
        "WHERE o.id = $1;",
    ),
    (
        "db/queries/orders.sql",
        "the shared customer order list drops the store filter the runtime serves",
        r"AND \(\$5::text IS NULL OR s\.code = \$5\)",
        "",
    ),
    (
        "db/queries/identity.sql",
        "the shared staff insert drops the brand column the runtime serves",
        r"INSERT INTO staff \(staff_code, full_name, store_id, role, brand_id\)",
        "INSERT INTO staff (staff_code, full_name, store_id, role)",
    ),
    (
        "db/queries/identity.sql",
        "the shared staff read drops the brand scope the runtime serves",
        r"WHERE s\.id = \$1\s+AND \(\$2::uuid IS NULL OR s\.brand_id = \$2::uuid\);",
        "WHERE s.id = $1;",
    ),
    (
        "db/queries/identity.sql",
        "the shared user read drops the brand scope the runtime serves",
        r"WHERE u\.id = \$1\s+AND \(\$2::uuid IS NULL OR s\.brand_id = \$2::uuid\);",
        "WHERE u.id = $1;",
    ),
    (
        "db/queries/identity.sql",
        "the shared unlinked staff read drops the brand scope the runtime serves",
        r"AND \(\$1::uuid IS NULL OR brand_id = \$1::uuid\)",
        "",
    ),
    (
        "db/queries/identity.sql",
        "the shared linked-user count grows an alias the runtime does not serve",
        r"SELECT COUNT\(\*\) FROM users WHERE staff_id = \$1",
        "SELECT COUNT(*) AS n FROM users WHERE staff_id = $1",
    ),
    (
        "services/api/repos/orders.py",
        "the runtime's copy of the order header drifts from the shared file",
        r"WHERE o\.id = \$1\s+AND \(\$2::uuid IS NULL OR s\.brand_id = \$2::uuid\)",
        "WHERE o.id = $1 AND ($2::uuid IS NULL OR s.brand_id = $3::uuid)",
    ),
]


def run() -> int:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", TEST, "-q", "-p", "no:cacheprovider"],
        cwd=ROOT, capture_output=True, text=True)
    return proc.returncode


def main() -> int:
    originals = {rel: (ROOT / rel).read_bytes() for rel, _, _, _ in MUTATIONS}
    bad = 0
    try:
        for rel, name, pattern, replacement in MUTATIONS:
            target = ROOT / rel
            original = originals[rel]
            text = original.decode("utf-8")
            mutated, count = re.subn(pattern, replacement, text, count=1)
            if count != 1:
                print(f"[SKIP ] {name}: the anchor matched {count} times - {pattern!r}")
                bad += 1
                continue
            target.write_bytes(mutated.encode("utf-8"))
            code = run()
            verdict = "FAILS (good)" if code != 0 else "PASSES (BAD)"
            print(f"[{verdict}] {name} (pytest exit {code})")
            if code == 0:
                bad += 1
            target.write_bytes(original)
    finally:
        for rel, original in originals.items():
            (ROOT / rel).write_bytes(original)
    print("restored:", all((ROOT / rel).read_bytes() == original
                           for rel, original in originals.items()))
    print("mutations that did not fail their check:", bad)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
