"""Dashboard stats: pure-SQL aggregates for charts (read-only, no PII).

One round-trip per section (4 queries): totals, revenue_by_month,
orders_by_store, new_customers_by_month. Month buckets cover the last 6
calendar months INCLUDING the current month; empty months yield zero rows
(zero-filled in Python so the shape is stable for charts).

Scope: every aggregate honours the caller's (brand, group) session scope
(NULL/NULL = HQ global). Orders/revenue scope via the order's store
brand + customer/group brand; customers scope via group link or an order
in a brand store; stores scope via their brand. Campaigns have no brand
column: active_campaigns is global reference data (same rule as the
connectors registry). Quarantine-by-default: scoped sessions never see
other scopes' rows.
"""

from __future__ import annotations

import asyncpg

# Scope predicates ($1 = brand_id-or-null, $2 = group_id-or-null).
# HQ (NULL/NULL) passes everything; scoped sessions see only their rows.
_ORDER_SCOPE = """($1::uuid IS NULL OR s.brand_id = $1::uuid)
  AND ($2::uuid IS NULL OR c.group_id = $2::uuid)
  AND ($2::uuid IS NULL OR b.group_id = $2::uuid)"""
_CUST_SCOPE = """($2::uuid IS NULL OR c.group_id = $2::uuid)
  AND ($1::uuid IS NULL OR EXISTS
    (SELECT 1 FROM orders o JOIN stores s ON s.id = o.store_id
     WHERE o.customer_id = c.id AND s.brand_id = $1::uuid))"""

# Business-timezone pin for every bucket boundary in this module. Buckets
# are HK calendar periods, NOT DB-session-zone periods: a 16:00Z+ instant
# is the NEXT business day in Asia/Hong_Kong, and the snapshot tables
# (cohort_snapshots, rfm_*) are written on that same business day. Must
# stay in lockstep with ../crm-cdp-data `pipelines/snapshot.py` `f889386`
# (that file's last change, where `BUSINESS_TZ_*` sits at `:142-145`). pgembed
# ships NO PG tzdata, so the NAMED zone cannot appear in SQL ("time zone
# \"Asia/Hong_Kong\" not recognized"); Hong Kong has no DST, so the fixed
# offset is semantically identical and the name stays attached here.
BUSINESS_TZ_NAME = "Asia/Hong_Kong"
BUSINESS_TZ_OFFSET_HOURS = 8
BUSINESS_TZ_SQL = "AT TIME ZONE interval '8 hours'"

TOTALS_SQL = """
SELECT (SELECT COUNT(*) FROM customers c
        WHERE c.deleted_at IS NULL AND """ + _CUST_SCOPE + """) AS customers,
       (SELECT COUNT(*) FROM orders o
        JOIN stores s ON s.id = o.store_id
        LEFT JOIN customers c ON c.id = o.customer_id
        LEFT JOIN brands b ON b.id = s.brand_id
        WHERE """ + _ORDER_SCOPE + """) AS orders,
       (SELECT COALESCE(SUM(o.total_cents), 0) FROM orders o
        JOIN stores s ON s.id = o.store_id
        LEFT JOIN customers c ON c.id = o.customer_id
        LEFT JOIN brands b ON b.id = s.brand_id
        WHERE """ + _ORDER_SCOPE + """) AS revenue_cents,
       (SELECT COUNT(*) FROM campaigns WHERE status = 'active') AS active_campaigns
"""

_DATA_MAX_SQL = """
SELECT MAX(o.ordered_at) FROM orders o
JOIN stores s ON s.id = o.store_id
LEFT JOIN customers c ON c.id = o.customer_id
LEFT JOIN brands b ON b.id = s.brand_id
WHERE """ + _ORDER_SCOPE

REVENUE_BY_MONTH_SQL = """
SELECT to_char(m, 'YYYY-MM') AS month,
       COALESCE(SUM(o.total_cents), 0)::bigint AS total_cents
FROM generate_series($3::date - interval '5 months',
                     $3::date, interval '1 month') AS m
LEFT JOIN (SELECT o.ordered_at, o.total_cents FROM orders o
           JOIN stores s ON s.id = o.store_id
           LEFT JOIN customers c ON c.id = o.customer_id
           LEFT JOIN brands b ON b.id = s.brand_id
           WHERE """ + _ORDER_SCOPE + """) o
  ON date_trunc('month', o.ordered_at """ + BUSINESS_TZ_SQL + """) = m
GROUP BY m ORDER BY m
"""

ORDERS_BY_STORE_SQL = """
SELECT s.code AS store_code,
       COUNT(o.id) FILTER (WHERE $2::uuid IS NULL
                           OR c.group_id = $2::uuid)::bigint AS orders
FROM stores s
JOIN brands b ON b.id = s.brand_id
LEFT JOIN orders o ON o.store_id = s.id
LEFT JOIN customers c ON c.id = o.customer_id
WHERE ($1::uuid IS NULL OR s.brand_id = $1::uuid)
  AND ($2::uuid IS NULL OR b.group_id = $2::uuid)
GROUP BY s.code ORDER BY s.code
"""

