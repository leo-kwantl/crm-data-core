"""Daily snapshot helpers: KPI grain + pct-change / movement math.

Downstream contract (app serves, ML scores, DATA persists history):
- RFM already snapshots (rfm_snapshots); recs batch (rec_batches).
- CLV/churn/segments are live-compute only — 013_analytics_history.sql
  adds clv/churn snapshot tables + kpi_daily_snapshots + training runs.
- This module is the data-side runner math: pure functions (unit-tested
  without a DB) + the parameterised SQL the daily job uses.

Daily job: scripts/run_daily_snapshot.py --as-of YYYY-MM-DD
  (idempotent per day x scope via the COALESCE UNIQUE indexes).
"""
from __future__ import annotations


def pct_change(prev: float, cur: float) -> float | None:
    """Relative change (cur-prev)/prev. None when prev == 0 (undefined)."""
    if prev == 0:
        return None
    return (cur - prev) / prev


def kpi_movement(prev: dict, cur: dict) -> dict:
    """Compare two kpi_daily_snapshots rows -> deltas + pct per metric."""
    metrics = ("customers_total", "orders_total", "revenue_cents_total",
               "orders_day", "revenue_day_cents", "new_customers_day",
               "active_30d")
    deltas = {}
    for m in metrics:
        p, c = float(prev.get(m, 0)), float(cur.get(m, 0))
        deltas[m] = {"prev": p, "cur": c, "delta": c - p,
                     "pct": pct_change(p, c)}
    return {"from": prev.get("snapshot_date"), "to": cur.get("snapshot_date"),
            "deltas": deltas}


def clv_movement(prev_scores: dict, cur_scores: dict) -> dict:
    """Compare two CLV runs keyed by customer_id -> entered/exited/moved.

    Each score: {clv_cents, value_tier, p_alive}. Moved = tier changed or
    |clv| moved >= 10% (tunable threshold, kept explicit for tests).
    """
    prev_ids, cur_ids = set(prev_scores), set(cur_scores)
    both = prev_ids & cur_ids
    moved = []
    for cid in sorted(both):
        p, c = prev_scores[cid], cur_scores[cid]
        pc = pct_change(float(p.get("clv_cents", 0)), float(c.get("clv_cents", 0)))
        tier_flip = p.get("value_tier") != c.get("value_tier")
        if tier_flip or (pc is not None and abs(pc) >= 0.10):
            moved.append({"customer_id": cid,
                          "clv_prev": p.get("clv_cents"),
                          "clv_cur": c.get("clv_cents"),
                          "pct": pc, "tier_prev": p.get("value_tier"),
                          "tier_cur": c.get("value_tier")})
    return {"customers_prev": len(prev_ids), "customers_cur": len(cur_ids),
            "entered": sorted(cur_ids - prev_ids),
            "exited": sorted(prev_ids - cur_ids),
            "moved": moved}


# --- KPI daily-grain SQL (one row per day x scope, idempotent re-run) ---

