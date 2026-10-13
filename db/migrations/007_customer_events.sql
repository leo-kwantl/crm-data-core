-- migrate:up
-- Customer journey events: append-only timeline per customer.
-- kind is free TEXT (note/visit/call/complaint/review/...); a later
-- reference-data pass owns the vocabulary — do NOT add a lookup table here.

CREATE TABLE customer_events (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id uuid NOT NULL REFERENCES customers (id) ON DELETE CASCADE,
  occurred_at timestamptz NOT NULL DEFAULT now(),
  kind text NOT NULL CHECK (char_length(kind) BETWEEN 1 AND 64),
  source text NOT NULL DEFAULT 'manual' CHECK (char_length(source) BETWEEN 1 AND 64),
  summary text NOT NULL CHECK (char_length(summary) BETWEEN 1 AND 2000),
  payload jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_by text NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX customer_events_customer_idx
  ON customer_events (customer_id, occurred_at DESC);

-- Append-only: no UPDATE/DELETE grants for app roles; owner keeps full rights.
REVOKE UPDATE, DELETE ON customer_events FROM PUBLIC;

-- migrate:down
DROP TABLE IF EXISTS customer_events;
