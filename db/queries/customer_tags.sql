-- Customer-tag serving forms (raw SQL, asyncpg $n placeholders).
-- Reads are scoped by the tag's own (brand_id, group_id); assignments
-- resolve through the tag, never across scopes.

-- name: tag_list
-- params: brand_id uuid, group_id uuid
SELECT id, brand_id, group_id, name, description, rule, is_system,
  created_by, created_at, updated_at
FROM customer_tags
WHERE brand_id = $1::uuid
  AND group_id = $2::uuid
ORDER BY name;

-- name: tag_by_name
-- params: brand_id uuid, group_id uuid, name text
SELECT id, brand_id, group_id, name, description, rule, is_system,
  created_by, created_at, updated_at
FROM customer_tags
WHERE brand_id = $1::uuid
  AND group_id = $2::uuid
  AND name = $3::text;

-- name: tag_create
-- params: brand_id uuid, group_id uuid, name text, description text, rule jsonb, is_system bool, created_by text|None
INSERT INTO customer_tags
  (brand_id, group_id, name, description, rule, is_system, created_by)
VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7)
ON CONFLICT (brand_id, group_id, name) DO NOTHING
RETURNING id, name;

-- name: tag_assign
-- params: tag_id uuid, customer_id uuid, source text, run_id uuid|None
INSERT INTO customer_tag_assignments (tag_id, customer_id, source, run_id)
VALUES ($1, $2, $3, $4)
ON CONFLICT (tag_id, customer_id) DO UPDATE
  SET source = EXCLUDED.source, run_id = EXCLUDED.run_id,
      assigned_at = now();

-- name: tag_unassign
-- params: tag_id uuid, customer_id uuid
DELETE FROM customer_tag_assignments
WHERE tag_id = $1 AND customer_id = $2;

-- name: tag_customers_page
-- params: tag_id uuid, limit_n int
-- Members through the tag's own scope (assignments carry no scope of
-- their own, so the tag id is the scope).
SELECT a.customer_id, a.source, a.assigned_at,
  v.member_id, v.full_name, v.value_tier, v.churn_band, v.rfm_segment
FROM customer_tag_assignments a
JOIN customer_tags t ON t.id = a.tag_id
LEFT JOIN customer_single_view v
  ON v.customer_id = a.customer_id
  AND v.brand_id = t.brand_id
  AND v.group_id = t.group_id
WHERE a.tag_id = $1::uuid
ORDER BY a.assigned_at DESC, a.customer_id ASC
LIMIT $2::int;

-- name: tag_run_open
-- params: tag_id uuid, brand_id uuid, group_id uuid
INSERT INTO tag_runs (tag_id, brand_id, group_id)
VALUES ($1, $2, $3)
RETURNING id, started_at;

-- name: tag_run_close
-- params: run_id uuid, matched_count int
UPDATE tag_runs SET matched_count = $2::int, finished_at = now()
WHERE id = $1::uuid;

-- name: tag_run_latest
-- params: tag_id uuid
SELECT id, matched_count, started_at, finished_at
FROM tag_runs
WHERE tag_id = $1::uuid
ORDER BY started_at DESC, id DESC
LIMIT 1;
