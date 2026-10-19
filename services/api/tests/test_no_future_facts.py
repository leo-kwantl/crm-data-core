"""No seeded fact may be dated in the future.

The defect this file guards (measured on the live demo data 2026-10-20): one
order at `2027-03-01T20:00Z` with `status='completed'` -- invisible in every
chart that windows on "the last N days", but first on every `last_order_at`
surface, forever, because its recency never lapses. It came from a *test*
fixture (`test_snapshots.py`) that hardcoded a future instant and kept its own
row; the seed generators were innocent, and these tests keep them that way.

The property asserted is "the generator cannot reach the future", not "this
particular row is gone":

* `db/seeds/*.sql` dates every fact relative to `now()` -- a hardcoded date
  literal in a fact insert would drift into the future on its own.
* the two python seeders are anchored (`ANCHOR`, `MONTH_RANGES`) and must not
  be anchored in the future -- that is what turns a re-seed into the bug.
* `pipelines/apply.py` refuses a *completed* order dated after now
  (`future_dated`, tested in `test_apply.py`), so an ingest feed cannot file
  one either.
"""

from __future__ import annotations

import importlib.util
import re
from datetime import date
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SEEDS = REPO / "db" / "seeds"

# Tables whose timestamps record something that happened. A future value here
# is a fact that has not happened yet.
FACT_TABLES = ("orders", "order_items", "customer_events", "consent_events")

_DATE_LITERAL = re.compile(r"'2\d{3}-\d{2}-\d{2}")
_TIME_COLUMN = re.compile(
    r"\b(ordered_at|occurred_at|captured_at|created_at|updated_at)\b", re.I)


def _seed_files() -> list[Path]:
    files = sorted(SEEDS.glob("*.sql"))
    assert files, f"no seed SQL under {SEEDS}"
    return files


def _strip_comments(stmt: str) -> str:
    """Drop whole-line `--` comments so a statement's head is its INSERT."""
    return "\n".join(
        ln for ln in stmt.splitlines() if not ln.strip().startswith("--"))


def test_seed_sql_dates_every_fact_relative_to_now() -> None:
    """A fact insert carries no absolute date and no future offset."""
    seen_now_relative = 0
    for path in _seed_files():
        text = path.read_text(encoding="utf-8")
        assert "now() +" not in text, (
            f"{path.name}: a seed may not add time to now(); a fact must not"
            " be dated in the future")
        for raw_stmt in text.split(";"):
            stmt = _strip_comments(raw_stmt)
            head = stmt.strip()[:200].lower()
            if not head.startswith("insert into"):
                continue
            table = head.split()[2].strip("( \n\t")
            if table not in FACT_TABLES:
                continue
            line = stmt.strip().splitlines()[0]
            assert not _DATE_LITERAL.search(stmt), (
                f"{path.name}: {table} is inserted with an absolute date"
                f" ({_DATE_LITERAL.search(stmt).group(0)}...) -- a literal"
                " drifts into the future as the clock moves; use"
                " `now() - interval`")
            if not _TIME_COLUMN.search(line):
                # No timestamp column in the list: the value comes from the
                # column default (now()), which cannot be future-dated.
                continue
            assert "now()" in stmt, (
                f"{path.name}: {table} insert dates a column without now():"
                f" {line!r} -- every fact timestamp must be relative to the"
                " seed clock")
            seen_now_relative += 1
    assert seen_now_relative > 0, (
        "the scan found no now()-relative fact insert -- it is not reading the"
        " statements it thinks it is")


def _load(name: str, relpath: str):
    spec = importlib.util.spec_from_file_location(name, REPO / relpath)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_reseed_anchor_is_not_in_the_future() -> None:
    """`ANCHOR` is the last day a reseeded order may land on."""
    mod = _load("seed_reseed_1200_guard", "scripts/seed_reseed_1200.py")
    anchor = mod.ANCHOR
    assert isinstance(anchor, date)
    assert anchor <= date.today(), (
        f"scripts/seed_reseed_1200.py ANCHOR={anchor} is in the future: every"
        " order this seeder draws is <= ANCHOR, so the next re-seed would file"
        " completed orders that have not happened")


def test_the_dev_history_month_range_is_not_in_the_future() -> None:
    """`MONTH_RANGES` is drawn as whole months, day-of-month irrelevant."""
    mod = _load("seed_dev_history_guard", "scripts/seed_dev_history.py")
    months = list(mod.TARGET_MONTHS)
    assert months, "seed_dev_history.TARGET_MONTHS is empty"
    today = date.today()
    latest = max(months)
    assert latest <= (today.year, today.month), (
        f"scripts/seed_dev_history.py reaches {latest[0]}-{latest[1]:02d}, after"
        f" {today.year}-{today.month:02d}: orders drawn there are future facts")
