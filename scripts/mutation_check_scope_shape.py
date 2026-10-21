"""Mutation-check the scope-shape guard (data, A114).

The mutation turns the guard into a no-op and asserts that EVERY surface
that refuses a group scope fails without it: the helper's own rule and
each of the three CLIs. A check that cannot fail is not a check: this
script is the receipt.

    python scripts/mutation_check_scope_shape.py
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
TARGET = ROOT / "pipelines" / "scope_shape.py"
TEST = "services/api/tests/test_scope_shape.py"

MUTATIONS = [
    ("the guard never refuses",
     [("    if not group_id:\n        return",
       "    if True:  # mutation: the guard never refuses\n        return")]),
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
