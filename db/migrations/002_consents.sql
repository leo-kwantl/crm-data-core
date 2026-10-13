-- migrate:up
CREATE TABLE consents (
  customer_id uuid NOT NULL REFERENCES customers (id),
  channel text NOT NULL CHECK (channel IN ('sms', 'email', 'whatsapp')),
  status text NOT NULL CHECK (status IN ('opted_in', 'opted_out')),
  captured_by text NOT NULL,
  captured_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (customer_id, channel)
);

CREATE TABLE consent_events (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id uuid NOT NULL REFERENCES customers (id),
  channel text NOT NULL,
  status text NOT NULL,
  captured_by text NOT NULL,
  captured_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX consent_events_customer_idx ON consent_events (customer_id, captured_at);

-- Append-only: no UPDATE/DELETE grants for app roles; owner keeps full rights.
REVOKE UPDATE, DELETE ON consent_events FROM PUBLIC;

-- migrate:down
DROP TABLE IF EXISTS consent_events;
DROP TABLE IF EXISTS consents;
