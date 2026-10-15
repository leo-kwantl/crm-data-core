-- migrate:up
-- 026: LD11 report definitions registry (data, [ADDITIVE]).
--
-- Report pages (overview/rfm/churn/clv/...) are app-served, but their
-- DEFINITIONS live here so snapshots and pages can't drift: each row
-- pins (axes, metrics, filters) for one report version. Immutable rows
-- (REVOKE UPDATE/DELETE, same convention as 013 snapshots): a new
-- version is a new row; readers take MAX(version) per report_id.
-- Any RFM/design writeup rides inside definition jsonb (key
-- "rfm_note") — no second table.

CREATE TABLE report_definitions (
  report_id text NOT NULL,
  version int NOT NULL CHECK (version >= 1),
  name text NOT NULL CHECK (char_length(name) BETWEEN 1 AND 128),
  kind text NOT NULL CHECK (char_length(kind) BETWEEN 1 AND 32),
  grain text NOT NULL CHECK (char_length(grain) BETWEEN 1 AND 32),
  definition jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (report_id, version)
);

REVOKE UPDATE, DELETE ON report_definitions FROM PUBLIC;

-- migrate:down
DROP TABLE IF EXISTS report_definitions;
