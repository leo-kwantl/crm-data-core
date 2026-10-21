"""Mutation-check the brand scope on the global order list (data).

The mutation makes the brand predicate ALWAYS TRUE while keeping the
statement's arity (the placeholder stays, the rule does not), so the leak
app closed in `78fa78d` is EXPOSED: a brand-scoped session reads every
brand's orders again. The check that must fail is data's own two-brand
test -- a check that cannot fail is not a check, so this script is the
receipt.

    python scripts/mutation_check_orders_brand_scope.py
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
TARGET = ROOT / "services" / "api" / "repos" / "orders.py"
TEST = "services/api/tests/test_orders_brand_scope.py"

MUTATIONS = [
    ("the brand predicate on the global order list (always true)",
     [("  AND ($9::uuid IS NULL OR s.brand_id = $9::uuid)",
       "  AND ($9::uuid IS NULL OR $9::uuid IS NOT NULL)")]),
]


def run() -> int:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", TEST, "-q", "-p", "no:cacheprovider"],
        cwd=ROOT, capture_output=True, text=True)
    return proc.returncode


def main() -> int:
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
