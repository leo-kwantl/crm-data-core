-- migrate:up
-- SCV snapshot vintage, brand-scope fix (064): the four latest_* CTEs
-- below used `s.group_id IS NOT DISTINCT FROM v_group_id`, which matches
-- nothing: no job stamps a group scope (scope-shape rule), so every
-- snapshot shell carries group_id NULL, and NULL is distinct from the
-- brand's pair. The refresh therefore picked no vintage and stamped
-- NULL model columns on all 583 flagship rows (measured 2026-10-31).
-- The fix mirrors latest_rec's long-standing predicate: a brand-wide
-- shell (group_id NULL) serves every pair of that brand; a group-stamped
-- shell only its pair. Global shells (brand NULL) still never feed a
-- brand scope: only the group arm changed, the brand arm is untouched.

ALTER TABLE customer_single_view
  ADD COLUMN IF NOT EXISTS recommendations jsonb NOT NULL DEFAULT '[]'::jsonb,
  ADD COLUMN IF NOT EXISTS rec_batch_id uuid NULL REFERENCES rec_batches (id) ON DELETE SET NULL,
  ADD COLUMN IF NOT EXISTS rec_as_of timestamptz NULL;

CREATE OR REPLACE FUNCTION refresh_customer_single_view(p_brand_id uuid, p_group_id uuid)
RETURNS integer LANGUAGE plpgsql AS $$
DECLARE
  v_group_id uuid;
