-- Named-query convention for db/queries/*.sql (one file per resource).
-- Rules:
--   1. One file per resource: customers.sql, consents.sql, orders.sql, products.sql.
--   2. Each query starts with `-- name: <query_name>` followed by a comment line
--      describing params, e.g. `-- params: customer_id uuid`.
--   3. snake_case names, $1-style placeholders, explicit column lists (no SELECT *).
--   4. Search queries: exact match on normalized phone/email; pg_trgm only for names.
--   5. PII routes must set Cache-Control: no-store and skip body logging (see deps.py).

-- name: template_by_id
-- params: id uuid
SELECT id, member_id, full_name, attributes, created_at, updated_at, deleted_at
FROM customers
WHERE id = $1 AND deleted_at IS NULL;
