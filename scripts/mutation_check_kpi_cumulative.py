"""Mutation-check the KPI snapshot's cumulative columns.

The rule (manager FYI 2026-10-22): a `kpi_daily_snapshots` row is a
statement about ONE day, so its cumulative columns are the state as of
that row's own date -- the bound is the row's `d_to` (the next business
day's midnight). Unbounded, the row carried the present table's counts
under a past date's label, so a reader plotting the cumulative curve sees
a flat line that looks like history.

The pin (`services/api/tests/test_snapshots.py::test_kpi_cumulative_
columns_are_as_of_the_row_date`) compares the written row against the
counts bounded by the same `d_to`.

A check that cannot fail is not a check, so this script is the receipt:
each mutation drops one of the three bounds and the pin must FAIL on
every one of them. The file is restored BYTES (a text round-trip
rewrites CRLF to LF and the tree then stops matching the commit).

The same three ways this script used to leave a lie behind are closed
here (see `scripts/mutation_check_shared_statement.py`): a dirty tree is
refused, a lock keeps two runs from interleaving their mutations, and
the touched file is proved to match the commit at the end.

    DATABASE_URL=... python scripts/mutation_check_kpi_cumulative.py
"""
from __future__ import annotations

import os
import pathlib
import re
import subprocess
import sys
import time

try:
    from _teeth_preamble import (announce, bytes_differ_from_commit,
                                 require_byte_baseline)
except ImportError:  # pragma: no cover - run as `python -m scripts....`
    from scripts._teeth_preamble import (announce, bytes_differ_from_commit,
                                         require_byte_baseline)

ROOT = pathlib.Path(__file__).resolve().parents[1]
TEST = "services/api/tests/test_snapshots.py"
TARGET = "pipelines/snapshot.py"
LOCK = ROOT / ".pi" / "mutation_check_kpi_cumulative.lock"
LOCK_STALE_SECONDS = 15 * 60

# (name, pattern, replacement) -- the pattern must match exactly once.
# The newline is captured and put back: the working copy is CRLF and the
# commit is LF, so a pattern that hard-codes "\n" matches nothing and a
# replacement that writes "\n" rewrites the line ending.
MUTATIONS = [
    (
        "the customer total drops the row's own date bound",
        r"\(SELECT COUNT\(\*\) FROM scoped_customers, day_bounds(\r?\n)    WHERE created_at < d_to\)",
        r"(SELECT COUNT(*) FROM scoped_customers, day_bounds\1    WHERE created_at IS NOT NULL)",
    ),
    (
        "the order total drops the row's own date bound",
        r"\(SELECT COUNT\(\*\) FROM scoped_orders, day_bounds(\r?\n)    WHERE ordered_at < d_to\)",
        r"(SELECT COUNT(*) FROM scoped_orders, day_bounds\1    WHERE ordered_at IS NOT NULL)",
    ),
    (
        "the revenue total drops the row's own date bound",
        r"\(SELECT COALESCE\(SUM\(total_cents\), 0\) FROM scoped_orders, day_bounds(\r?\n)    WHERE ordered_at < d_to\)",
        r"(SELECT COALESCE(SUM(total_cents), 0) FROM scoped_orders, day_bounds\1    WHERE ordered_at IS NOT NULL)",
    ),
]


def dirty_lines() -> list[str]:
    proc = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT,
                          capture_output=True, text=True)
    return [line for line in proc.stdout.splitlines() if line.strip()]


def differs_from_head(rel: str) -> bool:
    """Do the file's BYTES differ from the commit's?

    Not `git diff`: with `text=auto` git normalises the line endings
    before comparing, so a CRLF working copy reads as UNCHANGED while
    every byte differs from the blob. The baseline a mutation run needs
    is the committed BYTES.
    """
    return bytes_differ_from_commit(ROOT, rel)


def take_lock() -> None:
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    if LOCK.exists():
        age = time.time() - LOCK.stat().st_mtime
        if age < LOCK_STALE_SECONDS:
            raise SystemExit(
                f"[REFUSED] another run holds {LOCK.name} ({int(age)}s old) - "
                "two runs would interleave their mutations")
        print(f"[LOCK ] taking over a stale lock ({int(age)}s old)")
        LOCK.unlink()
    fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    os.write(fd, str(os.getpid()).encode())
    os.close(fd)


def release_lock() -> None:
    LOCK.unlink(missing_ok=True)


def run() -> int:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", TEST, "-q", "-p", "no:cacheprovider"],
        cwd=ROOT, capture_output=True, text=True)
    return proc.returncode


def main() -> int:
    announce("the KPI snapshot's cumulative columns", ROOT, TEST,
             targets=[TARGET])
    if not require_byte_baseline(ROOT, [TARGET]):
        return 2
    take_lock()
    target = ROOT / TARGET
    original = target.read_bytes()
    bad = 0
    try:
        for name, pattern, replacement in MUTATIONS:
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
        target.write_bytes(original)
        release_lock()
    print("restored:", target.read_bytes() == original)
    if differs_from_head(TARGET):
        print("[DRIFT] the file differs from the commit after the run:", TARGET)
        bad += 1
    print("mutations that did not fail their check:", bad)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
