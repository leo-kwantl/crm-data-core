"""Shared-database guard for the test suite.

The default fixture path starts a throwaway pgembed cluster, so a normal run
cannot touch anything anybody else reads. The `DATABASE_URL` swap path breaks
that promise: it points the whole suite at whatever database the variable names
— in practice the dev cluster — and nothing in a test says so.

This module holds the two cheap pieces of that property:

* `shared_url_problem()` — refuse an externally supplied `DATABASE_URL` unless
  the run explicitly opts in. The failure is at session start, before a single
  row is written, and it names the hazard rather than a missing variable.
* `table_counts()` / `diff()` — when a run *does* opt in, the suite must leave
  the database as it found it. Counts are taken per table at start and at
  finish, and any difference fails the run, naming the tables. That is the
  whole rule: a test that writes must remove its rows in a `finally` (or use a
  disposable schema), and the audit proves it instead of trusting it.

The 2026-10-19 leak is the reason this exists: `test_snapshots.py` wrote a
future-dated order plus a dev customer and store into the dev database, and
`test_orders.py` / `test_customer_events.py` leave a customer (and products,
orders) behind on every shared run. A count diff catches the class; guarding
the seed dates alone catches one instance of it.
"""

from __future__ import annotations

ALLOW_ENV = "CRM_CDP_ALLOW_SHARED_DB"


def shared_url_problem(environ: dict[str, str]) -> str | None:
    """Return a refusal message when the run would write to a shared database.

    `None` means "fine to proceed": either no external URL was supplied (the
    suite starts its own cluster) or the run opted in knowingly.
    """
    dsn = environ.get("DATABASE_URL")
    if not dsn:
        return None
    if environ.get(ALLOW_ENV) == "1":
        return None
    return (
        "DATABASE_URL is set, so this suite would write to that database -- "
        "not the throwaway pgembed cluster it starts by default.\n"
        f"  DATABASE_URL={dsn}\n"
        "The test suite is not transactional and several fixtures leave rows "
        "behind on purpose (customers, stores, products), so pointing it at a "
        "shared or dev database pollutes it silently: that is how one fixture "
        "put a 2027 order into the demo data.\n"
        f"  - to run against a scratch database: unset DATABASE_URL\n"
        f"  - to accept the risk knowingly: set {ALLOW_ENV}=1; the run is then "
        "audited and fails if any table's row count changed"
    )


async def table_counts(conn) -> dict[str, int]:
    """Row count of every ordinary table in the connection's search path."""
    names = [
        r["relname"]
        for r in await conn.fetch(
            "SELECT c.relname FROM pg_class c"
            " JOIN pg_namespace n ON n.oid = c.relnamespace"
            " WHERE c.relkind = 'r' AND n.nspname = current_schema()"
            " ORDER BY c.relname"
        )
    ]
    counts = {}
    for name in names:
        counts[name] = int(await conn.fetchval(f'SELECT count(*) FROM "{name}"'))
    return counts


def diff(before: dict[str, int], after: dict[str, int]) -> list[tuple[str, int, int]]:
    """Tables whose row count moved, as `(table, before, after)` sorted by name."""
    moved = []
    for name in sorted(set(before) | set(after)):
        was, now = before.get(name, 0), after.get(name, 0)
        if was != now:
            moved.append((name, was, now))
    return moved


def describe(moved: list[tuple[str, int, int]]) -> str:
    return ", ".join(f"{name} {was}->{now}" for name, was, now in moved)
