-- Revenue at risk (A1 / F4): the money a customer costs if they churn.
--
-- ONE file, ONE question: for each customer, the newest churn snapshot and
-- the newest CLV snapshot in the page's own scope, joined on the customer,
-- with revenue_at_risk_cents = (1 - p_alive) * clv_cents. The churn screen
-- ranks by probability; this is the value dimension it never joined, so a
-- manager can answer "which at-risk customers are worth spending on".
--
-- Scope, snapshot pick and one-row-per-customer are the same rules as
-- customers.sql page_clv_scores / page_churn_scores: the session's
-- (brand_id, group_id) is compared with each snapshot's own stamp by
-- equality (IS NOT DISTINCT FROM -- a bare = drops the GLOBAL scope),
-- the snapshot is picked ONCE (the newest as_of at or before today's +08
-- business date), and each customer is looked up through the
-- (snapshot_id, customer_id) primary key. A customer a snapshot does not
-- carry reads NULL -- never $0, never a tier -- and the vintage still
-- travels: as_of IS NULL means this scope has no snapshot at all,
-- as_of IS NOT NULL means this customer is not in that snapshot.
--
-- The enumeration statement (revenue_at_risk_top) reads the snapshots'
-- own populations: the scores a fit wrote for its scope. Soft-deleted
-- customers are excluded at read time (the fit may predate the delete).

-- name: revenue_at_risk_page
-- params: customer_ids uuid[], brand_id uuid|None, group_id uuid|None, activity text, churn_horizon_days int, clv_horizon_days int
SELECT c.customer_id,
       churn_scores.band AS churn_band,
       churn_scores.p_alive AS churn_p_alive,
       churn_scores.recency_days AS churn_recency_days,
       clv_scores.clv_cents, clv_scores.value_tier AS clv_tier,
       CASE WHEN churn_scores.p_alive IS NULL
                 OR clv_scores.clv_cents IS NULL THEN NULL
            ELSE ROUND((1 - churn_scores.p_alive)
                         * clv_scores.clv_cents)::bigint
       END AS revenue_at_risk_cents,
       chu.as_of AS churn_as_of, chu.id AS churn_snapshot_id,
       chu.activity AS churn_activity,
       chu.horizon_days AS churn_horizon_days,
       clv.as_of AS clv_as_of, clv.id AS clv_snapshot_id,
       clv.horizon_days AS clv_horizon_days
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
) churn_scores ON TRUE
LEFT JOIN (
  SELECT s.id, s.as_of, s.horizon_days
  FROM clv_snapshots s
  WHERE s.horizon_days = $6::int
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
) clv_scores ON TRUE;

-- name: revenue_at_risk_top
-- params: brand_id uuid|None, group_id uuid|None, activity text, churn_horizon_days int, clv_horizon_days int, limit_n int, band text|None
-- The triage list: customers carrying BOTH scores in the scope's newest
-- snapshots, richest risk first. band = 'at-risk' is the churn-triage
-- cut; NULL band reads every band and lets the money rank. Soft-deleted
-- customers never enumerate, even when the snapshot still carries them.
SELECT cs.customer_id,
       cs.band AS churn_band, cs.p_alive AS churn_p_alive,
       cs.recency_days AS churn_recency_days,
       vs.clv_cents, vs.value_tier AS clv_tier,
       ROUND((1 - cs.p_alive) * vs.clv_cents)::bigint
         AS revenue_at_risk_cents,
       chu.as_of AS churn_as_of, chu.id AS churn_snapshot_id,
       clv.as_of AS clv_as_of, clv.id AS clv_snapshot_id
FROM (
  SELECT s.id, s.as_of
  FROM churn_snapshots s
  WHERE s.activity = $3::text
    AND s.horizon_days = $4::int
    AND s.brand_id IS NOT DISTINCT FROM $1::uuid
    AND s.group_id IS NOT DISTINCT FROM $2::uuid
    AND s.as_of <= (now() AT TIME ZONE interval '8 hours')::date
  ORDER BY s.as_of DESC, s.created_at DESC, s.id DESC
  LIMIT 1
) chu
JOIN (
  SELECT s.id, s.as_of
  FROM clv_snapshots s
  WHERE s.horizon_days = $5::int
    AND s.brand_id IS NOT DISTINCT FROM $1::uuid
    AND s.group_id IS NOT DISTINCT FROM $2::uuid
    AND s.as_of <= (now() AT TIME ZONE interval '8 hours')::date
  ORDER BY s.as_of DESC, s.created_at DESC, s.id DESC
  LIMIT 1
) clv ON TRUE
JOIN churn_customer_scores cs ON cs.snapshot_id = chu.id
JOIN clv_customer_scores vs
  ON vs.snapshot_id = clv.id AND vs.customer_id = cs.customer_id
JOIN customers cu ON cu.id = cs.customer_id
WHERE cu.deleted_at IS NULL
  AND ($7::text IS NULL OR cs.band = $7::text)
ORDER BY ROUND((1 - cs.p_alive) * vs.clv_cents)::bigint DESC,
         cs.customer_id ASC
LIMIT $6::int;
