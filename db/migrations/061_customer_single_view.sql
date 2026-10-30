-- migrate:up
-- 061: customer_single_view (data, [ADDITIVE]) — one row per customer per
-- scope with the attributes serving reads today from five places.
--
-- Grain: one row per (brand_id, group_id, customer_id). PK is brand-first
-- because every snapshot scope index is (brand_id, group_id): the refresh
-- probes snapshots brand-first and the reads filter brand-first.
-- No global row: brand_id is NOT NULL, a group-alone call is refused by
-- the refresh function, and no reader substitutes another scope's row.
--
-- Brand reach: a customer belongs to a scope row only through a completed
-- order in one of the brand's stores (stores.brand_id); a customer with no
-- completed order in the brand has no row here, and soft-deleted customers
-- never enumerate. customers.group_id NULL-or-matching rows are stamped
-- with the scope's group (same convention as customers.sql list_customers).
--
-- Snapshot vintage (same rules as customers.sql page_clv_scores /
-- page_churn_scores and revenue_at_risk.sql): the session scope is compared
-- with each snapshot's own stamp by equality (IS NOT DISTINCT FROM), the
-- snapshot is picked ONCE per refresh (newest as_of at or before today's
-- +08 business date — a future-dated snapshot is not a fact yet), and a
-- customer the snapshot does not carry reads NULL, never $0. RFM is picked
-- by newest window_to (any mode; v1/v2 rows coexist per 031), CLV by
-- horizon_days = 90, churn by activity = 'orders' and horizon_days = 90,
-- segments by mode = 'overall' (the product's single served k surface per
-- 015) — the nightly manifest's own parameters, so the refresh reads what
-- the nightly wrote. A customer missing from a snapshot keeps NULL scores
-- with the vintage still stamped (snapshot ids NULL = scope has no run yet).
--
-- Call order (no scheduler lives in this repo — pipelines/contracts.md:
-- "Cron owns the tick"): run ml snapshot jobs first (rfm, clv with
-- horizon 90, churn with activity orders + horizon 90, segments overall),
-- then SELECT refresh_customer_single_view(brand, group); tag evaluation
-- reads this table, so tags evaluate after the refresh. Re-runs are safe
-- (idempotent upsert; stale rows for the scope are deleted).

CREATE TABLE customer_single_view (
  brand_id uuid NOT NULL REFERENCES brands (id),
  group_id uuid NOT NULL REFERENCES groups (id),
  customer_id uuid NOT NULL REFERENCES customers (id) ON DELETE CASCADE,
  member_id text NOT NULL,
  full_name text NOT NULL,
  tier_code text NULL,
  gender text NULL,
  date_of_birth date NULL,
  district text NULL,
  acquisition_channel text NOT NULL DEFAULT 'unknown',
  contacts_summary jsonb NOT NULL DEFAULT '[]'::jsonb,
  consents_summary jsonb NOT NULL DEFAULT '{}'::jsonb,
  custom_data jsonb NOT NULL DEFAULT '{}'::jsonb,
  order_count integer NOT NULL DEFAULT 0 CHECK (order_count >= 0),
  total_spent_cents bigint NOT NULL DEFAULT 0 CHECK (total_spent_cents >= 0),
  avg_order_value_cents numeric NOT NULL DEFAULT 0 CHECK (avg_order_value_cents >= 0),
  avg_items_per_order numeric NOT NULL DEFAULT 0 CHECK (avg_items_per_order >= 0),
  last_order_at timestamptz NULL,
  r smallint NULL CHECK (r IS NULL OR (r BETWEEN 1 AND 5)),
  f smallint NULL CHECK (f IS NULL OR (f BETWEEN 1 AND 5)),
  m smallint NULL CHECK (m IS NULL OR (m BETWEEN 1 AND 5)),
  rfm_segment text NULL,
  recency_days integer NULL CHECK (recency_days IS NULL OR recency_days >= 0),
  frequency integer NULL CHECK (frequency IS NULL OR frequency >= 0),
  monetary_cents bigint NULL CHECK (monetary_cents IS NULL OR monetary_cents >= 0),
  rfm_snapshot_id uuid NULL REFERENCES rfm_snapshots (id) ON DELETE SET NULL,
  rfm_as_of date NULL,
  clv_p_alive double precision NULL CHECK (clv_p_alive IS NULL OR (clv_p_alive >= 0 AND clv_p_alive <= 1)),
  clv_expected_purchases double precision NULL CHECK (clv_expected_purchases IS NULL OR clv_expected_purchases >= 0),
  clv_avg_order_value_cents double precision NULL CHECK (clv_avg_order_value_cents IS NULL OR clv_avg_order_value_cents >= 0),
  clv_cents double precision NULL CHECK (clv_cents IS NULL OR clv_cents >= 0),
  value_tier text NULL CHECK (value_tier IS NULL OR value_tier IN ('low', 'mid', 'high')),
  clv_snapshot_id uuid NULL REFERENCES clv_snapshots (id) ON DELETE SET NULL,
  clv_as_of date NULL,
  churn_p_alive double precision NULL CHECK (churn_p_alive IS NULL OR (churn_p_alive >= 0 AND churn_p_alive <= 1)),
  churn_expected_purchases double precision NULL CHECK (churn_expected_purchases IS NULL OR churn_expected_purchases >= 0),
  churn_band text NULL CHECK (churn_band IS NULL OR churn_band IN ('at-risk', 'cooling', 'active')),
  churn_snapshot_id uuid NULL REFERENCES churn_snapshots (id) ON DELETE SET NULL,
  churn_as_of date NULL,
  segment_id integer NULL CHECK (segment_id IS NULL OR segment_id >= 0),
  segment_label text NULL,
  segment_mode text NULL,
  segment_run_id uuid NULL REFERENCES segment_runs (id) ON DELETE SET NULL,
  segment_as_of date NULL,
  refreshed_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (brand_id, group_id, customer_id)
);
CREATE INDEX customer_single_view_scope_idx
  ON customer_single_view (brand_id, group_id);
