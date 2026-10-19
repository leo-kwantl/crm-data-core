"""The forecast window and the seasonality it must identify are one number.

`ml_engine/forecast.py` fits its seasonal candidate only when the window holds
`SEASON_MIN_CYCLES` observations per season position -- in practice
`lookback_weeks - LOST_WINDOW_WEEKS >= SEASON_MIN_CYCLES * season_period`,
because the window's own last week (the one `as_of` falls in) is dropped
before the fit. The manifests pinned 52 weeks against an annual
`season_period` of 52, so that bar was missed for every production series: the
seasonal branch was unreachable, every series came from the level fallback,
and each batch reported `seasonality` unmeasured while looking healthy (right
up until ml `ee271ca` started saying so on stderr). The first fix was 104,
which still squeezed in at 103 fitted weeks = 1.98 cycles and was refused; the
window is 106. The guard is these assertions -- and it is written against the
bar, not the number, so 104 alone would go red on the lost week rather than
pass on the arithmetic.

Why a test instead of a comment: the two numbers live in two repos and were
kept in agreement by memory. This reads the pinned manifest row AND ml's
constant, so an edit to either side alone turns red -- the same class of
drift as the app/data mirror. In a data-only checkout ml's numbers are
absent, and the bar degrades to the documented floor rather than the test
disappearing: a guard that skips is a guard that reports success where it
could not run.
"""

from __future__ import annotations

import ast
from datetime import date
from pathlib import Path

import pytest
import yaml

from pipelines import schedule as sched

REPO = Path(__file__).resolve().parents[3]
ML_FORECAST = REPO.parent / "crm-cdp-ml" / "ml_engine" / "forecast.py"

# ml's constants, duplicated on purpose: a duplicate that is *checked against
# the original whenever the sibling is present* is a pin, not a copy. They are
# the floor, not a decoration -- with only the sibling read, a data-only
# checkout skipped both arithmetic tests and the file reported green, i.e. the
# guard was silent in the one checkout shape it protects. When the sibling is
# absent the bar degrades to the floor and the test still runs; when the
# sibling exists but the constant is gone the test fails (ml renamed what this
# reads).
SEASON_MIN_CYCLES_FLOOR = 2
LOST_WINDOW_WEEKS_FLOOR = 1


def _ml_int_constant(name: str) -> int:
    """Read a module-level integer constant from ml's forecast module.

    Parsed, not imported: the ml checkout is a sibling repo (absent in a
    data-only CI run) and importing it would drag numpy in for one integer.
    A rename or a move fails here loudly -- which is the point.
    """
    tree = ast.parse(ML_FORECAST.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if (isinstance(target, ast.Name) and target.id == name
                        and isinstance(node.value, ast.Constant)
                        and isinstance(node.value.value, int)):
                    return node.value.value
    raise AssertionError(f"{name} is not a module-level integer in "
                         f"{ML_FORECAST}")


def _ml_int_constant_or(floor: int, name: str) -> int:
    """ml's value when its checkout sits beside this one, else the floor."""
    if not ML_FORECAST.exists():
        return floor
    return _ml_int_constant(name)


def _argv_value(argv: list[str], flag: str) -> int:
    assert flag in argv, f"{flag} not threaded: {argv}"
    return int(argv[argv.index(flag) + 1])


def _ctx(**kw):
    base = {"pipeline": "test_pipe", "as_of": date(2026, 10, 1),
            "brand_id": None, "group_id": None, "created_by": "test",
            "ml_repo": REPO, "db_url": "postgresql://fake/db"}
    base.update(kw)
    return sched.JobCtx(**base)


def _forecast_jobs(database_url: str) -> list[tuple[str, dict]]:
    """`(pipeline, params)` for every stored manifest's forecast job."""
    import asyncio

    import asyncpg

    async def _read():
        conn = await asyncpg.connect(database_url)
        try:
            rows = await conn.fetch(
                "SELECT DISTINCT ON (pipeline) pipeline, yaml"
                " FROM pipeline_manifests ORDER BY pipeline, version DESC")
        finally:
            await conn.close()
        out = []
        for r in rows:
            doc = yaml.safe_load(r["yaml"])
            jobs = (doc.get("pipelines", {}).get(r["pipeline"], {})
                    .get("jobs", {}))
            if "forecast" in jobs:
                out.append((r["pipeline"], jobs["forecast"].get("params")
                            or {}))
        return out

    return asyncio.run(_read())


def test_stored_manifest_window_identifies_the_annual_cycle(
        database_url: str) -> None:
    """Every stored manifest's pinned window clears the identifiability bar."""
    cycles = _ml_int_constant_or(SEASON_MIN_CYCLES_FLOOR, "SEASON_MIN_CYCLES")
    lost = _ml_int_constant_or(LOST_WINDOW_WEEKS_FLOOR, "LOST_WINDOW_WEEKS")
    jobs = _forecast_jobs(database_url)
    assert jobs, ("no stored manifest has a forecast job -- this test would "
                  "pass vacuously")
    for pipeline, params in jobs:
        lookback = params.get("lookback_weeks")
        period = params.get("season_period", 52)
        assert lookback is not None, (
            f"{pipeline}: forecast pins no lookback_weeks, so the code "
            f"default decides -- see the default test below")
        assert lookback >= cycles * period + lost, (
            f"{pipeline}: lookback_weeks {lookback} < {cycles} x "
            f"season_period {period} + {lost} (the half-observed last week "
            f"the fit drops) -> every series falls back to level and "
            f"`seasonality` is unmeasured, silently")


def test_code_default_window_identifies_the_annual_cycle() -> None:
    """A manifest that pins nothing still gets an identifiable window."""
    cycles = _ml_int_constant_or(SEASON_MIN_CYCLES_FLOOR, "SEASON_MIN_CYCLES")
    lost = _ml_int_constant_or(LOST_WINDOW_WEEKS_FLOOR, "LOST_WINDOW_WEEKS")
    argv = sched.ENGINE_BUILDERS["forecast_snapshot"]({}, _ctx())
    lookback = _argv_value(argv, "--lookback-weeks")
    period = _argv_value(argv, "--season-period")
    assert lookback >= cycles * period + lost, (
        f"code default lookback_weeks {lookback} < {cycles} x "
        f"season_period {period} + {lost} fitted week(s)")
    assert period == 52, "the annual season is the default ml fits"


def test_manifest_season_period_reaches_the_cli() -> None:
    """Both numbers travel, so the pinned pair is the pair that runs."""
    argv = sched.ENGINE_BUILDERS["forecast_snapshot"](
        {"lookback_weeks": 106, "season_period": 52}, _ctx())
    assert _argv_value(argv, "--lookback-weeks") == 106
    assert _argv_value(argv, "--season-period") == 52
