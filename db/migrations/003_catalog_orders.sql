-- migrate:up
CREATE TABLE stores (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  code text NOT NULL UNIQUE,
  name text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE products (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  sku text NOT NULL UNIQUE,
  name text NOT NULL,
  category text NOT NULL,
  price_cents integer NOT NULL CHECK (price_cents >= 0),
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX products_name_trgm_fallback ON products (name);
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm') THEN
    EXECUTE 'CREATE INDEX IF NOT EXISTS products_name_trgm ON products USING gin (name gin_trgm_ops)';
  END IF;
END $$;

CREATE TABLE orders (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id uuid NOT NULL REFERENCES customers (id),
  store_id uuid NOT NULL REFERENCES stores (id),
  ordered_at timestamptz NOT NULL DEFAULT now(),
  total_cents integer NOT NULL CHECK (total_cents >= 0),
  status text NOT NULL DEFAULT 'completed',
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX orders_customer_idx ON orders (customer_id, ordered_at DESC);

CREATE TABLE order_items (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  order_id uuid NOT NULL REFERENCES orders (id),
  product_id uuid NOT NULL REFERENCES products (id),
  qty integer NOT NULL CHECK (qty > 0),
  unit_price_cents integer NOT NULL CHECK (unit_price_cents >= 0)
);
CREATE INDEX order_items_order_idx ON order_items (order_id);

CREATE TABLE customer_access_logs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  staff_id text NOT NULL,
  customer_id uuid REFERENCES customers (id),
  criteria_type text NOT NULL,
  result_count integer NOT NULL DEFAULT 0,
  at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX customer_access_logs_staff_idx ON customer_access_logs (staff_id, at DESC);

-- Append-only: no UPDATE/DELETE grants for app roles.
REVOKE UPDATE, DELETE ON customer_access_logs FROM PUBLIC;

-- migrate:down
DROP INDEX IF EXISTS products_name_trgm;
DROP INDEX IF EXISTS products_name_trgm_fallback;
DROP TABLE IF EXISTS customer_access_logs;
DROP TABLE IF EXISTS order_items;
DROP TABLE IF EXISTS orders;
DROP TABLE IF EXISTS products;
DROP TABLE IF EXISTS stores;
