-- 051_manifest_kpi_daily_nightly.sql
--
-- `kpi_daily` was scheduled by nothing and read by nothing: `snapshot.py` has
-- the writer, `schedule.py` has the engine (`_run_data_engine` dispatches it at
-- `pipelines/schedule.py:788` over `KPI_DAILY_UPSERT_SQL`, scoped and stamped
-- like every other data engine), but no manifest named it, so the table held a
-- single 2026-09-30 row. It needs no new module -- one job entry makes it
-- nightly, and history starts accumulating from the first tick rather than
-- from the day someone builds a page.
--
-- The job runs right after `apply` (KPI derives from the same orders day the
-- snapshots do), before `rfm`/`churn`/`clv` so a KPI row exists even when a
-- model job later fails.
--
-- Every other pinned value is carried over verbatim from nightly v7: the
-- segment modes and `lookback_weeks: 106` (044/046) are re-stated, not
-- re-decided — a re-stating migration owns every number in it.

-- migrate:up

DO $man$
DECLARE
  v_yaml text;
BEGIN
  SELECT yaml INTO v_yaml FROM pipeline_manifests WHERE pipeline = 'nightly';
  IF v_yaml IS NULL THEN
    RAISE EXCEPTION 'nightly manifest row is missing';
  END IF;

  UPDATE pipeline_manifests
     SET yaml = $yaml$version: 1
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
      kpi:
        engine: kpi_daily
        depends_on:
        - apply
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
$yaml$,
         version = version + 1,
         updated_by = 'migration 051',
         updated_at = now()
   WHERE pipeline = 'nightly'
     -- Content, not a version number: a replayed cluster reaches this row with
     -- a version the live cluster never had (041/044/046/049 all bump it), so a
     -- numeric gate silently skips on one path and applies on the other. The
     -- job's absence is the condition that matters, and re-running with it
     -- present is a no-op.
     AND position('kpi_daily' in yaml) = 0;
END
$man$;

-- migrate:down
-- No rollback: removing the job entry would only put the table back to a
-- writer nobody calls. The row it has written is real history and stays.
