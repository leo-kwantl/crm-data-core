-- Named customer queries (raw SQL, asyncpg $n placeholders).
-- PII: phone/email match exact normalized values only; never LIKE on PII.

-- name: customer_by_id
-- params: customer_id uuid
SELECT id, member_id, external_member_id, full_name, attributes,
  created_at, updated_at, group_id, linked_customer_id,
  gender, date_of_birth, district, address_line,
  date_part('year', age(date_of_birth))::int AS age_years
FROM customers
WHERE id = $1 AND deleted_at IS NULL;

-- name: customer_contacts
-- params: customer_id uuid
SELECT id, customer_id, kind, value_normalized, value_display, is_primary, created_at
FROM customer_contacts
WHERE customer_id = $1
ORDER BY created_at;

-- name: customer_consents
-- params: customer_id uuid
SELECT channel, status
FROM consents
WHERE customer_id = $1;

-- name: customer_purchase_summary
-- params: customer_id uuid
SELECT COUNT(*) AS order_count,
       COALESCE(SUM(total_cents), 0) AS total_spent_cents,
       MAX(ordered_at) AS last_order_at
FROM orders
WHERE customer_id = $1;

-- name: customer_insert
-- params: member_id text, external_member_id text|None, full_name text, attributes jsonb,
--   group_id uuid|None, gender text|None, date_of_birth date|None, district text|None, address_line text|None
INSERT INTO customers (member_id, external_member_id, full_name, attributes,
  group_id, gender, date_of_birth, district, address_line)
VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7, $8, $9)
RETURNING id, member_id, external_member_id, full_name, attributes,
  created_at, updated_at;

-- name: customer_contact_insert
-- params: customer_id uuid, kind text, value_normalized text, value_display text, is_primary bool
INSERT INTO customer_contacts (customer_id, kind, value_normalized, value_display, is_primary)
VALUES ($1, $2, $3, $4, $5);

-- name: customer_update_name
-- params: customer_id uuid, full_name text
UPDATE customers SET full_name = $2, updated_at = now()
WHERE id = $1 AND deleted_at IS NULL;

-- name: customer_merge_attributes
-- params: customer_id uuid, attributes jsonb
UPDATE customers
SET attributes = jsonb_strip_nulls(COALESCE(attributes, '{}'::jsonb) || $2::jsonb),
    updated_at = now()
WHERE id = $1 AND deleted_at IS NULL;

-- name: customer_replace_contacts_delete
-- params: customer_id uuid
DELETE FROM customer_contacts WHERE customer_id = $1;

-- name: customer_soft_delete
-- params: customer_id uuid
UPDATE customers SET deleted_at = now(), updated_at = now()
WHERE id = $1 AND deleted_at IS NULL;

-- name: list_customers
-- params: cursor_at timestamptz|None, cursor_id uuid|None, limit int, tier_code text|None, sms_consent text|None, group_id uuid|None, brand_id uuid|None
-- Masked list, newest-first on (created_at DESC, id DESC).
-- Keyset page: fetch limit+1 upstream. Contacts are masked at
-- serialization (see repos/customers.list_customers_page); never LIKE on PII.
-- SCOPE: brand_id is the session's own brand (deps.session_brand); a customer
-- with no completed order is visible to every brand.
SELECT id, member_id, external_member_id, full_name, tier_code, created_at,
  gender, date_part('year', age(date_of_birth))::int AS age_years
FROM customers
WHERE deleted_at IS NULL
  AND ($4::text IS NULL OR tier_code = $4)
  AND ($5::text IS NULL OR EXISTS
    (SELECT 1 FROM consents s WHERE s.customer_id = customers.id
     AND s.channel = 'sms' AND s.status = $5))
  AND ($1::timestamptz IS NULL OR (created_at, id) < ($1::timestamptz, $2::uuid))
  AND ($6::uuid IS NULL OR group_id = $6::uuid OR group_id IS NULL)
  AND ($7::uuid IS NULL OR EXISTS
    (SELECT 1 FROM orders o JOIN stores s ON s.id = o.store_id
     WHERE o.customer_id = customers.id AND o.status = 'completed'
     AND s.brand_id = $7::uuid)
    OR NOT EXISTS (SELECT 1 FROM orders o WHERE o.customer_id = customers.id
     AND o.status = 'completed'))
ORDER BY created_at DESC, id DESC
LIMIT $3;

-- name: count_customers
-- params: tier_code text|None, sms_consent text|None, group_id uuid|None, brand_id uuid|None
-- The scoped twin of list_customers: the total for the same filters.
SELECT COUNT(*) FROM customers
WHERE deleted_at IS NULL
  AND ($1::text IS NULL OR tier_code = $1)
  AND ($2::text IS NULL OR EXISTS
    (SELECT 1 FROM consents s WHERE s.customer_id = customers.id
     AND s.channel = 'sms' AND s.status = $2))
  AND ($3::uuid IS NULL OR group_id = $3::uuid OR group_id IS NULL)
  AND ($4::uuid IS NULL OR EXISTS
    (SELECT 1 FROM orders o JOIN stores s ON s.id = o.store_id
     WHERE o.customer_id = customers.id AND o.status = 'completed'
     AND s.brand_id = $4::uuid)
    OR NOT EXISTS (SELECT 1 FROM orders o WHERE o.customer_id = customers.id
     AND o.status = 'completed'));