KPI_DAILY_UPSERT_SQL = """
WITH day_bounds AS (
  SELECT ($1::date)::timestamptz AS d_from,
         (($1::date + 1))::timestamptz AS d_to,
         (($1::date - 30))::timestamptz AS act_from
),
scoped_orders AS (
  SELECT o.id, o.total_cents, o.ordered_at, o.customer_id
  FROM orders o
  JOIN stores s ON s.id = o.store_id
  JOIN brands b ON b.id = s.brand_id
  WHERE o.status = 'completed'
    AND ($2::uuid IS NULL OR s.brand_id = $2::uuid)
    AND ($3::uuid IS NULL OR b.group_id = $3::uuid)
),
scoped_customers AS (
  SELECT c.id, c.created_at
  FROM customers c
  WHERE c.deleted_at IS NULL
    AND ($3::uuid IS NULL OR c.group_id = $3::uuid)
    AND ($2::uuid IS NULL OR EXISTS (
      SELECT 1 FROM orders o2 JOIN stores s2 ON s2.id = o2.store_id
      WHERE o2.customer_id = c.id AND o2.status = 'completed'
        AND s2.brand_id = $2::uuid))
)
INSERT INTO kpi_daily_snapshots
  (snapshot_date, customers_total, orders_total, revenue_cents_total,
   orders_day, revenue_day_cents, new_customers_day, active_30d,
   brand_id, group_id)
SELECT $1::date,
  (SELECT COUNT(*) FROM scoped_customers),
  (SELECT COUNT(*) FROM scoped_orders),
  (SELECT COALESCE(SUM(total_cents), 0) FROM scoped_orders),
  (SELECT COUNT(*) FROM scoped_orders, day_bounds
    WHERE ordered_at >= d_from AND ordered_at < d_to),
  (SELECT COALESCE(SUM(total_cents), 0) FROM scoped_orders, day_bounds
    WHERE ordered_at >= d_from AND ordered_at < d_to),
  (SELECT COUNT(*) FROM scoped_customers, day_bounds
    WHERE created_at >= d_from AND created_at < d_to),
  (SELECT COUNT(DISTINCT customer_id) FROM scoped_orders, day_bounds
    WHERE ordered_at >= act_from AND ordered_at < d_to),
  $2::uuid, $3::uuid
ON CONFLICT DO NOTHING
RETURNING id, snapshot_date, customers_total, orders_total,
  revenue_cents_total,
  orders_day, revenue_day_cents, new_customers_day, active_30d
"""

KPI_TREND_SQL = """
SELECT snapshot_date, customers_total, orders_total, revenue_cents_total,
       orders_day, revenue_day_cents, new_customers_day, active_30d
FROM kpi_daily_snapshots
WHERE snapshot_date >= $1::date AND snapshot_date <= $2::date
  AND brand_id IS NOT DISTINCT FROM $3::uuid
  AND group_id IS NOT DISTINCT FROM $4::uuid
ORDER BY snapshot_date
"""

# --- Cohort retention snapshots (020, one table feeds line + heatmap) ---
#
# Cohort of a customer = date_trunc(granularity, first completed order).
# retained(offset k) = cohort members with >= 1 completed order in the
# period (cohort_period + k·granularity). retention_rate =
# retained / cohort_size (NULL when cohort_size = 0). Offset 0 is 1.0 by
# construction. Week periods start Monday (date_trunc('week')).
# Params: $1 granularity ('week'|'month'|'year'), $2 max_offset (>= 0),
# $3 brand_id scope (NULL = all), $4 group_id scope (NULL = all).
# Re-run = no-op via the grain UNIQUE in 020_cohort_snapshots.sql.

COHORT_GRANULARITIES = ("week", "month", "year")


def retention_rate(cohort_size: int, retained: int) -> float | None:
    """retained / cohort_size. None when the cohort is empty."""
    if cohort_size <= 0:
        return None
    return retained / cohort_size


COHORT_SNAPSHOT_UPSERT_SQL = """
WITH scoped_orders AS (
  SELECT o.customer_id, o.ordered_at
  FROM orders o
  JOIN stores s ON s.id = o.store_id
  JOIN brands b ON b.id = s.brand_id
  JOIN customers c ON c.id = o.customer_id
  WHERE o.status = 'completed'
    AND ($3::uuid IS NULL OR s.brand_id = $3::uuid)
    AND ($4::uuid IS NULL OR c.group_id = $4::uuid)
    AND ($4::uuid IS NULL OR b.group_id = $4::uuid)
),
cohort_of AS (
  SELECT customer_id,
         date_trunc($1::text, MIN(ordered_at))::date AS cohort
  FROM scoped_orders
  GROUP BY customer_id
),
cohort_size AS (
  SELECT cohort, COUNT(*) AS n FROM cohort_of GROUP BY cohort
),
offsets AS (
  SELECT generate_series(0, $2::int) AS k
),
periods AS (
  SELECT c.cohort, o.k,
    CASE $1::text
      WHEN 'week' THEN (c.cohort + (o.k * 7 || ' days')::interval)::date
      WHEN 'month' THEN (c.cohort + (o.k || ' months')::interval)::date
      ELSE (c.cohort + (o.k || ' years')::interval)::date
    END AS period_start
  FROM (SELECT DISTINCT cohort FROM cohort_of) c
  CROSS JOIN offsets o
),
retained AS (
  SELECT p.cohort, p.k, COUNT(DISTINCT so.customer_id) AS n
  FROM periods p
  JOIN cohort_of co ON co.cohort = p.cohort
  JOIN scoped_orders so ON so.customer_id = co.customer_id
    AND date_trunc($1::text, so.ordered_at)::date = p.period_start
  GROUP BY p.cohort, p.k
)
INSERT INTO cohort_snapshots
  (granularity, cohort_period, period_offset, cohort_size, retained,
   retention_rate, brand_id, group_id)
SELECT $1::text, p.cohort, p.k, cs.n,
       COALESCE(r.n, 0),
       CASE WHEN cs.n = 0 THEN NULL
            ELSE COALESCE(r.n, 0)::float / cs.n END,
       $3::uuid, $4::uuid
FROM periods p
JOIN cohort_size cs ON cs.cohort = p.cohort
LEFT JOIN retained r ON r.cohort = p.cohort AND r.k = p.k
ON CONFLICT DO NOTHING
"""

