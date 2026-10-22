-- Named queries for read-only product lookup.
-- Convention: `-- name: <query_name>` + `-- params: ...` comment lines.
-- Products are non-PII, so LIKE matching on name/sku is allowed here
-- (unlike phone/email customer search, which is exact-match only).

-- name: list_products
-- params: q text|None, cursor_at timestamptz|None, cursor_id uuid|None, limit int, category text|None, is_active bool|None
-- Keyset page on (created_at DESC, id DESC). Fetch limit+1 upstream.
SELECT id, sku, name, category, price_cents,
       description, specs, image_url, is_active, created_at
FROM products
WHERE ($1::text IS NULL OR name ILIKE '%' || $1 || '%' OR sku ILIKE '%' || $1 || '%')
  AND ($5::text IS NULL OR category = $5)
  AND ($6::boolean IS NULL OR is_active = $6)
  AND ($2::timestamptz IS NULL OR (created_at, id) < ($2::timestamptz, $3::uuid))
ORDER BY created_at DESC, id DESC
LIMIT $4;

-- name: get_product
-- params: product_id uuid
SELECT id, sku, name, category, price_cents,
       description, specs, image_url, is_active, created_at
FROM products
WHERE id = $1;

-- name: list_inventory_for_product
-- params: product_id uuid, brand_id uuid|None
-- Stock rows for one product with store display fields, store-code order.
-- SCOPE: brand_id is the session's own brand (deps.session_brand).
SELECT i.id, i.product_id, i.store_id, s.code AS store_code, s.name AS store_name,
       i.qty, i.updated_at
FROM inventory i
JOIN stores s ON s.id = i.store_id
WHERE i.product_id = $1
  AND ($2::uuid IS NULL OR s.brand_id = $2::uuid)
ORDER BY s.code ASC;

-- name: upsert_inventory
-- params: product_id uuid, store_id uuid, qty int
-- Insert or replace the (product, store) stock row.
INSERT INTO inventory (product_id, store_id, qty)
VALUES ($1, $2, $3)
ON CONFLICT (product_id, store_id)
DO UPDATE SET qty = EXCLUDED.qty, updated_at = now()
RETURNING id, product_id, store_id, qty, updated_at;
