-- migrate:up
-- 053: recommendation attribution (data D3, [BREAKING ingest]).
--
-- The capture path that makes an online `shipped_wins` claim possible:
-- a token is minted when a recommendation is SERVED, and the order line
-- that answers it is attributed to that token.
--
-- `rec_clicks` is the single source of truth for "what was served": the
-- batch, customer, product and rank name the recommendation that was
-- shown. The writer of that row is the server that serves the
-- recommendation (app writes it in the same request that serves it).
--
-- `order_recommendations` is what data writes at apply time, and it is
-- per ORDER LINE, not per order: one order can hold several products
-- from several recommendations, and the line is the truth. It is
-- APPEND-ONLY - one row per order line, never updated: a fact that was
-- true once.
--
-- No denormalisation: batch / customer / product / rank are NOT copied
-- into `order_recommendations`, because a copied column can drift from
-- the row it was copied from. The reader joins.
--
-- THE MINT IS DETERMINISTIC (uuid v5 over batch_id, customer_id,
-- product_id, rank), so serving the same recommendation twice lands on
-- the same primary key. That is the point: "the staff sent this twice"
-- is a normal thing, and it must not raise. So the write is an UPSERT:
--
--   INSERT INTO rec_clicks
--     (rec_ref, batch_id, customer_id, product_id, rank)
--   VALUES ($1, $2, $3, $4, $5)
--   ON CONFLICT (rec_ref) DO UPDATE
--     SET served_count = rec_clicks.served_count + 1,
--         last_served_at = now();
--
-- `served_at` is when it was FIRST served and never moves; `last_served_at`
-- is the most recent serve; `served_count` is how many times. The token
-- is opaque to the merchant either way - it leaks neither the rank nor
-- the batch.
--
-- `rec_clicks.batch_id` is ON DELETE SET NULL, and nullable, for one
-- reason: the retention job ages `rec_batches` (365 days) and cascades
-- to `rec_items`, so a batch can disappear while the orders that answer
-- its recommendations are still arriving. A click that vanished with its
-- batch would stop resolving, and an unresolvable token quarantines a
-- valid order line -- housekeeping must never cost a shop a sale. The
-- click outlives its batch; the customer / product / rank it carries are
-- what the attribution needs, and a NULL batch says "that batch was
-- aged" instead of pointing at a row that no longer exists.
--
-- THE READER'S RULE - SCOPE COMES FROM THE CUSTOMER, NEVER FROM THE
-- BATCH. A click that lost its batch has lost the brand that batch was
-- scoped to, so a scoped attribution read that filters on the batch's
-- brand would silently under-count from the first retention sweep
-- onwards - and an under-count that looks like a floor is not a floor.
-- The scope is still derivable: click -> customer_id -> customers.group_id
-- (a customer row never ages) and the brand follows the customer's
-- stores. So the reader joins the customer, not the batch, and the
-- `batch_id IS NULL` marker means "that batch was aged", not "that click
-- has no scope". A copied brand column would be exactly the
-- denormalisation this header refuses above, so it is not added.
--
-- The measurement is a FLOOR: order lines that carried no token are
-- invisible to it, so attributed revenue is a lower bound, never the
-- batch's revenue. "No token came back" is `not_measured`, never $0.

CREATE TABLE rec_clicks (
  rec_ref uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  batch_id uuid NULL REFERENCES rec_batches (id) ON DELETE SET NULL,
  customer_id uuid NOT NULL REFERENCES customers (id),
  product_id uuid NOT NULL REFERENCES products (id),
  rank integer NOT NULL CHECK (rank >= 1),
  served_at timestamptz NOT NULL DEFAULT now(),
  served_count integer NOT NULL DEFAULT 1 CHECK (served_count >= 1),
  last_served_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX rec_clicks_lookup_idx ON rec_clicks (customer_id, product_id);
CREATE INDEX rec_clicks_batch_idx ON rec_clicks (batch_id);

CREATE TABLE order_recommendations (
  order_item_id uuid NOT NULL REFERENCES order_items (id),
  rec_ref uuid NOT NULL REFERENCES rec_clicks (rec_ref),
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (order_item_id, rec_ref)
);
CREATE INDEX order_recommendations_ref_idx ON order_recommendations (rec_ref);

-- migrate:down
DROP TABLE IF EXISTS order_recommendations;
DROP TABLE IF EXISTS rec_clicks;
