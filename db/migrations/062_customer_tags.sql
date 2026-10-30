-- migrate:up
-- 062: customer_tags (data, [ADDITIVE]) — named audiences over SCV columns.
--
-- customer_tags holds the definition; customer_tag_assignments the
-- membership (written by tag evaluation after the SCV refresh, or by hand
-- with source = 'manual'); tag_runs records one evaluation per tag scope
-- with its matched count. Tags are scoped like snapshots: the pair
-- (brand_id, group_id), never a global row — the six system seeds below
-- are stamped on the DEMO scope (group 60000000-…-000001, brand
-- 60100000-…-000001, per 011), and every new scope gets its own copies.
--
-- Rule JSON mini-schema (evaluated by a future reader, stored here):
-- {"all":[...]} / {"any":[...]} of atoms {"col","op","value"} where col is
-- a customer_single_view column and op is one of = != < <= > >= in
-- is_null not_null contains (jsonb @>). Money thresholds are in cents,
-- consistent with total_spent_cents / avg_order_value_cents.

CREATE TABLE customer_tags (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  brand_id uuid NOT NULL REFERENCES brands (id),
  group_id uuid NOT NULL REFERENCES groups (id),
  name text NOT NULL CHECK (char_length(name) BETWEEN 1 AND 64),
  description text NOT NULL DEFAULT '',
  rule jsonb NOT NULL,
  is_system boolean NOT NULL DEFAULT false,
  created_by text NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (brand_id, group_id, name)
);
CREATE INDEX customer_tags_scope_idx ON customer_tags (brand_id, group_id);

CREATE TABLE tag_runs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tag_id uuid NOT NULL REFERENCES customer_tags (id) ON DELETE CASCADE,
  brand_id uuid NOT NULL REFERENCES brands (id),
  group_id uuid NOT NULL REFERENCES groups (id),
  matched_count integer NOT NULL DEFAULT 0 CHECK (matched_count >= 0),
  started_at timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz NULL
);
CREATE INDEX tag_runs_tag_idx ON tag_runs (tag_id);

CREATE TABLE customer_tag_assignments (
  tag_id uuid NOT NULL REFERENCES customer_tags (id) ON DELETE CASCADE,
  customer_id uuid NOT NULL REFERENCES customers (id) ON DELETE CASCADE,
  source text NOT NULL DEFAULT 'rule' CHECK (source IN ('rule', 'manual')),
  run_id uuid NULL REFERENCES tag_runs (id) ON DELETE SET NULL,
  assigned_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tag_id, customer_id)
);
CREATE INDEX customer_tag_assignments_customer_idx
  ON customer_tag_assignments (customer_id);

-- Six system tags on the DEMO scope (seed defaults; a tag reader copies
-- them per scope). Cents thresholds sit below typical baskets/CLV so the
-- demo seed matches rows: avg basket 5000c ($50), CLV 100000c ($1000).
INSERT INTO customer_tags
  (brand_id, group_id, name, description, rule, is_system, created_by)
VALUES
  ('60100000-0000-0000-0000-000000000001',
   '60000000-0000-0000-0000-000000000001',
   'high_value_retain_target', 'High CLV customers showing churn signal',
   '{"all": [{"col": "value_tier", "op": "=", "value": "high"}, {"col": "churn_band", "op": "in", "value": ["at-risk", "cooling"]}, {"col": "clv_cents", "op": ">=", "value": 100000}]}',
   true, 'seed-062'),
  ('60100000-0000-0000-0000-000000000001',
   '60000000-0000-0000-0000-000000000001',
   'small_amount_item_purchaser', 'Repeat buyers with a small average basket',
   '{"all": [{"col": "avg_order_value_cents", "op": "<", "value": 5000}, {"col": "order_count", "op": ">=", "value": 2}]}',
   true, 'seed-062'),
  ('60100000-0000-0000-0000-000000000001',
   '60000000-0000-0000-0000-000000000001',
   'at_risk_cooler', 'Customers in the cooling band',
   '{"all": [{"col": "churn_band", "op": "=", "value": "cooling"}]}',
   true, 'seed-062'),
  ('60100000-0000-0000-0000-000000000001',
   '60000000-0000-0000-0000-000000000001',
   'dormant_reactivation_candidate', 'Past buyers with no recent purchase',
   '{"all": [{"col": "recency_days", "op": ">=", "value": 180}, {"col": "order_count", "op": ">=", "value": 1}]}',
   true, 'seed-062'),
  ('60100000-0000-0000-0000-000000000001',
   '60000000-0000-0000-0000-000000000001',
   'new_unknown_no_scores', 'New customers with no RFM snapshot yet',
   '{"all": [{"col": "order_count", "op": "<=", "value": 1}, {"col": "rfm_snapshot_id", "op": "is_null"}]}',
   true, 'seed-062'),
  ('60100000-0000-0000-0000-000000000001',
   '60000000-0000-0000-0000-000000000001',
   'opted_out_sensitive', 'Customers opted out of direct messaging',
   '{"any": [{"col": "consents_summary", "op": "contains", "value": {"sms": "opted_out"}}, {"col": "consents_summary", "op": "contains", "value": {"email": "opted_out"}}, {"col": "consents_summary", "op": "contains", "value": {"whatsapp": "opted_out"}}]}',
   true, 'seed-062')
ON CONFLICT (brand_id, group_id, name) DO NOTHING;

-- migrate:down
DROP TABLE IF EXISTS customer_tag_assignments;
DROP TABLE IF EXISTS tag_runs;
DROP TABLE IF EXISTS customer_tags;
