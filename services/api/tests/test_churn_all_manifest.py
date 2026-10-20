"""The churn page's "All activity" tab needs an `all` snapshot to exist.

The portal offers the tab, and no `activity = 'all'` snapshot has ever been
written: the tab 404s and the empty state says "run the pipeline first",
which is false on a system whose pipeline has run. ml measured what making
the tab true costs (ml master 0326402): the `all` leg admits the SAME
cohort as the `orders` leg (brand scope: 0 customers in `all` but not in
`orders`; the same 498 customers either way) and re-times 2 of 498
(expected purchases 214.7 -> 216.7, +0.9%; HQ 500.7 -> 503.6, +0.6%), so the
promise the portal can make is "the same cohort, read on more activity",
never "a different population".

The fix is one manifest row: no ml change, no DDL, no new column -
`churn_snapshots` already carries `activity`, and the argv comes from the
manifest job's params. What can go wrong without a guard, and is therefore
asserted here:

- the row is lost in a later re-stating migration. A manifest is re-stated
  wholesale, so a re-stating migration owns every number in it, and a
  re-statement that forgets this leg leaves a dead control in the product's
  own UI with a nightly pipeline that reports success.
- the horizon drifts. The `all` leg is horizon 90, matching the nightly
  `orders` leg; 180 is not a promise for `all` (the only 180 pair that
  exists is an HQ one from 2026-10-15).
- `--rescore` is passed. The `all` leg is its own leg of the unique key
  (as_of, activity, horizon_days, scope) and writes its own row;
  `--rescore` targets a day x scope that already has a row.
- the `orders` leg's `activity` param is dropped, so it follows the code
  default instead of being pinned.

The assertions read the STORED manifest and the argv the builder produces,
so a later edit to either turns red here rather than in the product.
"""

from __future__ import annotations

import ast
from datetime import date
from pathlib import Path

import yaml

from pipelines import schedule as sched

REPO = Path(__file__).resolve().parents[3]
ML_CHURN_CLI = REPO.parent / "crm-cdp-ml" / "ml_jobs" / "run_churn_snapshot.py"


def _ctx(**kw):
    base = {"pipeline": "test_pipe", "as_of": date(2026, 10, 1),
            "brand_id": None, "group_id": None, "created_by": "test",
            "ml_repo": REPO, "db_url": "postgresql://fake/db"}
    base.update(kw)
    return sched.JobCtx(**base)


def _nightly_jobs(database_url: str) -> dict:
    """The stored nightly manifest's jobs, as written to the database."""
    import asyncio

    import asyncpg

    async def _read():
        conn = await asyncpg.connect(database_url)
        try:
            row = await conn.fetchrow(
                "SELECT yaml FROM pipeline_manifests WHERE pipeline = 'nightly'")
        finally:
            await conn.close()
        if row is None:
            return {}
        doc = yaml.safe_load(row["yaml"])
        return (doc.get("pipelines", {}).get("nightly", {}).get("jobs", {})
                or {})

    return asyncio.run(_read())


def _argv_value(argv: list[str], flag: str) -> str:
    assert flag in argv, f"{flag} not threaded: {argv}"
    return argv[argv.index(flag) + 1]


def test_stored_nightly_manifest_has_the_all_activity_leg(
        database_url: str) -> None:
    """The nightly manifest runs the `all` leg, and pins what it is."""
    jobs = _nightly_jobs(database_url)
    assert jobs, ("no stored nightly manifest - this test would pass "
                  "vacuously")

    all_leg = jobs.get("churn_all")
    assert all_leg is not None, (
        "the nightly manifest has no `churn_all` job: the portal's 'All "
        "activity' tab has no snapshot to read and 404s, and the empty "
        "state says 'run the pipeline first' on a system whose pipeline "
        "has run")
    assert all_leg.get("engine") == "churn_snapshot", all_leg
    params = all_leg.get("params") or {}
    assert params.get("activity") == "all", (
        f"churn_all.params.activity is {params.get('activity')!r}, not 'all' "
        f"- the leg would write an `orders` row a second time")
    assert int(params.get("horizon_days", 0)) == 90, (
        f"churn_all.params.horizon_days is {params.get('horizon_days')!r}; "
        f"the leg is horizon 90 to match the nightly `orders` leg, and 180 "
        f"is not a promise for `all`")
    assert "apply" in (all_leg.get("depends_on") or []), (
        f"churn_all must run after apply, not before: {all_leg}")