NEW_CUSTOMERS_BY_MONTH_SQL = """
SELECT to_char(m, 'YYYY-MM') AS month,
       COUNT(c.id)::bigint AS new_customers
FROM generate_series($3::date - interval '5 months',
                     $3::date, interval '1 month') AS m
LEFT JOIN customers c ON date_trunc('month', c.created_at """ + BUSINESS_TZ_SQL + """) = m
  AND c.deleted_at IS NULL
  AND """ + _CUST_SCOPE + """
GROUP BY m ORDER BY m
"""


def _business_today() -> _dt.date:
    """Today in the business timezone, independent of host and session TZ.

    Neither clock is reliable for this: the pgembed session zone is GMT
    and the host clock is whatever the box is set to (here +08). Mirrors
    data's `business_today()` in pipelines/snapshot.py.
    """
    import datetime as _dt

    return _dt.datetime.now(
        _dt.timezone(_dt.timedelta(hours=BUSINESS_TZ_OFFSET_HOURS))).date()


async def _resolve_anchor(conn, mode: str, brand_id=None,
                          group_id=None):
    """LD5 as-of anchor for the trend grids + the data_through marker.

    latest -> the freshest order month in scope (falls back to now() when
    the scope holds no orders); current -> now(), which may leave trailing
    zero months the response names via data_through. Returns (anchor_ts,
    data_through_iso)."""
    import datetime as _dt

    data_max = await conn.fetchval(_DATA_MAX_SQL, brand_id, group_id)
    through = (data_max.date().isoformat() if data_max is not None
               else None)
    # Date (not midnight datetime): a naive 00:00 VC timestamptz cast can
    # fall on the previous day under a behind-UTC session TZ and drag the
    # whole grid one month back. Date arithmetic below is TZ-proof.
    if mode == "latest" and data_max is not None:
        anchor = _dt.date(data_max.year, data_max.month, 1)
    else:
        now = _business_today()
        anchor = _dt.date(now.year, now.month, 1)
    return anchor, through


async def _bucket_bounds(granularity: str, date_from, date_to,
                         today=None):
    """Inclusive-start grid bounds (python dates) for the requested grain.

    Defaults preserve the legacy shape: month = last 6 calendar months
    incl. current; week = last 12 Mon-Sun weeks incl. current; year =
    last 5 calendar years incl. current. Explicit date_from/date_to
    clip/extend the grid (capped: 52 weeks, 24 months, 10 years).
    `today` overrides the "current" end (LD5 latest pins it to the data).
    """
    import datetime as _dt

    today = today or _business_today()
    if granularity == "week":
        cur_mon = today - _dt.timedelta(days=today.weekday())
        d_from = date_from or (cur_mon - _dt.timedelta(weeks=11))
        d_to = date_to or today
        start = d_from - _dt.timedelta(days=d_from.weekday())
        end = d_to
        n = (end - start).days // 7 + 1
        n = max(1, min(n, 52))
        end = start + _dt.timedelta(weeks=n) - _dt.timedelta(days=1)
        return start, end
    if granularity == "year":
        y_to = date_to.year if date_to else today.year
        y_from = date_from.year if date_from else y_to - 4
        y_from = max(y_from, y_to - 9)
        return _dt.date(y_from, 1, 1), _dt.date(y_to, 12, 31)
    m_to = (date_to.year, date_to.month) if date_to else (today.year, today.month)
    m_from = ((date_from.year, date_from.month) if date_from
              else ((m_to[0] - (1 if m_to[1] <= 6 else 0)), ((m_to[1] - 7) % 12) + 1))
    span = (m_to[0] * 12 + m_to[1]) - (m_from[0] * 12 + m_from[1]) + 1
    span = max(1, min(span, 24))
    y, m = m_to[0], m_to[1]
    for _ in range(span - 1):
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    return _dt.date(y, m, 1), _dt.date(m_to[0], m_to[1], 1)


def _grid(granularity: str, start, end) -> list:
    """Bucket-start dates oldest-first for the grain."""
    import calendar as _cal
    import datetime as _dt

    out = []
    if granularity == "week":
        cur = start
        while cur <= end:
            out.append(cur)
            cur += _dt.timedelta(weeks=1)
    elif granularity == "year":
        for y in range(start.year, end.year + 1):
            out.append(_dt.date(y, 1, 1))
    else:
        y, m = start.year, start.month
        while (y, m) <= (end.year, end.month):
            out.append(_dt.date(y, m, 1))
            last = _cal.monthrange(y, m)[1]
            nxt = _dt.date(y, m, last) + _dt.timedelta(days=1)
            y, m = nxt.year, nxt.month
    return out


