"""Dashboard stats: pure-SQL aggregates for charts (read-only, no PII).

One round-trip per section (4 queries): totals, revenue_by_month,
orders_by_store, new_customers_by_month. Month buckets cover the last 6
calendar months INCLUDING the current month; empty months yield zero rows
(zero-filled in Python so the shape is stable for charts).

Month buckets are BUSINESS months (Asia/Hong_Kong, +08), not the session
zone — an order at 2025-09-30T18:00Z is 2025-10-01 on the business
calendar. See `BUSINESS_TZ_SQL` in pipelines/snapshot.py for why the
offset form is used instead of the zone name.
"""

from __future__ import annotations

import asyncpg

from pipelines.snapshot import BUSINESS_TZ_SQL

TOTALS_SQL = """
SELECT (SELECT COUNT(*) FROM customers WHERE deleted_at IS NULL) AS customers,
       (SELECT COUNT(*) FROM orders) AS orders,
       (SELECT COALESCE(SUM(total_cents), 0) FROM orders) AS revenue_cents,
       (SELECT COUNT(*) FROM campaigns WHERE status = 'active') AS active_campaigns
"""

REVENUE_BY_MONTH_SQL = f"""
SELECT to_char(m, 'YYYY-MM') AS month,
       COALESCE(SUM(o.total_cents), 0)::bigint AS total_cents
FROM generate_series(
       date_trunc('month', now() {BUSINESS_TZ_SQL}) - interval '5 months',
       date_trunc('month', now() {BUSINESS_TZ_SQL}), interval '1 month') AS m
LEFT JOIN orders o ON date_trunc('month', o.ordered_at {BUSINESS_TZ_SQL}) = m
GROUP BY m ORDER BY m
"""

ORDERS_BY_STORE_SQL = """
SELECT s.code AS store_code, COUNT(o.id)::bigint AS orders
FROM stores s LEFT JOIN orders o ON o.store_id = s.id
GROUP BY s.code ORDER BY s.code
"""

NEW_CUSTOMERS_BY_MONTH_SQL = f"""
SELECT to_char(m, 'YYYY-MM') AS month,
       COUNT(c.id)::bigint AS new_customers
FROM generate_series(
       date_trunc('month', now() {BUSINESS_TZ_SQL}) - interval '5 months',
       date_trunc('month', now() {BUSINESS_TZ_SQL}), interval '1 month') AS m
LEFT JOIN customers c ON date_trunc('month', c.created_at {BUSINESS_TZ_SQL}) = m
  AND c.deleted_at IS NULL
GROUP BY m ORDER BY m
"""


async def get_overview(pool: asyncpg.Pool) -> dict:
    async with pool.acquire() as conn:
        totals = await conn.fetchrow(TOTALS_SQL)
        revenue = await conn.fetch(REVENUE_BY_MONTH_SQL)
        by_store = await conn.fetch(ORDERS_BY_STORE_SQL)
        new_cust = await conn.fetch(NEW_CUSTOMERS_BY_MONTH_SQL)
    return {
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
    }
