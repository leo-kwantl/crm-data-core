-- migrate:up
-- Phase 4: offline recommendation batches. A batch is one full run of the
-- batch recommender (assoc rules + item similarity + popularity cold-start);
-- items are the ranked recs per customer. Batches are immutable history —
-- a refit writes a new batch, never edits an old one (same convention as
-- rfm_snapshots). Readers always serve the latest batch.

CREATE TABLE rec_batches (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  algos text NOT NULL, -- e.g. 'assoc+itemsim+popular'
  params jsonb NOT NULL DEFAULT '{}'::jsonb,
  customer_count integer NOT NULL DEFAULT 0 CHECK (customer_count >= 0),
  item_count integer NOT NULL DEFAULT 0 CHECK (item_count >= 0),
  created_by text NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE rec_items (
  batch_id uuid NOT NULL REFERENCES rec_batches (id) ON DELETE CASCADE,
  customer_id uuid NOT NULL REFERENCES customers (id) ON DELETE CASCADE,
  product_id uuid NOT NULL REFERENCES products (id) ON DELETE CASCADE,
  score double precision NOT NULL CHECK (score >= 0),
  rank integer NOT NULL CHECK (rank >= 1),
  reason text NOT NULL CHECK (char_length(reason) BETWEEN 1 AND 200),
  PRIMARY KEY (batch_id, customer_id, rank)
);
CREATE INDEX rec_items_lookup_idx ON rec_items (batch_id, customer_id);

-- migrate:down
DROP TABLE IF EXISTS rec_items;
DROP TABLE IF EXISTS rec_batches;