BEGIN
  IF p_brand_id IS NULL THEN
    RAISE EXCEPTION 'refresh_customer_single_view: brand scope required (group-alone and global scopes are never stamped)';
  END IF;
  SELECT b.group_id INTO v_group_id FROM brands b WHERE b.id = p_brand_id;
  IF v_group_id IS NULL THEN
    RAISE EXCEPTION 'refresh_customer_single_view: unknown brand %', p_brand_id;
  END IF;
  IF p_group_id IS NOT NULL AND p_group_id IS DISTINCT FROM v_group_id THEN
    RAISE EXCEPTION 'refresh_customer_single_view: group % is not the brand''s pair', p_group_id;
  END IF;

  WITH biz_today AS (
    SELECT (now() AT TIME ZONE interval '8 hours')::date AS d
  ),
  latest_rfm AS (
    SELECT s.id, s.window_to FROM rfm_snapshots s
    WHERE s.brand_id IS NOT DISTINCT FROM p_brand_id
      AND (s.group_id IS NULL OR s.group_id IS NOT DISTINCT FROM v_group_id)
    ORDER BY s.window_to DESC, s.scored_at DESC, s.id DESC LIMIT 1
  ),
  latest_clv AS (
    SELECT s.id, s.as_of FROM clv_snapshots s, biz_today b
    WHERE s.horizon_days = 90
      AND s.brand_id IS NOT DISTINCT FROM p_brand_id
      AND (s.group_id IS NULL OR s.group_id IS NOT DISTINCT FROM v_group_id)
      AND s.as_of <= b.d
    ORDER BY s.as_of DESC, s.created_at DESC, s.id DESC LIMIT 1
  ),
  latest_churn AS (
    SELECT s.id, s.as_of FROM churn_snapshots s, biz_today b
    WHERE s.activity = 'orders' AND s.horizon_days = 90
      AND s.brand_id IS NOT DISTINCT FROM p_brand_id
      AND (s.group_id IS NULL OR s.group_id IS NOT DISTINCT FROM v_group_id)
      AND s.as_of <= b.d
    ORDER BY s.as_of DESC, s.created_at DESC, s.id DESC LIMIT 1
  ),
  latest_seg AS (
    SELECT s.id, s.as_of FROM segment_runs s, biz_today b
    WHERE s.mode = 'overall'
      AND s.brand_id IS NOT DISTINCT FROM p_brand_id
      AND (s.group_id IS NULL OR s.group_id IS NOT DISTINCT FROM v_group_id)
      AND s.as_of <= b.d
    ORDER BY s.as_of DESC, s.created_at DESC, s.id DESC LIMIT 1
  ),
  latest_rec AS (
    SELECT s.id, s.created_at FROM rec_batches s
    WHERE s.brand_id = p_brand_id
      AND (s.group_id IS NULL OR s.group_id IS NOT DISTINCT FROM v_group_id)
    ORDER BY s.created_at DESC, s.id DESC LIMIT 1
  ),
  base AS (
    SELECT DISTINCT c.id AS customer_id
    FROM customers c
    JOIN orders o ON o.customer_id = c.id AND o.status = 'completed'
    JOIN stores st ON st.id = o.store_id AND st.brand_id = p_brand_id
    WHERE c.deleted_at IS NULL
      AND (c.group_id IS NULL OR c.group_id IS NOT DISTINCT FROM v_group_id)
  ),
  ord AS (
    SELECT o.customer_id,
           COUNT(*)::int AS order_count,
           COALESCE(SUM(o.total_cents), 0)::bigint AS total_spent_cents,
           MAX(o.ordered_at) AS last_order_at,
           COALESCE(SUM(oi.qty), 0)::bigint AS total_items
    FROM orders o
    JOIN stores st ON st.id = o.store_id AND st.brand_id = p_brand_id
    LEFT JOIN order_items oi ON oi.order_id = o.id
    WHERE o.status = 'completed'
    GROUP BY o.customer_id
  ),
  con AS (
    SELECT cc.customer_id,
           COALESCE(jsonb_agg(jsonb_build_object(
             'kind', cc.kind, 'value', cc.value_display,
             'is_primary', cc.is_primary)
             ORDER BY cc.created_at), '[]'::jsonb) AS contacts_summary
    FROM customer_contacts cc GROUP BY cc.customer_id
  ),
  cns AS (
    SELECT n.customer_id,
           COALESCE(jsonb_object_agg(n.channel, n.status), '{}'::jsonb) AS consents_summary
    FROM consents n GROUP BY n.customer_id
  )
  INSERT INTO customer_single_view (
      brand_id, group_id, customer_id, member_id, full_name, tier_code,
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
      recommendations, rec_batch_id, rec_as_of,
      refreshed_at
    )
    SELECT p_brand_id, v_group_id, c.id, c.member_id, c.full_name, c.tier_code,
      c.gender, c.date_of_birth, c.district,
      COALESCE(c.acquisition_channel, 'unknown'),
      COALESCE(con.contacts_summary, '[]'::jsonb),
      COALESCE(cns.consents_summary, '{}'::jsonb),
      COALESCE(cd.data, '{}'::jsonb),
      COALESCE(ord.order_count, 0),
      COALESCE(ord.total_spent_cents, 0),
      CASE WHEN COALESCE(ord.order_count, 0) > 0
        THEN ord.total_spent_cents::numeric / ord.order_count ELSE 0 END,
      CASE WHEN COALESCE(ord.order_count, 0) > 0
        THEN ord.total_items::numeric / ord.order_count ELSE 0 END,
      ord.last_order_at,
      rs.r, rs.f, rs.m, rs.segment, rs.recency_days, rs.frequency, rs.monetary_cents,
      (SELECT id FROM latest_rfm), (SELECT window_to FROM latest_rfm),
      vs.p_alive, vs.expected_purchases, vs.avg_order_value_cents,
      vs.clv_cents, vs.value_tier,
      (SELECT id FROM latest_clv), (SELECT as_of FROM latest_clv),
      hs.p_alive, hs.expected_purchases, hs.band,
      (SELECT id FROM latest_churn), (SELECT as_of FROM latest_churn),
      sa.segment, sa.label, (SELECT CASE WHEN id IS NULL THEN NULL ELSE 'overall' END FROM latest_seg),
      (SELECT id FROM latest_seg), (SELECT as_of FROM latest_seg),
      COALESCE(rc.recommendations, '[]'::jsonb),
      (SELECT id FROM latest_rec), (SELECT created_at FROM latest_rec),
      now()
    FROM base
    JOIN customers c ON c.id = base.customer_id
    LEFT JOIN ord ON ord.customer_id = base.customer_id
    LEFT JOIN con ON con.customer_id = base.customer_id
    LEFT JOIN cns ON cns.customer_id = base.customer_id
    LEFT JOIN customer_custom_data cd ON cd.customer_id = base.customer_id
    LEFT JOIN LATERAL (
      SELECT s.r, s.f, s.m, s.segment, s.recency_days, s.frequency, s.monetary_cents
      FROM rfm_customer_scores s
      WHERE s.snapshot_id = (SELECT id FROM latest_rfm)
        AND s.customer_id = base.customer_id LIMIT 1
    ) rs ON TRUE
    LEFT JOIN LATERAL (
      SELECT s.p_alive, s.expected_purchases, s.avg_order_value_cents, s.clv_cents, s.value_tier
      FROM clv_customer_scores s
      WHERE s.snapshot_id = (SELECT id FROM latest_clv)
        AND s.customer_id = base.customer_id LIMIT 1
    ) vs ON TRUE
    LEFT JOIN LATERAL (
      SELECT s.p_alive, s.expected_purchases, s.band
      FROM churn_customer_scores s
      WHERE s.snapshot_id = (SELECT id FROM latest_churn)
        AND s.customer_id = base.customer_id LIMIT 1
    ) hs ON TRUE
    LEFT JOIN LATERAL (
      SELECT s.segment, s.label
      FROM segment_assignments s
      WHERE s.run_id = (SELECT id FROM latest_seg)
        AND s.customer_id = base.customer_id LIMIT 1
    ) sa ON TRUE
    LEFT JOIN LATERAL (
      SELECT COALESCE(jsonb_agg(jsonb_build_object(
               'product_id', i.product_id, 'rank', i.rank,
               'score', i.score, 'reason', i.reason)
               ORDER BY i.rank), '[]'::jsonb) AS recommendations
      FROM rec_items i
      WHERE i.batch_id = (SELECT id FROM latest_rec)
        AND i.customer_id = base.customer_id
        AND i.rank <= 5
    ) rc ON TRUE
    ON CONFLICT (brand_id, group_id, customer_id) DO UPDATE SET
      member_id = EXCLUDED.member_id, full_name = EXCLUDED.full_name,
      tier_code = EXCLUDED.tier_code, gender = EXCLUDED.gender,
      date_of_birth = EXCLUDED.date_of_birth, district = EXCLUDED.district,
      acquisition_channel = EXCLUDED.acquisition_channel,
      contacts_summary = EXCLUDED.contacts_summary,
      consents_summary = EXCLUDED.consents_summary,
      custom_data = EXCLUDED.custom_data,
      order_count = EXCLUDED.order_count,
      total_spent_cents = EXCLUDED.total_spent_cents,
      avg_order_value_cents = EXCLUDED.avg_order_value_cents,
      avg_items_per_order = EXCLUDED.avg_items_per_order,
      last_order_at = EXCLUDED.last_order_at,
      r = EXCLUDED.r, f = EXCLUDED.f, m = EXCLUDED.m,
      rfm_segment = EXCLUDED.rfm_segment,
      recency_days = EXCLUDED.recency_days, frequency = EXCLUDED.frequency,
      monetary_cents = EXCLUDED.monetary_cents,
      rfm_snapshot_id = EXCLUDED.rfm_snapshot_id, rfm_as_of = EXCLUDED.rfm_as_of,
      clv_p_alive = EXCLUDED.clv_p_alive,
      clv_expected_purchases = EXCLUDED.clv_expected_purchases,
      clv_avg_order_value_cents = EXCLUDED.clv_avg_order_value_cents,
      clv_cents = EXCLUDED.clv_cents, value_tier = EXCLUDED.value_tier,
      clv_snapshot_id = EXCLUDED.clv_snapshot_id, clv_as_of = EXCLUDED.clv_as_of,
      churn_p_alive = EXCLUDED.churn_p_alive,
      churn_expected_purchases = EXCLUDED.churn_expected_purchases,
      churn_band = EXCLUDED.churn_band,
      churn_snapshot_id = EXCLUDED.churn_snapshot_id,
      churn_as_of = EXCLUDED.churn_as_of,
      segment_id = EXCLUDED.segment_id, segment_label = EXCLUDED.segment_label,
      segment_mode = EXCLUDED.segment_mode,
      segment_run_id = EXCLUDED.segment_run_id,
      segment_as_of = EXCLUDED.segment_as_of,
      recommendations = EXCLUDED.recommendations,
      rec_batch_id = EXCLUDED.rec_batch_id,
      rec_as_of = EXCLUDED.rec_as_of,
      refreshed_at = now();

  DELETE FROM customer_single_view v
  WHERE v.brand_id = p_brand_id AND v.group_id = v_group_id
    AND NOT EXISTS (
      SELECT 1 FROM customers c
      JOIN orders o ON o.customer_id = c.id AND o.status = 'completed'
      JOIN stores st ON st.id = o.store_id AND st.brand_id = p_brand_id
      WHERE c.id = v.customer_id AND c.deleted_at IS NULL
        AND (c.group_id IS NULL OR c.group_id IS NOT DISTINCT FROM v_group_id)
    );

  RETURN (SELECT count(*)::int FROM customer_single_view
    WHERE brand_id = p_brand_id AND group_id = v_group_id);
END $$;

-- migrate:down
-- Best-effort: restores 063's refresh function; re-apply the 063 file
-- (its up block is a full CREATE OR REPLACE) to get it back exactly.
-- Row data is untouched by either direction.
