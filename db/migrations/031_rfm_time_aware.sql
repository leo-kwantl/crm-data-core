-- migrate:up
-- 031: LD8 time-aware RFM storage (data, [ADDITIVE] analytics, joint
-- w/ ml per the ACKed joint contract).
--
-- Per-window snapshots reuse rfm_snapshots + rfm_customer_scores
-- unchanged except a `window` label ('current' = rolling 365d ending
-- as-of; 'mom' = previous calendar month; 'yoy' = same calendar
-- month prior year; 'lifetime' = all history) and rules_version 'v2'
-- for time-aware rows (v1 rows stay valid, untouched). Two new
-- tables: per-segment per-window sizes + medians (diff/pct vs the
-- comparator window served by query), and segment movement flows.
-- Entered/exited customers are rows with the ENTERED/EXITED
-- sentinels (uppercase — real segments are lowercase, no collision),
-- always with pct_of_from NULL: they sit OUTSIDE the flow pct
-- denominator by construction. ml executes the movement math
-- verbatim; this file is the grain it writes to.

ALTER TABLE rfm_snapshots
  ADD COLUMN window_key text NOT NULL DEFAULT 'current'
    CHECK (window_key IN ('current', 'mom', 'yoy', 'lifetime'));

CREATE TABLE rfm_segment_sizes (
  as_of date NOT NULL,
  window_key text NOT NULL
    CHECK (window_key IN ('current', 'mom', 'yoy', 'lifetime')),
  segment text NOT NULL
    CHECK (char_length(segment) BETWEEN 1 AND 64),
  n integer NOT NULL CHECK (n >= 0),
  median_r double precision NOT NULL,
  median_f double precision NOT NULL,
  median_m double precision NOT NULL,
  PRIMARY KEY (as_of, window_key, segment)
);

CREATE TABLE rfm_segment_movement (
  as_of date NOT NULL,
  pair text NOT NULL
    CHECK (pair IN ('current_x_mom', 'current_x_yoy')),
  from_segment text NOT NULL
    CHECK (char_length(from_segment) BETWEEN 1 AND 64),
  to_segment text NOT NULL
    CHECK (char_length(to_segment) BETWEEN 1 AND 64),
  n integer NOT NULL CHECK (n >= 0),
  pct_of_from double precision NULL
    CHECK (pct_of_from IS NULL
           OR (pct_of_from >= 0 AND pct_of_from <= 1)),
  PRIMARY KEY (as_of, pair, from_segment, to_segment)
);

CREATE INDEX rfm_movement_asof_pair_idx
  ON rfm_segment_movement (as_of, pair);

REVOKE UPDATE, DELETE
  ON rfm_segment_sizes, rfm_segment_movement FROM PUBLIC;

-- migrate:down
DROP TABLE IF EXISTS rfm_segment_movement;
DROP TABLE IF EXISTS rfm_segment_sizes;
ALTER TABLE rfm_snapshots DROP COLUMN IF EXISTS window_key;
