-- Named customer queries (raw SQL, asyncpg $n placeholders).
-- PII: phone/email match exact normalized values only; never LIKE on PII.

-- name: customer_by_id
-- params: customer_id uuid
SELECT id, member_id, external_member_id, full_name, attributes,
  gender, date_of_birth, district, address_line,
  date_part('year', age(date_of_birth))::int AS age_years,
  created_at, updated_at
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
--   gender text|None, date_of_birth date|None, district text|None, address_line text|None
INSERT INTO customers (member_id, external_member_id, full_name, attributes,
  gender, date_of_birth, district, address_line)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
RETURNING id, member_id, external_member_id, full_name, attributes,
  gender, date_of_birth, district, address_line, created_at, updated_at;

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
SET attributes = COALESCE(attributes, '{}'::jsonb) || $2, updated_at = now()
WHERE id = $1 AND deleted_at IS NULL;

-- name: customer_replace_contacts_delete
-- params: customer_id uuid
DELETE FROM customer_contacts WHERE customer_id = $1;

-- name: customer_soft_delete
-- params: customer_id uuid
UPDATE customers SET deleted_at = now(), updated_at = now()
WHERE id = $1 AND deleted_at IS NULL;

-- name: list_customers
-- params: cursor_at timestamptz|None, cursor_id uuid|None, limit int
-- Global masked list, newest-first on (created_at DESC, id DESC).
-- Keyset page: fetch limit+1 upstream. Contacts are masked at
-- serialization (see repos/customers.list_customers_page); never LIKE on PII.
SELECT id, member_id, external_member_id, full_name, created_at
FROM customers
WHERE deleted_at IS NULL
  AND ($1::timestamptz IS NULL OR (created_at, id) < ($1::timestamptz, $2::uuid))
ORDER BY created_at DESC, id DESC
LIMIT $3;

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
