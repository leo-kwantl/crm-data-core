-- Single-customer-view serving forms (raw SQL, asyncpg $n placeholders).
-- The refresh itself is the 061 function; these are the shared reads plus
-- its callable handle. Scope is always the pair (brand_id, group_id) by
-- equality — a scope with no refreshed rows reads nothing.

-- name: scv_refresh
-- params: brand_id uuid, group_id uuid
-- Runs refresh_customer_single_view for the scope; returns the row count.
SELECT refresh_customer_single_view($1::uuid, $2::uuid) AS scv_rows;

-- name: scv_by_id
-- params: brand_id uuid, group_id uuid, customer_id uuid
SELECT brand_id, group_id, customer_id, member_id, full_name, tier_code,
  gender, date_of_birth, district, acquisition_channel,
  contacts_summary, consents_summary, custom_data,
  order_count, total_spent_cents, avg_order_value_cents,
  avg_items_per_order, last_order_at,
  r, f, m, rfm_segment, recency_days, frequency, monetary_cents,
  rfm_snapshot_id, rfm_as_of,
  clv_p_alive, clv_expected_purchases, clv_avg_order_value_cents,
  clv_cents, value_tier, clv_snapshot_id, clv_as_of,
  churn_p_alive, churn_expected_purchases, churn_band,
  churn_snapshot_id, churn_as_of,
  segment_id, segment_label, segment_mode, segment_run_id, segment_as_of,
  refreshed_at
FROM customer_single_view
WHERE brand_id = $1::uuid AND group_id = $2::uuid AND customer_id = $3::uuid;

-- name: scv_page
-- params: brand_id uuid, group_id uuid, rfm_segment text|None, value_tier text|None, churn_band text|None, min_clv_cents float|None, limit_n int
SELECT customer_id, member_id, full_name, tier_code,
  order_count, total_spent_cents, avg_order_value_cents, last_order_at,
  r, f, m, rfm_segment, recency_days, monetary_cents,
  clv_cents, value_tier, churn_p_alive, churn_band,
  segment_id, segment_label, refreshed_at
FROM customer_single_view
WHERE brand_id = $1::uuid AND group_id = $2::uuid
  AND ($3::text IS NULL OR rfm_segment = $3)
  AND ($4::text IS NULL OR value_tier = $4)
  AND ($5::text IS NULL OR churn_band = $5)
  AND ($6::float IS NULL OR clv_cents IS NOT NULL AND clv_cents >= $6)
ORDER BY total_spent_cents DESC, customer_id ASC
LIMIT $7::int;

-- name: scv_count
-- params: brand_id uuid, group_id uuid, rfm_segment text|None, value_tier text|None, churn_band text|None, min_clv_cents float|None
-- The scoped twin of scv_page: the total for the same filters.
SELECT COUNT(*) FROM customer_single_view
WHERE brand_id = $1::uuid AND group_id = $2::uuid
  AND ($3::text IS NULL OR rfm_segment = $3)
  AND ($4::text IS NULL OR value_tier = $4)
  AND ($5::text IS NULL OR churn_band = $5)
  AND ($6::float IS NULL OR clv_cents IS NOT NULL AND clv_cents >= $6);
