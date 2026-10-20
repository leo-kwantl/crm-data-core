-- 049_manifest_future_rows_guard.sql
--
-- The nightly tick ends with a detection job. Prevention already exists in both
-- writers (`guard_as_of` read at `pipelines/schedule.py` HEAD, crm-cdp-ml's
-- `d77a342` read at -- refusing a future `--as-of` before it connects), and a
-- trigger was rejected: it would be a third choke point reachable only by a
-- raw INSERT, and it
-- hard-codes "a future row can never exist", turning a clock incident into a
-- migration.
--
-- What was missing is detection, measured on the live cluster 2026-10-20: 753
-- rows across 7 tables held an `as_of` four to six days ahead
-- (`churn_snapshots` 21, `clv_snapshots` 21, `segment_runs` 69,
-- `rfm_segment_movement` 333, `rfm_segment_sizes` 132, `btyd_params` 22,
-- `pipeline_runs` 151). Every one was found by hand or by accident. The worst
-- of them fail *silently and in the future*: a `pipeline_runs` row claiming
-- `succeeded` for a day that has not happened makes the real tick skip that
-- job, and the page shows nothing instead of a wrong number.
--
-- `future_rows_guard` runs last (it depends on every other nightly job) and
-- fails naming `table: count`, so a new writer shows up as a red tick on the
-- night it lands rather than as a number nobody can explain. It reuses
-- `pipelines/retention.py`'s `AGED` map (filtered to the analytic anchors,
-- `as_of` / `scored_at`) rather than keeping a second table list that could
-- drift.
--
-- Version-gated on `version < 7` -- 045/046 own 6 -- so a later admin edit is
-- never overwritten.

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
      forecast:
        engine: forecast_snapshot
        depends_on:
        - apply
        params:
          horizon_days: 28
          lookback_weeks: 106
          min_history_weeks: 8
      future_rows:
        engine: future_rows_guard
        depends_on:
        - apply
        - rfm
        - churn
        - clv
        - segments_purchase
        - segments_overall
        - segments_behavior
        - forecast
$$, 7, '049')
ON CONFLICT (pipeline) DO UPDATE
  SET yaml = EXCLUDED.yaml, version = EXCLUDED.version,
      updated_by = EXCLUDED.updated_by, updated_at = now()
  WHERE pipeline_manifests.version < 7;

-- migrate:down
-- Back to 045's nightly (the seven jobs without the guard); the version stamp
-- returns to 6 so a re-apply of this file still fires.
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
      forecast:
        engine: forecast_snapshot
        depends_on:
        - apply
        params:
          horizon_days: 28
          lookback_weeks: 106
          min_history_weeks: 8
$$, version = 6, updated_by = '049-down', updated_at = now()
 WHERE pipeline = 'nightly'
   AND updated_by = '049';
