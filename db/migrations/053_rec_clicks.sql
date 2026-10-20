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
-- from several recommendations, and the line is the truth.
--
-- No denormalisation: batch / customer / product / rank are NOT copied
-- into `order_recommendations`, because a copied column can drift from
-- the row it was copied from. The reader joins.
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

CREATE TABLE rec_clicks (
  rec_ref uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  batch_id uuid NULL REFERENCES rec_batches (id) ON DELETE SET NULL,
  customer_id uuid NOT NULL REFERENCES customers (id),
  product_id uuid NOT NULL REFERENCES products (id),
  rank integer NOT NULL CHECK (rank >= 1),
  created_at timestamptz NOT NULL DEFAULT now()
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
