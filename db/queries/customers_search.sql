-- Customer-search queries (canonical SQL; loaded by services/api/repos/search.py).
-- Email matches exact normalized (lowercase) first, then a case-insensitive
-- substring fallback for staff keyword search (see customers_by_email_partial).
-- Phone matches exact normalized first, then a digit-substring
-- fallback for staff keyword search (see customers_by_phone_partial).
-- Names use ILIKE (pg_trgm GIN where available).

-- name: customers_by_phone
-- params: value_normalized text
SELECT c.id, c.member_id, c.external_member_id, c.full_name, c.attributes, c.created_at, c.updated_at
FROM customers c
JOIN customer_contacts cc ON cc.customer_id = c.id
WHERE cc.kind = 'phone' AND cc.value_normalized = $1
  AND c.deleted_at IS NULL;

-- name: customers_by_phone_partial
-- params: needle text, limit int
-- Digit-substring fallback for the phone criterion (staff keyword search
-- from the topbar / search page, e.g. last-4 "9021" matching
-- "+85291902134"). Exact normalized match is always tried first (see
-- repos/search.py); this runs only when exact finds nothing. Callers pass
-- digits only (LIKE wildcards impossible) with ESCAPE '\'. Email stays
-- exact-only. POST-body + auth + rate-limit + audit are unchanged.
SELECT c.id, c.member_id, c.external_member_id, c.full_name, c.attributes, c.created_at, c.updated_at
FROM customers c
JOIN customer_contacts cc ON cc.customer_id = c.id
WHERE cc.kind = 'phone' AND cc.value_normalized LIKE '%' || $1 || '%' ESCAPE '\'
  AND c.deleted_at IS NULL
ORDER BY c.full_name ASC
LIMIT $2;

-- name: customers_by_email
-- params: value_normalized text
SELECT c.id, c.member_id, c.external_member_id, c.full_name, c.attributes, c.created_at, c.updated_at
FROM customers c
JOIN customer_contacts cc ON cc.customer_id = c.id
WHERE cc.kind = 'email' AND cc.value_normalized = $1
  AND c.deleted_at IS NULL;

-- name: customers_by_email_partial
-- params: needle text, limit int
-- Substring fallback for the email criterion (staff keyword search, e.g.
-- "taiman@" or "@example.com"). Exact normalized match is always tried
-- first (see repos/search.py); this runs only when exact finds nothing.
-- Callers lowercase the needle and escape LIKE wildcards (\\, %, _) with
-- ESCAPE '\'. POST-body + auth + rate-limit + audit are unchanged.
SELECT c.id, c.member_id, c.external_member_id, c.full_name, c.attributes, c.created_at, c.updated_at
FROM customers c
JOIN customer_contacts cc ON cc.customer_id = c.id
WHERE cc.kind = 'email' AND cc.value_normalized LIKE '%' || $1 || '%' ESCAPE '\'
  AND c.deleted_at IS NULL
ORDER BY c.full_name ASC
LIMIT $2;

-- name: customers_by_member_id
-- params: member_id text
-- Exact match on EITHER the internal member_id OR the client POS external_member_id.
SELECT id, member_id, external_member_id, full_name, attributes, created_at, updated_at
FROM customers
WHERE (member_id = $1 OR external_member_id = $1) AND deleted_at IS NULL;

-- name: customers_by_name
-- params: name text, limit int
-- Legacy single-needle lookup (kept for compatibility). Canonical name search
-- is customers_by_name_tokens below: multi-keyword AND semantics.
SELECT id, member_id, external_member_id, full_name, attributes, created_at, updated_at
FROM customers
WHERE full_name ILIKE '%' || $1 || '%' AND deleted_at IS NULL
ORDER BY full_name ASC
LIMIT $2;

-- name: customers_by_name_tokens
-- params: tokens text[], limit int
-- Multi-keyword AND name match: EVERY token must appear as a case-insensitive
-- partial match (ILIKE %token%). Callers escape LIKE wildcards (\, %, _) and
-- pass ESCAPE '\'. Phone/email matching is untouched (exact-only elsewhere).
SELECT id, member_id, external_member_id, full_name, attributes, created_at, updated_at
FROM customers
WHERE (
  SELECT bool_and(full_name ILIKE '%' || t || '%' ESCAPE '\')
  FROM unnest($1::text[]) AS t
) AND deleted_at IS NULL
ORDER BY full_name ASC
LIMIT $2;

-- name: customers_by_member_id_partial
-- params: needle text, limit int
-- Substring fallback for the member_id criterion (internal M-xxx OR client
-- POS external_member_id). Counter IDs are low-sensitivity (safe in URLs via
-- GET /customers?member_id=), so LIKE here never widens PII exposure:
-- phone/email stay exact-normalized-only elsewhere. Callers escape LIKE
-- wildcards (\, %, _) and pass ESCAPE '\'.
SELECT id, member_id, external_member_id, full_name, attributes, created_at, updated_at
FROM customers
WHERE (member_id ILIKE '%' || $1 || '%' ESCAPE '\'
   OR external_member_id ILIKE '%' || $1 || '%' ESCAPE '\')
  AND deleted_at IS NULL
ORDER BY member_id ASC
LIMIT $2;

-- name: customer_by_id
-- params: id uuid
SELECT id, member_id, external_member_id, full_name, attributes, created_at, updated_at
FROM customers
WHERE id = $1 AND deleted_at IS NULL;

-- name: contacts_by_customer_ids
-- params: customer_ids uuid[]
SELECT id, customer_id, kind, value_normalized, value_display, is_primary, created_at
FROM customer_contacts
WHERE customer_id = ANY($1::uuid[])
ORDER BY created_at ASC;

-- name: access_log_insert
-- params: staff_id text, customer_id uuid (nullable), criteria_type text, result_count int
INSERT INTO customer_access_logs (staff_id, customer_id, criteria_type, result_count)
VALUES ($1, $2, $3, $4);
