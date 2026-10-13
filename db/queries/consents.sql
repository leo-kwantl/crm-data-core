-- Consent queries (canonical SQL; loaded by services/api/repos/consents.py).
-- Writes to `consents` and appends to `consent_events` in ONE transaction
-- (see repo.replace_consents). History table is NEVER updated or deleted.

-- name: consents_by_customer
-- params: customer_id uuid
SELECT customer_id, channel, status, captured_by, captured_at
FROM consents
WHERE customer_id = $1
ORDER BY channel;

-- name: consent_events_by_customer
-- params: customer_id uuid
SELECT id, customer_id, channel, status, captured_by, captured_at
FROM consent_events
WHERE customer_id = $1
ORDER BY captured_at ASC, id ASC;

-- name: customer_exists
-- params: customer_id uuid
SELECT 1 AS ok
FROM customers
WHERE id = $1 AND deleted_at IS NULL;

-- name: consent_upsert
-- params: customer_id uuid, channel text, status text, captured_by text
INSERT INTO consents (customer_id, channel, status, captured_by, captured_at)
VALUES ($1, $2, $3, $4, now())
ON CONFLICT (customer_id, channel)
DO UPDATE SET status = EXCLUDED.status,
              captured_by = EXCLUDED.captured_by,
              captured_at = EXCLUDED.captured_at
RETURNING customer_id, channel, status, captured_by, captured_at;

-- name: consent_event_insert
-- params: customer_id uuid, channel text, status text, captured_by text
INSERT INTO consent_events (customer_id, channel, status, captured_by)
VALUES ($1, $2, $3, $4)
RETURNING id, customer_id, channel, status, captured_by, captured_at;

-- Profile-derivation stub (for profile agent reuse): overall subscription
-- status derived from per-channel consent. Equivalent repo helper:
-- repos.consents.derive_contactability(rows). A real SQL view needs a
-- migration (owned by another track), so the canonical definition lives here:
--
--   CREATE OR REPLACE VIEW v_customer_subscription AS
--   SELECT c.id AS customer_id,
--          bool_or(ct.status = 'opted_in') FILTER (WHERE ct.channel IS NOT NULL)
--            IS TRUE AS is_contactable_for_marketing,
--          count(*) FILTER (WHERE ct.status = 'opted_in') AS opted_in_channels,
--          count(*) AS channel_count
--   FROM customers c
--   LEFT JOIN consents ct ON ct.customer_id = c.id
--   GROUP BY c.id;
--
-- name: customer_subscription_derived
-- params: customer_id uuid
SELECT count(*) FILTER (WHERE status = 'opted_in') > 0 AS is_contactable_for_marketing,
       count(*) FILTER (WHERE status = 'opted_in') AS opted_in_channels,
       count(*) AS channel_count
FROM consents
WHERE customer_id = $1;
