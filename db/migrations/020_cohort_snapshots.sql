-- migrate:up
-- 020: cohort retention snapshots (data D2, [ADDITIVE] analytics).
--
-- Grain (agreed with app): one row per
-- (granularity x cohort_period x period_offset x scope) with cohort_size +
-- retained + retention_rate. App serves GET /analytics/cohort from this
-- ONE table (retention line + heatmap; granularity = week|month|year).
--
-- Math (see pipelines/contracts.md "020"): cohort of a customer =
-- date_trunc(granularity, first completed order); retained(offset k) =
-- cohort members with >= 1 completed order in period
-- (cohort_period + k·granularity); retention_rate = retained/cohort_size
-- (NULL when cohort_size = 0). Offset 0 is 1.0 by construction.
-- Writer: COHORT_SNAPSHOT_UPSERT_SQL in pipelines/snapshot.py
-- (a re-run RE-MEASURES its cells - ON CONFLICT ... DO UPDATE on this
-- index's expressions - and deletes the cells the population no longer
-- produces; see 056 and pipelines/contracts.md "020").

CREATE TABLE cohort_snapshots (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  granularity text NOT NULL CHECK (granularity IN ('week', 'month', 'year')),
  cohort_period date NOT NULL,
  period_offset integer NOT NULL CHECK (period_offset >= 0),
  cohort_size integer NOT NULL DEFAULT 0 CHECK (cohort_size >= 0),
  retained integer NOT NULL DEFAULT 0 CHECK (retained >= 0),
  retention_rate double precision NULL CHECK (retention_rate IS NULL
    OR (retention_rate >= 0 AND retention_rate <= 1)),
  created_at timestamptz NOT NULL DEFAULT now(),
  brand_id uuid NULL REFERENCES brands (id),
  group_id uuid NULL REFERENCES groups (id),
  CHECK (retained <= cohort_size)
);
CREATE UNIQUE INDEX cohort_snapshots_grain_ux
  ON cohort_snapshots (granularity, cohort_period, period_offset,
    COALESCE(brand_id, '00000000-0000-0000-0000-000000000000'::uuid),
    COALESCE(group_id, '00000000-0000-0000-0000-000000000000'::uuid));
CREATE INDEX cohort_snapshots_scope_idx
  ON cohort_snapshots (granularity, cohort_period);

-- migrate:down
DROP TABLE IF EXISTS cohort_snapshots;