CREATE INDEX customer_single_view_avg_order_idx
  ON customer_single_view (brand_id, group_id, avg_order_value_cents);
CREATE INDEX customer_single_view_churn_idx
  ON customer_single_view (brand_id, group_id, churn_p_alive);
CREATE INDEX customer_single_view_rfm_segment_idx
  ON customer_single_view (brand_id, group_id, rfm_segment);
CREATE INDEX customer_single_view_value_tier_idx
  ON customer_single_view (brand_id, group_id, value_tier);
CREATE INDEX customer_single_view_churn_band_idx
  ON customer_single_view (brand_id, group_id, churn_band);

-- Refresh one scope from live tables + newest in-scope snapshots.
-- Refuses a group-alone scope (brand NULL) and a global scope (both NULL):
-- the pair is only selectable as a brand's own pair, so the group is
-- resolved from the brand and a mismatched group is refused too.
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
      AND s.group_id IS NOT DISTINCT FROM v_group_id
    ORDER BY s.window_to DESC, s.scored_at DESC, s.id DESC LIMIT 1
  ),
  latest_clv AS (
    SELECT s.id, s.as_of FROM clv_snapshots s, biz_today b
    WHERE s.horizon_days = 90
      AND s.brand_id IS NOT DISTINCT FROM p_brand_id
      AND s.group_id IS NOT DISTINCT FROM v_group_id
      AND s.as_of <= b.d
    ORDER BY s.as_of DESC, s.created_at DESC, s.id DESC LIMIT 1
  ),
  latest_churn AS (
    SELECT s.id, s.as_of FROM churn_snapshots s, biz_today b
    WHERE s.activity = 'orders' AND s.horizon_days = 90
      AND s.brand_id IS NOT DISTINCT FROM p_brand_id
      AND s.group_id IS NOT DISTINCT FROM v_group_id
      AND s.as_of <= b.d
    ORDER BY s.as_of DESC, s.created_at DESC, s.id DESC LIMIT 1
  ),
  latest_seg AS (
    SELECT s.id, s.as_of FROM segment_runs s, biz_today b
    WHERE s.mode = 'overall'
      AND s.brand_id IS NOT DISTINCT FROM p_brand_id
      AND s.group_id IS NOT DISTINCT FROM v_group_id
      AND s.as_of <= b.d
    ORDER BY s.as_of DESC, s.created_at DESC, s.id DESC LIMIT 1
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
DROP FUNCTION IF EXISTS refresh_customer_single_view(uuid, uuid);
DROP INDEX IF EXISTS customer_single_view_churn_band_idx;
DROP INDEX IF EXISTS customer_single_view_value_tier_idx;
DROP INDEX IF EXISTS customer_single_view_rfm_segment_idx;
DROP INDEX IF EXISTS customer_single_view_churn_idx;
DROP INDEX IF EXISTS customer_single_view_avg_order_idx;
DROP INDEX IF EXISTS customer_single_view_scope_idx;
DROP TABLE IF EXISTS customer_single_view;