COHORT_TREND_SQL = """
SELECT cohort_period, period_offset, cohort_size, retained, retention_rate
FROM cohort_snapshots
WHERE granularity = $1::text
  AND brand_id IS NOT DISTINCT FROM $2::uuid
  AND group_id IS NOT DISTINCT FROM $3::uuid
ORDER BY cohort_period, period_offset
"""

# --- Twin as-of cutoffs (LD5, no migration) ---
#
# Two ways to read "as of" without duplicating tables, stolen from
# legacy's vw_churn_clv_inference_latest/current pair (identical views,
# different cutoff):
#   latest  = MAX(date) present in the data (data-max cutoff; the table
#             may lag wall-clock when a feed stalls).
#   current = yesterday wall-clock (CURRENT_DATE - 1; complete-day rule).
# The daily job resolves the data-max cutoff with LATEST_*_SQL, then
# serves either cutoff through the same trend queries by passing the
# resolved date as the end bound. Pure-Python resolve_asof() keeps the
# rule unit-testable without a DB.
from datetime import date as _date, timedelta as _timedelta

ASOF_MODES = ("latest", "current")


def resolve_asof(mode: str, latest_in_data: _date,
                 today: _date) -> _date:
    """Resolve an as-of cutoff date for a snapshot read.

    latest  -> latest_in_data (data-max; feeds may lag wall-clock).
    current -> today - 1 day (complete-day wall-clock rule).
    Raises ValueError on unknown mode.
    """
    if mode == "latest":
        return latest_in_data
    if mode == "current":
        return today - _timedelta(days=1)
    raise ValueError(f"unknown as-of mode: {mode!r} "
                     f"(expected one of {ASOF_MODES})")


LATEST_KPI_DATE_SQL = """
SELECT MAX(snapshot_date) FROM kpi_daily_snapshots
WHERE brand_id IS NOT DISTINCT FROM $1::uuid
  AND group_id IS NOT DISTINCT FROM $2::uuid
"""

LATEST_COHORT_DATE_SQL = """
SELECT MAX(cohort_period) FROM cohort_snapshots
WHERE granularity = $1::text
  AND brand_id IS NOT DISTINCT FROM $2::uuid
  AND group_id IS NOT DISTINCT FROM $3::uuid
"""


# --- LD8 time-aware RFM movement (joint contract, ml executes verbatim).
RFM_WINDOWS = ("current", "mom", "yoy", "lifetime")
MOVEMENT_PAIRS = ("current_x_mom", "current_x_yoy")
# Customers present in only one window of the pair. Uppercase by
# design: real segments are lowercase, so these never collide.
ENTERED = "ENTERED"
EXITED = "EXITED"