def _label(granularity: str, day) -> str:
    if granularity == "week":
        return day.strftime("%Y-%m-%d")
    if granularity == "year":
        return day.strftime("%Y")
    return day.strftime("%Y-%m")


_SERIES_SQL = {
    "revenue": """
SELECT date_trunc($1, o.ordered_at """ + BUSINESS_TZ_SQL + """)::date AS bucket,
       COALESCE(SUM(o.total_cents), 0)::bigint AS total_cents
FROM orders o
JOIN stores s ON s.id = o.store_id
LEFT JOIN customers c ON c.id = o.customer_id
LEFT JOIN brands b ON b.id = s.brand_id
WHERE o.ordered_at >= $2::timestamptz AND o.ordered_at < $3::timestamptz
  AND """ + _ORDER_SCOPE.replace("$1", "$4").replace("$2", "$5") + """
GROUP BY 1
""",
    "new_customers": """
SELECT date_trunc($1, c.created_at """ + BUSINESS_TZ_SQL + """)::date AS bucket,
       COUNT(c.id)::bigint AS new_customers
FROM customers c
WHERE c.created_at >= $2::timestamptz AND c.created_at < $3::timestamptz
  AND c.deleted_at IS NULL
  AND """ + _CUST_SCOPE.replace("$1", "$4").replace("$2", "$5") + """
GROUP BY 1
""",
}


async def _series(conn, kind: str, granularity: str, start, end,
                brand_id=None, group_id=None) -> list[dict]:
    """Zero-filled {period, value} series for one kind + grain."""
    import datetime as _dt

    grain = {"week": "week", "month": "month", "year": "year"}[granularity]
    grid = _grid(granularity, start, end)
    lo = _dt.datetime.combine(grid[0], _dt.time.min)
    hi_key = grid[-1]
    if granularity == "week":
        hi = _dt.datetime.combine(hi_key + _dt.timedelta(days=7), _dt.time.min)
    elif granularity == "year":
        hi = _dt.datetime(hi_key.year + 1, 1, 1)
    else:
        hi = (_dt.datetime(hi_key.year + (1 if hi_key.month == 12 else 0),
                           1 if hi_key.month == 12 else hi_key.month + 1, 1))
    rows = await conn.fetch(_SERIES_SQL[kind], grain, lo, hi,
                            brand_id, group_id)
    by_bucket = {r["bucket"]: r for r in rows}
    out = []
    for day in grid:
        # date_trunc week/month/year of any ts in the bucket == bucket start.
        r = by_bucket.get(day)
        if kind == "revenue":
            out.append({"period": _label(granularity, day),
                        "total_cents": int(r["total_cents"]) if r else 0})
        else:
            out.append({"period": _label(granularity, day),
                        "new_customers": int(r["new_customers"]) if r else 0})
    return out


async def get_overview(pool: asyncpg.Pool, *, granularity: str = "month",
                       date_from=None, date_to=None,
                       brand_id=None, group_id=None,
                       mode: str = "latest") -> dict:
    async with pool.acquire() as conn:
        anchor, through = await _resolve_anchor(conn, mode, brand_id,
                                                group_id)
        totals = await conn.fetchrow(TOTALS_SQL, brand_id, group_id)
        revenue = await conn.fetch(REVENUE_BY_MONTH_SQL, brand_id, group_id,
                                   anchor)
        by_store = await conn.fetch(ORDERS_BY_STORE_SQL, brand_id, group_id)
        new_cust = await conn.fetch(NEW_CUSTOMERS_BY_MONTH_SQL,
                                    brand_id, group_id, anchor)
        extra: dict = {}
        if granularity != "month" or date_from is not None or date_to is not None:
            start, end = await _bucket_bounds(granularity, date_from, date_to,
                                              today=anchor)
            extra = {
                "granularity": granularity,
                "date_from": start.isoformat(),
                "date_to": end.isoformat(),
                "revenue_series": await _series(conn, "revenue",
                                                 granularity, start, end,
                                                 brand_id, group_id),
                "new_customers_series": await _series(conn, "new_customers",
                                                        granularity, start, end,
                                                        brand_id, group_id),
            }
    body = {
        # LD5 as-of envelope: which date the trend grids are pinned to,
        # plus the freshest data date naming any trailing gap honestly.
        "mode": mode,
        "data_through": through,
        "totals": {
            "customers": int(totals["customers"]),
            "orders": int(totals["orders"]),
            "revenue_cents": int(totals["revenue_cents"]),
            "active_campaigns": int(totals["active_campaigns"]),
        },
        "revenue_by_month": [
            {"month": r["month"], "total_cents": int(r["total_cents"])} for r in revenue
        ],
        "orders_by_store": [
            {"store_code": r["store_code"], "orders": int(r["orders"])} for r in by_store
        ],
        "new_customers_by_month": [
            {"month": r["month"], "new_customers": int(r["new_customers"])}
            for r in new_cust
        ],
        **extra,
    }
    return body