-- name: page_contacts
-- params: customer_ids uuid[]
SELECT id, customer_id, kind, value_normalized, value_display, is_primary, created_at
FROM customer_contacts
WHERE customer_id = ANY($1::uuid[])
ORDER BY created_at;

-- name: page_order_counts
-- params: customer_ids uuid[]
SELECT customer_id, COUNT(*) AS order_count
FROM orders
WHERE customer_id = ANY($1::uuid[])
GROUP BY customer_id;

-- name: page_consents
-- params: customer_ids uuid[]
SELECT customer_id, channel, status
FROM consents
WHERE customer_id = ANY($1::uuid[]);

-- name: page_clv_scores
-- params: customer_ids uuid[], brand_id uuid|None, group_id uuid|None, horizon_days int
-- The value of every customer on the page, from the NEWEST CLV snapshot in
-- the page's own scope, with the vintage that produced it.
--
-- Scope: the session's (brand_id, group_id) is compared with the snapshot's
-- own stamp by equality, the rule the other snapshot reads already use
-- (db/queries/kpi_daily.sql `clv_snapshots_list`): a brand session reads its
-- brand's snapshot and no other's, a session with no group scope does not
-- read a group's snapshot, and a group does not read the global one. A scope
-- with no snapshot reads nothing, and says so with a NULL vintage.
--
-- The snapshot is picked ONCE (the newest as_of at or before today's +08
-- business date - a snapshot dated in the future is not a fact this system
-- has yet) and each customer is then looked up through the
-- (snapshot_id, customer_id) primary key, so the 19-20 snapshots per
-- customer never multiply the page. The lateral LIMIT 1 is a PLAN guard, not
-- a semantic limit: without it the planner hashes the whole snapshot
-- (measured 498 rows at brand scope, 2026-10-20); with it the plan is one
-- index probe per customer (measured 0.4 ms for 50 customers).
--
-- A customer the snapshot does not carry reads NULL - never $0, never a tier.
-- The vintage still travels: as_of IS NULL means this scope has no snapshot
-- at all, as_of IS NOT NULL means this customer is not in that snapshot.
SELECT c.customer_id,
       cs.clv_cents, cs.value_tier,
       clv.as_of AS clv_as_of, clv.id AS clv_snapshot_id,
       clv.horizon_days AS clv_horizon_days
FROM unnest($1::uuid[]) AS c(customer_id)
LEFT JOIN (
  SELECT s.id, s.as_of, s.horizon_days
  FROM clv_snapshots s
  WHERE s.horizon_days = $4::int
    AND s.brand_id IS NOT DISTINCT FROM $2::uuid
    AND s.group_id IS NOT DISTINCT FROM $3::uuid
    AND s.as_of <= (now() AT TIME ZONE interval '8 hours')::date
  ORDER BY s.as_of DESC, s.created_at DESC, s.id DESC
  LIMIT 1
) clv ON TRUE
LEFT JOIN LATERAL (
  SELECT s.clv_cents, s.value_tier
  FROM clv_customer_scores s
  WHERE s.snapshot_id = clv.id AND s.customer_id = c.customer_id
  LIMIT 1
) cs ON TRUE;

-- name: page_churn_scores
-- params: customer_ids uuid[], brand_id uuid|None, group_id uuid|None, activity text, horizon_days int
-- The risk of every customer on the page: the churn band of the NEWEST
-- snapshot in the page's own scope, with the vintage and the (activity,
-- horizon) pair it came from - a band without its pair is a number with no
-- meaning, and a band without its date is a prediction with no clock.
--
-- Scope, snapshot pick and one-row-per-customer are the same rules as
-- page_clv_scores; the band is a MODEL's prediction and the read states it
-- as one, with its date and the pair it was measured on.
SELECT c.customer_id,
       cs.band, cs.p_alive, cs.recency_days,
       chu.as_of AS churn_as_of, chu.id AS churn_snapshot_id,
       chu.activity AS churn_activity, chu.horizon_days AS churn_horizon_days
FROM unnest($1::uuid[]) AS c(customer_id)
LEFT JOIN (
  SELECT s.id, s.as_of, s.activity, s.horizon_days
  FROM churn_snapshots s
  WHERE s.activity = $4::text
    AND s.horizon_days = $5::int
    AND s.brand_id IS NOT DISTINCT FROM $2::uuid
    AND s.group_id IS NOT DISTINCT FROM $3::uuid
    AND s.as_of <= (now() AT TIME ZONE interval '8 hours')::date
  ORDER BY s.as_of DESC, s.created_at DESC, s.id DESC
  LIMIT 1
) chu ON TRUE
LEFT JOIN LATERAL (
  SELECT s.band, s.p_alive, s.recency_days
  FROM churn_customer_scores s
  WHERE s.snapshot_id = chu.id AND s.customer_id = c.customer_id
  LIMIT 1
) cs ON TRUE;
