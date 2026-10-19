-- 041_manifest_segments_nightly.sql
--
-- Segments move to the nightly cron, and `behavior` gets a schedule for the
-- first time. Measured reason (manager + ml, cluster-verified): the three
-- segment runs sat in `weekly` (`0 2 * * 1`) and `segments_behavior` sat in
-- NO manifest, so no brand had a behavior run newer than the day's one
-- manual fire -- a page fed by a job nobody runs -- and
-- `/analytics/segments/explain` could 404 for up to seven days after an
-- explain/segment mismatch. Nightly makes the repair daily. Measured cost of
-- the new jobs, one nightly tick at `2026-10-20` on the live cluster: 4.9 s
-- purchase, 4.9 s overall, 1.5 s behavior (medians) x 6 brands + the global
-- batch = ~79 s added to the tick, most of it interpreter start.
--
-- `weekly` keeps its copy: its `assoc`, `explain` and `matrix` jobs declare
-- `depends_on: segments_overall`, so dropping the segments jobs there would
-- leave dangling dependency names. The duplicate run on the weekly night is
-- a `skipped` row, not a second write (`segment_runs_day_scope_ux`).
--
-- Why a migration at all, against 015's "later edits go through the app
-- config API, never migrations": the cluster and this seed had already
-- drifted -- live rows are version 3 with `engine: ld8_snapshot` where 015
-- seeds `rfm_snapshot`, and `weekly` at `0 2 * * 1` with eight jobs where
-- 015 seeds `0 2 * * 0` with three. A clone could therefore not reproduce
-- the schedule anyone is actually running. This re-states both manifests as
-- the authoritative text, version-gated on `version < 4` so a later admin
-- edit (which bumps the version) is never overwritten.

-- migrate:up
INSERT INTO pipeline_manifests (pipeline, yaml, version, updated_by)
VALUES ('nightly', $$version: 1
pipelines:
  nightly:
    schedule: 0 1 * * *
    scopes:
      brands:
      - '*'
      groups:
      - '*'
    jobs:
      apply:
        engine: upload_apply
        params:
          strategy: validated_only
      rfm:
        engine: ld8_snapshot
        depends_on:
        - apply
      churn:
        engine: churn_snapshot
        depends_on:
        - apply
        params:
          activity: orders
          horizon_days: 90
      clv:
        engine: clv_snapshot
        depends_on:
        - apply
        params:
          horizon_days: 90
      segments_purchase:
        engine: segments
        params:
          mode: purchase
          k: 5
      segments_overall:
        engine: segments
        depends_on:
        - segments_purchase
        params:
          mode: overall
          k: 5
      segments_behavior:
        engine: segments
        depends_on:
        - segments_purchase
        params:
          mode: behavior
          k: 5
$$, 4, '041')
ON CONFLICT (pipeline) DO UPDATE
  SET yaml = EXCLUDED.yaml, version = EXCLUDED.version,
      updated_by = EXCLUDED.updated_by, updated_at = now()
  WHERE pipeline_manifests.version < 4;

INSERT INTO pipeline_manifests (pipeline, yaml, version, updated_by)
VALUES ('weekly', $$version: 1
pipelines:
  weekly:
    schedule: 0 2 * * 1
    scopes:
      brands:
      - '*'
      groups:
      - '*'
    jobs:
      segments_purchase:
        engine: segments
        params:
          mode: purchase
          k: 5
      segments_overall:
        engine: segments
        depends_on:
        - segments_purchase
        params:
          mode: overall
          k: 5
      recs:
        engine: recs_batch
        params:
          top_n: 10
          min_support: 2
          min_confidence: 0.2
      assoc:
        engine: assoc_batch
        depends_on:
        - segments_overall
        params:
          min_support: 5
          min_confidence: 0.2
          min_rules: 50
      explain:
        engine: explain_batch
        depends_on:
        - segments_overall
        params:
          segment_run_id: latest
          segment_mode: overall
          segment_k: 5
          top_n: 20
      forecast:
        engine: forecast_snapshot
        depends_on:
        - assoc
        params:
          horizon_days: 28
          lookback_weeks: 52
          min_history_weeks: 8
      cohort:
        engine: cohort_snapshot
        params:
          granularities:
          - week
          - month
          - year
          max_offsets:
            week: 12
            month: 12
            year: 5
      matrix:
        engine: matrix_batch
        depends_on:
        - segments_overall
        params:
          segment_run_id: latest
          segment_mode: overall
          segment_k: 5
$$, 4, '041')
ON CONFLICT (pipeline) DO UPDATE
  SET yaml = EXCLUDED.yaml, version = EXCLUDED.version,
      updated_by = EXCLUDED.updated_by, updated_at = now()
  WHERE pipeline_manifests.version < 4;

-- migrate:down
-- Back to the pre-041 nightlies: the eight-job weekly (identical text, so
-- only its version stamp moves) and the segment-free nightly.
UPDATE pipeline_manifests SET yaml = $$version: 1
pipelines:
  nightly:
    schedule: 0 1 * * *
    scopes:
      brands:
      - '*'
      groups:
      - '*'
    jobs:
      apply:
        engine: upload_apply
        params:
          strategy: validated_only
      rfm:
        engine: ld8_snapshot
        depends_on:
        - apply
      churn:
        engine: churn_snapshot
        depends_on:
        - apply
        params:
          activity: orders
          horizon_days: 90
      clv:
        engine: clv_snapshot
        depends_on:
        - apply
        params:
          horizon_days: 90
$$, version = 3, updated_by = '041-down', updated_at = now()
 WHERE pipeline = 'nightly'
   AND updated_by = '041';

UPDATE pipeline_manifests SET version = 3, updated_by = '041-down',
       updated_at = now()
 WHERE pipeline = 'weekly' AND updated_by = '041';