def test_the_orders_leg_stays_pinned_to_orders(database_url: str) -> None:
    """The existing leg keeps `orders` pinned, not left to a code default."""
    jobs = _nightly_jobs(database_url)
    churn = jobs.get("churn")
    assert churn is not None, "the nightly manifest has no `churn` job"
    params = churn.get("params") or {}
    assert params.get("activity") == "orders", (
        f"churn.params.activity is {params.get('activity')!r}; the nightly "
        f"leg must pin 'orders' so a code-default change cannot flip it")


def test_future_rows_guard_covers_the_all_activity_leg(
        database_url: str) -> None:
    """The guard checks `churn_snapshots`, so it must depend on the leg.

    The `all` leg writes into `churn_snapshots`; a guard that does not
    depend on it can pass on a table whose newest row the leg has not
    written yet - the guard would read a day it never checked.
    """
    jobs = _nightly_jobs(database_url)
    guard = jobs.get("future_rows")
    assert guard is not None, "the nightly manifest has no future_rows job"
    deps = guard.get("depends_on") or []
    assert "churn_all" in deps, (
        f"future_rows depends on {deps} but not on churn_all: the guard "
        f"reads churn_snapshots, and the `all` leg writes into it")


def test_all_activity_leg_builds_mls_flag_set(database_url: str) -> None:
    """The leg's params reach the CLI as the flag set ml declares."""
    jobs = _nightly_jobs(database_url)
    params = (jobs.get("churn_all") or {}).get("params") or {}
    argv = sched.ENGINE_BUILDERS["churn_snapshot"](params, _ctx())
    assert _argv_value(argv, "--activity") == "all"
    assert _argv_value(argv, "--horizon-days") == "90"
    assert _argv_value(argv, "--model") == "bgnbd"
    assert "--rescore" not in argv, (
        f"--rescore targets a day x scope that already has a row; the `all` "
        f"leg is its own leg of the unique key "
        f"(as_of, activity, horizon_days, scope) and writes its own row: "
        f"{argv}")


def test_the_two_legs_differ_only_by_activity(database_url: str) -> None:
    """Two legs, one difference - so the nightly cannot double-write."""
    jobs = _nightly_jobs(database_url)
    orders = (jobs.get("churn") or {}).get("params") or {}
    every = (jobs.get("churn_all") or {}).get("params") or {}
    assert set(orders) - {"activity"} == set(every) - {"activity"}, (
        f"the legs differ in more than `activity`: {orders} vs {every}")


def test_ml_cli_accepts_the_all_activity_value() -> None:
    """ml's own CLI declares `all` - read, not assumed.

    Parsed, not imported: the ml checkout is a sibling repo and importing it
    would drag in numpy for two strings. When the sibling is absent the
    check is skipped rather than failing (there is nothing to check), and
    when it is present but the choice is gone the test fails - ml renamed
    what this reads.
    """
    if not ML_CHURN_CLI.exists():
        return
    tree = ast.parse(ML_CHURN_CLI.read_text(encoding="utf-8"))
    choices: list[str] | None = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "add_argument":
            continue
        if not node.args or not isinstance(node.args[0], ast.Constant):
            continue
        if node.args[0].value != "--activity":
            continue
        for kw in node.keywords:
            if kw.arg == "choices" and isinstance(kw.value, (ast.List,
                                                           ast.Tuple)):
                choices = [e.value for e in kw.value.elts
                           if isinstance(e, ast.Constant)]
    assert choices is not None, (
        f"{ML_CHURN_CLI} no longer declares --activity with a choice list")
    assert "all" in choices, (
        f"ml's churn CLI declares --activity choices {choices}; the `all` "
        f"leg would be an argparse exit 2 on every tick")
