-- Named queries for purchase history (orders + order_items).
-- Convention: `-- name: <query_name>` + `-- params: ...` comment lines.

-- name: customer_exists
-- params: customer_id uuid
SELECT 1 AS one
FROM customers
WHERE id = $1 AND deleted_at IS NULL;

-- name: list_customer_orders
-- params: customer_id uuid, cursor_at timestamptz|None, cursor_id uuid|None, limit int
-- Keyset page newest-first on (ordered_at DESC, id DESC). Fetch limit+1 upstream.
SELECT o.id, o.customer_id, o.store_id, o.ordered_at, o.total_cents, o.status,
       (SELECT COUNT(*) FROM order_items i WHERE i.order_id = o.id) AS item_count
FROM orders o
WHERE o.customer_id = $1
  AND ($2::timestamptz IS NULL OR (o.ordered_at, o.id) < ($2::timestamptz, $3::uuid))
ORDER BY o.ordered_at DESC, o.id DESC
LIMIT $4;

-- name: get_order_header
-- params: order_id uuid
SELECT o.id, o.customer_id, o.store_id, o.ordered_at, o.total_cents, o.status,
       s.code AS store_code, s.name AS store_name
FROM orders o
JOIN stores s ON s.id = o.store_id
WHERE o.id = $1;

-- name: list_order_items
-- params: order_id uuid
SELECT i.id, i.order_id, i.product_id, i.qty, i.unit_price_cents,
       p.name AS product_name, p.sku AS sku
FROM order_items i
JOIN products p ON p.id = i.product_id
WHERE i.order_id = $1
ORDER BY i.id;

-- name: list_orders_global
-- params: cursor_at timestamptz|None, cursor_id uuid|None, limit int
-- Global order list, newest-first on (ordered_at DESC, id DESC) with store
-- + customer display fields and per-order item count. Keyset page: fetch
-- limit+1 upstream. Line items stay on GET /orders/:id (detail endpoint).
SELECT o.id, o.customer_id, c.member_id AS customer_member_id,
       c.full_name AS customer_name,
       o.store_id, s.code AS store_code, s.name AS store_name,
       o.ordered_at, o.total_cents, o.status,
       (SELECT COUNT(*) FROM order_items i WHERE i.order_id = o.id) AS item_count
FROM orders o
JOIN customers c ON c.id = o.customer_id AND c.deleted_at IS NULL
JOIN stores s ON s.id = o.store_id
WHERE ($1::timestamptz IS NULL OR (o.ordered_at, o.id) < ($1::timestamptz, $2::uuid))
ORDER BY o.ordered_at DESC, o.id DESC
LIMIT $3;
