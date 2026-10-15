-- migrate:up
-- 024: LD1 custom-data side tables (data, [ADDITIVE]).
--
-- Schemaless overflow for D1 csv/excel `column_map` leftovers: one row
-- per core entity on the SAME pk, jsonb `data` merged on write
-- (data = data || patch, key by key). Core tables never widen for
-- source-specific fields. ON DELETE CASCADE: side rows die with the
-- entity. Writers: pipelines.apply upsert_custom_data (D1 leftover
-- flow calls it; nothing writes here yet).

CREATE TABLE customer_custom_data (
  customer_id uuid PRIMARY KEY REFERENCES customers (id) ON DELETE CASCADE,
  data jsonb NOT NULL DEFAULT '{}'::jsonb,
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE order_custom_data (
  order_id uuid PRIMARY KEY REFERENCES orders (id) ON DELETE CASCADE,
  data jsonb NOT NULL DEFAULT '{}'::jsonb,
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE event_custom_data (
  event_id uuid PRIMARY KEY REFERENCES customer_events (id)
    ON DELETE CASCADE,
  data jsonb NOT NULL DEFAULT '{}'::jsonb,
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE product_custom_data (
  product_id uuid PRIMARY KEY REFERENCES products (id) ON DELETE CASCADE,
  data jsonb NOT NULL DEFAULT '{}'::jsonb,
  updated_at timestamptz NOT NULL DEFAULT now()
);

-- migrate:down
DROP TABLE IF EXISTS product_custom_data;
DROP TABLE IF EXISTS event_custom_data;
DROP TABLE IF EXISTS order_custom_data;
DROP TABLE IF EXISTS customer_custom_data;
