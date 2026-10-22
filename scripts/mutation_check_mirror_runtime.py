"""Mutation-check the mirror guard (data, A114 part 3).

The mutation puts ONE character of drift into a shared runtime file and
asserts that the guard FAILS. A check that cannot fail is not a check: this
script is the receipt.

    python scripts/mutation_check_mirror_runtime.py
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
TARGET = ROOT / "services" / "api" / "analytics" / "scope.py"
TEST = "services/api/tests/test_mirror_runtime.py"

MUTATIONS = [
    ("one comment in a shared runtime file (drift by a byte)",
     [('NO_ANALYTICS_SCOPE = "no_analytics_scope"',
       "# mutation: drift\nNO_ANALYTICS_SCOPE = \"no_analytics_scope\"")]),
]


def run() -> int:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", TEST, "-q", "-p", "no:cacheprovider"],
        cwd=ROOT, capture_output=True, text=True)
    return proc.returncode


def main() -> int:
    announce("the runtime's mirror scope", ROOT, TEST)
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