def validate_segment_size(spec: dict) -> dict:
    """Validate an rfm_segment_sizes row. Returns a cleaned copy."""
    if not isinstance(spec, dict):
        raise ValueError("segment size must be a mapping")
    window = spec.get("window_key")
    if window not in RFM_WINDOWS:
        raise ValueError(
            f"window_key must be one of {RFM_WINDOWS}, got {window!r}")
    segment = spec.get("segment")
    if (not isinstance(segment, str) or not segment.strip()
            or len(segment) > 64):
        raise ValueError(
            f"segment must be 1..64 chars, got {segment!r}")
    n = spec.get("n")
    if isinstance(n, bool) or not isinstance(n, int) or n < 0:
        raise ValueError(f"n must be an int >= 0, got {n!r}")
    medians = {}
    for key in ("median_r", "median_f", "median_m"):
        value = spec.get(key)
        if (isinstance(value, bool)
                or not isinstance(value, (int, float))):
            raise ValueError(f"{key} must be numeric, got {value!r}")
        medians[key] = float(value)
    return {"window_key": window, "segment": segment.strip(), "n": n,
            **medians}


def validate_movement_row(spec: dict) -> dict:
    """Validate an rfm_segment_movement row. Returns a cleaned copy.

    Flow rows (real segment -> real segment) carry pct_of_from in
    0..1 over customers present in BOTH windows. ENTERED/EXITED rows
    must leave pct_of_from NULL: they sit outside the flow pct
    denominator by construction.
    """
    if not isinstance(spec, dict):
        raise ValueError("movement row must be a mapping")
    pair = spec.get("pair")
    if pair not in MOVEMENT_PAIRS:
        raise ValueError(
            f"pair must be one of {MOVEMENT_PAIRS}, got {pair!r}")
    flow = True
    for key in ("from_segment", "to_segment"):
        value = spec.get(key)
        if (not isinstance(value, str) or not value.strip()
                or len(value) > 64):
            raise ValueError(
                f"{key} must be 1..64 chars, got {value!r}")
    from_seg = spec["from_segment"].strip()
    to_seg = spec["to_segment"].strip()
    if ENTERED in (from_seg, to_seg) or EXITED in (from_seg, to_seg):
        flow = False
        if from_seg == EXITED or to_seg == ENTERED:
            raise ValueError(
                "sentinel direction: ENTERED only as from_segment, "
                f"EXITED only as to_segment (got {from_seg!r} -> "
                f"{to_seg!r})")
        if from_seg == ENTERED and to_seg == EXITED:
            raise ValueError("ENTERED -> EXITED is not a movement")
    n = spec.get("n")
    if isinstance(n, bool) or not isinstance(n, int) or n < 0:
        raise ValueError(f"n must be an int >= 0, got {n!r}")
    pct = spec.get("pct_of_from")
    if flow:
        if (isinstance(pct, bool) or not isinstance(pct, (int, float))
                or not 0 <= pct <= 1):
            raise ValueError(
                "flow rows need pct_of_from in 0..1 over both-window "
                f"customers, got {pct!r}")
        pct = float(pct)
    elif pct is not None:
        raise ValueError(
            "ENTERED/EXITED rows must leave pct_of_from NULL "
            "(outside the flow pct denominator)")
    return {"pair": pair, "from_segment": from_seg,
            "to_segment": to_seg, "n": n, "pct_of_from": pct}


SEGMENT_SIZE_UPSERT_SQL = """
INSERT INTO rfm_segment_sizes
  (as_of, window_key, segment, n, median_r, median_f, median_m)
VALUES ($1::date, $2, $3, $4, $5, $6, $7)
ON CONFLICT (as_of, window_key, segment) DO NOTHING
"""

MOVEMENT_UPSERT_SQL = """
INSERT INTO rfm_segment_movement
  (as_of, pair, from_segment, to_segment, n, pct_of_from)
VALUES ($1::date, $2, $3, $4, $5, $6)
ON CONFLICT (as_of, pair, from_segment, to_segment) DO NOTHING
"""

SIZES_FOR_WINDOW_SQL = """
SELECT segment, n, median_r, median_f, median_m
FROM rfm_segment_sizes
WHERE as_of = $1::date AND window_key = $2
ORDER BY segment
"""

MOVEMENT_FOR_PAIR_SQL = """
SELECT from_segment, to_segment, n, pct_of_from
FROM rfm_segment_movement
WHERE as_of = $1::date AND pair = $2
ORDER BY from_segment, to_segment
"""
