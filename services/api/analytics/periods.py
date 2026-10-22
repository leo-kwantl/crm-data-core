"""Period-over-period comparisons (MoM / YoY) for the dashboard and the
analytics pages -- read-only serving.

WHY THIS EXISTS
Business users read a number ("revenue is $847") and immediately ask the
follow-up question: is that up or down, and against what? A figure with no
baseline is not actionable, so every headline metric here ships with a
month-over-month and a year-over-year percentage change, plus the
plain year-to-date total for users who think in calendar years.

WINDOW SEMANTICS (see db/queries/analytics_comparisons.sql for the SQL)
Calendar month-to-date, always like-for-like:
    current : 1st of the reference month -> reference day (inclusive)
    MoM     : the SAME day range of the previous month
    YoY     : the SAME day range of the same month one year earlier
    YTD     : 1st of January -> reference day (a total, never a %)
Cutting all three at the same day-of-month is the whole point: comparing a
9-day month-to-date against a full 30-day previous month would make every
business look like it collapsed, and comparing it against a full previous
year would make every business look like it exploded. Neither is a real
change in performance.

The reference date is the DATA's max order date, not now(), so a frozen
seed keeps reporting the month it actually contains.

HONESTY RULES (the reason this module is more than a subtraction)
- A baseline whose window does not overlap the data at all is reported as
  available=false with reason="no_data", NOT as a -100% collapse. On a
  dataset with only 12 months of history every YoY baseline is legitimately
  absent, and the UI says so instead of inventing a number.
- A baseline that only partially overlaps the data -- or whose window is
  not the same length as the current one (a previous month too short to
  hold the same number of days) -- is not like-for-like: it is flagged
  coverage="partial" and the percentage is withheld with
  reason="partial_baseline" rather than shown as if comparable.
- A baseline of exactly 0 with real data in the window yields pct=None,
  reason="zero_baseline" -- "up from nothing" is not a percentage.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import asyncpg
from fastapi import HTTPException

from . import kpi_rollup

QUERIES_PATH = (Path(__file__).resolve().parents[3] / "db" / "queries"
                / "analytics_comparisons.sql")

_NAME_RE = re.compile(r"^--\s*name:\s*(\w+)\s*$", re.M)


def load_queries() -> dict[str, str]:
    text = QUERIES_PATH.read_text(encoding="utf-8")
    parts = _NAME_RE.split(text)
    out: dict[str, str] = {}
    for i in range(1, len(parts) - 1, 2):
        name, sql = parts[i], parts[i + 1]
        lines = [ln for ln in sql.splitlines()
                 if not ln.strip().startswith("-- name:")]
        out[name] = "\n".join(lines).strip().rstrip(";")
    return out


Q = load_queries()

# SQL column prefix -> response key. The query returns <prefix>_current,
# <prefix>_mom and <prefix>_yoy for each of these.
METRICS: tuple[tuple[str, str], ...] = (
    ("revenue", "revenue_cents"),
    ("orders", "orders"),
    ("buyers", "buyers"),
    ("new_customers", "new_customers"),
)

DEFAULT_SERIES_MONTHS = 13

# LD5 as-of vocabulary for trend reads. `latest` pins the reference to the
# freshest data in scope (max order date); `current` pins it to today, which
# may leave a trailing gap the response names via data_through instead of
# silently short-charting.
ASOF_MODES = ("latest", "current")


def parse_mode(mode: str | None) -> str:
    """?mode= validation: absent -> latest default; unknown -> 422."""
    if mode is None or mode == "latest":
        return "latest"
    if mode == "current":
        return "current"
    raise HTTPException(status_code=422, detail={"code": "bad_mode",
                                                "value": mode})

# A month built on a handful of orders swings wildly on one extra sale, so a
# baseline below this many orders is flagged low_sample and the UI shows the
# raw baseline next to the percentage instead of the percentage alone. This is
# the difference between "+126%" (unusable) and "+126%, up from $376 across 6
# orders" (a business user can weigh that).
LOW_SAMPLE_ORDERS = 10


def _iso(value: date | None) -> str | None:
    return value.isoformat() if value else None


def _coverage(start: date, end: date, data_min: date | None,
              data_max: date | None, expected_days: int | None = None) -> str:
    """How much of the half-open window [start, end) the data covers.

    "none"    -> the window predates the data entirely (or there is none)
    "partial" -> the window is only partly covered by the data, OR it is not
                 the same length as the current window (a previous month too
                 short to hold the same number of days is not like-for-like)
    "full"    -> the data spans the whole window and the span matches
    """
    if data_min is None or data_max is None:
        return "none"
    if start > data_max or end <= data_min:
        return "none"
    if expected_days is not None and (end - start).days != expected_days:
        return "partial"
    if start < data_min or end > data_max + timedelta(days=1):
        return "partial"
    return "full"


def _delta(current: int | None, baseline: int | None, coverage: str,
           baseline_orders: int | None = None) -> dict[str, Any]:
    """One comparison arm: percentage change plus an explicit reason when
    there is no honest percentage to show."""
    low = baseline_orders is not None and baseline_orders < LOW_SAMPLE_ORDERS
    if coverage == "none":
        return {"baseline": None, "pct": None, "coverage": "none",
                "reason": "no_data", "low_sample": low}
    if baseline is None:
        return {"baseline": None, "pct": None, "coverage": coverage,
                "reason": "no_data", "low_sample": low}
    if coverage == "partial":
        # Not like-for-like (data gap, or a clamped short previous month):
        # the percentage would over- or under-state the change, so it is
        # withheld, not faked. The raw baseline is still reported.
        return {"baseline": int(baseline), "pct": None,
                "coverage": "partial", "reason": "partial_baseline",
                "low_sample": low}
    if baseline == 0:
        return {"baseline": 0, "pct": None, "coverage": coverage,
                "reason": "zero_baseline", "low_sample": low}
    cur = current or 0
    return {"baseline": int(baseline),
            "pct": round((cur - int(baseline)) / int(baseline), 4),
            "coverage": coverage, "reason": None, "low_sample": low}


def _ratio(num: Any, den: Any) -> float | None:
    """A per-unit ratio (avg order value, units per order, spend per buyer).

    None when the denominator is missing or zero: "no orders" is not a
    $0.00 average. Callers keep the existing envelope (0.0 for an empty
    current window, honest delta reasons for the baselines) and the UI
    renders an uncomputable figure as a blank cell, never a fake zero."""
    if num is None or den is None:
        return None
    try:
        d = float(den)
    except (TypeError, ValueError):
        return None
    if d == 0:
        return None
    return float(num) / d


def _ratio_delta(cur_num: Any, cur_den: Any, base_num: Any, base_den: Any,
                 coverage: str, baseline_orders: int | None = None) -> dict[str, Any]:
    """MoM/YoY arm for a ratio: same honesty rules as _delta, applied to
    the per-unit figures. A baseline ratio of exactly 0 (orders but no
    revenue) is "up from nothing", not a percentage."""
    low = baseline_orders is not None and baseline_orders < LOW_SAMPLE_ORDERS
    base = _ratio(base_num, base_den)
    if coverage == "none" or base is None:
        return {"baseline": base, "pct": None, "coverage":
                "none" if coverage == "none" else coverage,
                "reason": "no_data", "low_sample": low}
    if coverage == "partial":
        return {"baseline": base, "pct": None,
                "coverage": "partial", "reason": "partial_baseline",
                "low_sample": low}
    if base == 0:
        return {"baseline": base, "pct": None, "coverage": coverage,
                "reason": "zero_baseline", "low_sample": low}
    cur = _ratio(cur_num, cur_den) or 0.0
    return {"baseline": base,
            "pct": round((cur - base) / base, 4),
            "coverage": coverage, "reason": None, "low_sample": low}


def _windows(bounds: asyncpg.Record) -> dict[str, dict[str, Any]]:
    current: dict[str, Any] = {"start": _iso(bounds["cur_start"]),
                "end_exclusive": _iso(bounds["cur_end"])}
    return {
        # Task 65: "mtd" names the current-month window explicitly. Same
        # span as "current" (kept so existing readers never change).
        "mtd": dict(current),
        "current": current,
        "ytd": {"start": _iso(bounds["ytd_start"]),
                "end_exclusive": _iso(bounds["ytd_end"])},
        "mom": {"start": _iso(bounds["mom_start"]),
                "end_exclusive": _iso(bounds["mom_end"])},
        "yoy": {"start": _iso(bounds["yoy_start"]),
                "end_exclusive": _iso(bounds["yoy_end"])},
    }


def _reference(bounds: asyncpg.Record, mode: str) -> dict[str, Any]:
    data_min = bounds["data_min"]
    data_max = bounds["data_max"]
    cur_days = (bounds["cur_end"] - bounds["cur_start"]).days
    return {
        "ref_date": _iso(bounds["ref_date"]),
        "data_min": _iso(data_min),
        "data_max": _iso(data_max),
        # LD5 gap marker: the freshest data date, named explicitly so a
        # `current` read never passes trailing zero buckets off as decline.
        "data_through": _iso(data_max),
        "mode": mode,
        "windows": _windows(bounds),
        "coverage": {
            "mom": _coverage(bounds["mom_start"], bounds["mom_end"], data_min,
                             data_max, cur_days),
            "yoy": _coverage(bounds["yoy_start"], bounds["yoy_end"], data_min,
                             data_max, cur_days),
        },
    }


async def _bounds(conn: asyncpg.Connection, ref_date: date | None,
                  brand_id: Any, group_id: Any) -> asyncpg.Record:
    return await conn.fetchrow(Q["window_bounds"], ref_date, brand_id, group_id)


async def get_comparison(pool: asyncpg.Pool,
                         ref_date: date | None = None,
                         brand_id: Any = None,
                         group_id: Any = None,
                         mode: str = "latest") -> dict[str, Any]:
    """Reference windows + the four headline metrics with MoM and YoY arms.

    Each metric also carries its year-to-date total (1 Jan -> reference
    day) as a plain figure with no percentage: the year is still in
    progress, so no like-for-like baseline exists. The UI decides which of
    MTD / YTD / MoM / YoY to show via the display-settings control.

    One endpoint serves the dashboard KPI strip and every analytics page, so
    the comparison semantics are defined exactly once. brand_id / group_id
    come from scope_filters(user) and narrow every aggregate to the caller's
    session scope (see db/queries/analytics_comparisons.sql).
    """
    # An explicit ref_date is already an as-of pin, so it wins over mode;
    # otherwise `current` pins the reference (and the series anchor) to
    # today while `latest` falls back to the freshest data in scope.
    anchor = ref_date
    if anchor is None and mode == "current":
        anchor = date.today()
    async with pool.acquire() as conn:
        bounds = await _bounds(conn, anchor, brand_id, group_id)
        args = (bounds["cur_start"], bounds["cur_end"],
                bounds["mom_start"], bounds["mom_end"],
                bounds["yoy_start"], bounds["yoy_end"],
                bounds["ytd_start"], bounds["ytd_end"],
                brand_id, group_id)
        row = await conn.fetchrow(Q["overview_comparison"], *args)
        stores = await conn.fetch(Q["store_windows"], *args)
        cohorts = await conn.fetch(Q["customer_cohort_windows"],
                                   *args[:6], group_id)
        series = await conn.fetch(Q["monthly_series"], DEFAULT_SERIES_MONTHS,
                                  anchor, brand_id, group_id)
        # Item 1: the dashboard reads the rollup. The daily curve out of
        # `kpi_daily_snapshots` (parsed from the shared file, never copied),
        # scope-exact, ending on the same reference day as the windows.
        # A scope with no snapshot rows reads an empty curve, not a 404.
        # An orderless scope has no reference day (the bounds resolve
        # NULL); the curve then ends today instead of failing the page.
        rollup = await kpi_rollup.get_rollup(
            conn, as_of=bounds["ref_date"] or date.today(),
            brand_id=brand_id, group_id=group_id)

    data_min, data_max = bounds["data_min"], bounds["data_max"]
    cur_days = (bounds["cur_end"] - bounds["cur_start"]).days
    mom_cov = _coverage(bounds["mom_start"], bounds["mom_end"], data_min,
                        data_max, cur_days)
    yoy_cov = _coverage(bounds["yoy_start"], bounds["yoy_end"], data_min,
                        data_max, cur_days)

    metrics: dict[str, Any] = {}
    for column, key in METRICS:
        current = row[f"{column}_current"]
        metrics[key] = {
            "current": int(current) if current is not None else 0,
            "mom": _delta(current, row[f"{column}_mom"], mom_cov,
                          row["orders_mom"]),
            "yoy": _delta(current, row[f"{column}_yoy"], yoy_cov,
                          row["orders_yoy"]),
            "ytd": _ytd_total(row[f"{column}_ytd"]),
        }
    # Task 64 (additive): total units sold reuses the count path exactly --
    # the SQL SUM is NULL for an empty window, like revenue_*.
    for column, key in (("items", "items_units"),):
        current = row[f"{column}_current"]
        metrics[key] = {
            "current": int(current) if current is not None else 0,
            "mom": _delta(current, row[f"{column}_mom"], mom_cov,
                          row["orders_mom"]),
            "yoy": _delta(current, row[f"{column}_yoy"], yoy_cov,
                          row["orders_yoy"]),
            "ytd": _ytd_total(row[f"{column}_ytd"]),
        }
    # Task 64 (additive): per-unit business ratios with MoM/YoY arms.
    # Shape mirrors a count metric ({current, mom, yoy, ytd: {total}}) so
    # the api.ts parser grows by one cmpMetric() call per key. Money ratios
    # stay in cents (float); the UI rounds to whole cents via formatCents.
    # avg buyer spend is the window's "avg lifetime spending" read: mean
    # revenue across the buyers active in the window.
    for key, num_cols, den_cols in (
        ("avg_order_cents", ("revenue_current", "revenue_mom",
         "revenue_yoy", "revenue_ytd"), ("orders_current", "orders_mom",
         "orders_yoy", "orders_ytd")),
        ("avg_items_per_order", ("items_current", "items_mom",
         "items_yoy", "items_ytd"), ("orders_current", "orders_mom",
         "orders_yoy", "orders_ytd")),
        ("avg_buyer_cents", ("revenue_current", "revenue_mom",
         "revenue_yoy", "revenue_ytd"), ("buyers_current", "buyers_mom",
         "buyers_yoy", "buyers_ytd")),
    ):
        n_cur, n_mom, n_yoy, n_ytd = (row[c] for c in num_cols)
        d_cur, d_mom, d_yoy, d_ytd = (row[c] for c in den_cols)
        cur_ratio = _ratio(n_cur, d_cur)
        ytd_ratio = _ratio(n_ytd, d_ytd)
        metrics[key] = {
            "current": cur_ratio if cur_ratio is not None else 0.0,
            "mom": _ratio_delta(n_cur, d_cur, n_mom, d_mom, mom_cov,
                                 row["orders_mom"]),
            "yoy": _ratio_delta(n_cur, d_cur, n_yoy, d_yoy, yoy_cov,
                                 row["orders_yoy"]),
            "ytd": {"total": ytd_ratio
                      if ytd_ratio is not None else 0.0},
        }
    # Task 65 (additive): explicit MTD total on every metric. MTD is the
    # current-month window, so the total equals current; the UI shows it as
    # its own Display-gated figure beside YTD.
    for metric in metrics.values():
        metric["mtd"] = {"total": metric["current"]}

    store_rows = [_store_row(r, mom_cov, yoy_cov) for r in stores]
    return {
        "reference": _reference(bounds, mode),
        "metrics": metrics,
        "stores": store_rows,
        "brands": _rollup(store_rows, "brand_code", "brand_code",
                          mom_cov, yoy_cov),
        "groups": _rollup(store_rows, "group_code", "group_code",
                          mom_cov, yoy_cov),
        "customer_groups": [
            {
                "group_code": r["group_code"],
                "group_name": r["group_name"],
                "revenue_current": int(r["revenue_current"]),
                "revenue_mom": int(r["revenue_mom"]),
                "revenue_yoy": int(r["revenue_yoy"]),
                "orders_current": int(r["orders_current"]),
                "buyers_current": int(r["buyers_current"]),
            }
            for r in cohorts
        ],
        "monthly_series": [
            {
                "month": r["month"],
                "revenue_cents": int(r["revenue_cents"]),
                "orders": int(r["orders"]),
                "new_customers": int(r["new_customers"]),
            }
            for r in series
        ],
        "rollup": rollup,
    }


def _ytd_total(value: Any) -> dict[str, Any]:
    """Year-to-date figure: a plain total, never a percentage.

    The year is still in progress, so there is no like-for-like baseline
    to compare against -- any % would compare a partial year with a full
    one. None (empty window) reports as 0: a year with no orders yet is a
    real zero, not missing data."""
    return {"total": int(value) if value is not None else 0}


def _store_row(r: asyncpg.Record, mom_cov: str, yoy_cov: str) -> dict[str, Any]:
    current = int(r["revenue_current"])
    return {
        "store_code": r["store_code"],
        "store_name": r["store_name"],
        "brand_code": r["brand_code"],
        "group_code": r["group_code"],
        "revenue_current": current,
        # The comparison baseline, top-level, on EVERY comparison row. The
        # brand and group rollups below carry `revenue_mom` / `revenue_yoy`
        # this way, and the client maps all three row kinds through one
        # helper (`cmpGroupRow` in apps/web/lib/api.ts) that reads
        # `revenue_mom`. Without it the helper read undefined and printed
        # 0, so every store chip on /overview claimed the store's revenue
        # "came from $0" while the real baseline sat inside `mom.baseline`.
        "revenue_mom": int(r["revenue_mom"]),
        "revenue_yoy": int(r["revenue_yoy"]),
        "orders_current": int(r["orders_current"]),
        "orders_mom": int(r["orders_mom"]),
        "orders_yoy": int(r["orders_yoy"]),
        "buyers_current": int(r["buyers_current"]),
        "mom": _delta(current, r["revenue_mom"], mom_cov, r["orders_mom"]),
        "yoy": _delta(current, r["revenue_yoy"], yoy_cov, r["orders_yoy"]),
        "ytd": _ytd_total(r["revenue_ytd"]),
        "orders_ytd": int(r["orders_ytd"]),
    }


def _rollup(rows: list[dict[str, Any]], field: str, key: str,
            mom_cov: str, yoy_cov: str) -> list[dict[str, Any]]:
    """Collapse the per-store rows into brand / group buckets.

    Rollups are computed on the summed revenue rather than by averaging the
    per-store percentages -- averaging percentages would let a $2 store swing
    a $3000 brand. A bucket whose stores are all unnamed collapses to a
    single null-key row and is labelled by the caller.

    A bucket inherits the scope-wide coverage, downgraded to "partial" when
    ANY contributing store has no baseline at all: summing a missing store as
    zero understates the baseline and would overstate growth, so the bucket
    withholds the percentage instead.
    """
    buckets: dict[Any, dict[str, Any]] = {}
    for row in rows:
        name = row[field]
        bucket = buckets.setdefault(name, {
            key: name, "revenue_current": 0, "revenue_mom": 0, "revenue_yoy": 0,
            "orders_current": 0, "orders_mom": 0, "orders_yoy": 0,
            "buyers_current": 0, "store_count": 0,
            "mom_missing": False, "yoy_missing": False,
        })
        bucket["revenue_current"] += row["revenue_current"]
        bucket["revenue_mom"] += row["mom"]["baseline"] or 0
        bucket["revenue_yoy"] += row["yoy"]["baseline"] or 0
        bucket["orders_current"] += row["orders_current"]
        bucket["orders_mom"] += row["orders_mom"]
        bucket["orders_yoy"] += row["orders_yoy"]
        bucket["buyers_current"] += row["buyers_current"]
        bucket["store_count"] += 1
        bucket["mom_missing"] |= row["mom"]["baseline"] is None
        bucket["yoy_missing"] |= row["yoy"]["baseline"] is None

    out: list[dict[str, Any]] = []
    for bucket in buckets.values():
        b_mom = "none" if mom_cov == "none" else (
            "partial" if bucket["mom_missing"] else mom_cov)
        b_yoy = "none" if yoy_cov == "none" else (
            "partial" if bucket["yoy_missing"] else yoy_cov)
        bucket["mom"] = _delta(bucket["revenue_current"],
                               bucket["revenue_mom"], b_mom,
                               bucket["orders_mom"])
        bucket["yoy"] = _delta(bucket["revenue_current"],
                               bucket["revenue_yoy"], b_yoy,
                               bucket["orders_yoy"])
        out.append(bucket)
    out.sort(key=lambda b: b["revenue_current"], reverse=True)
    return out
