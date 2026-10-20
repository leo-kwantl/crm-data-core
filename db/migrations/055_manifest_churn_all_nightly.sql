-- 055_manifest_churn_all_nightly.sql
--
-- The portal's churn page offers an "All activity" tab, and no
-- `activity = 'all'` snapshot has ever been written: the tab 404s and the
-- empty state says "run the pipeline first", which is false on a system
-- whose pipeline has run. ml measured what making the tab true costs (ml
-- master 0326402): the `all` leg admits the SAME cohort as the `orders` leg
-- (measured at brand scope: 0 customers in `all` but not in `orders`) and
-- re-times 2 of 498 (expected purchases 214.7 -> 216.7, +0.9%; HQ 500.7 ->
-- 503.6, +0.6%). So the promise the portal can make is "the same cohort,
-- read on more activity" - never "a different population".
--
-- One manifest row, no ml change, no DDL, no new column: `churn_snapshots`
-- already carries `activity`, and the argv comes from `_build_churn`
-- (`pipelines/schedule.py`), which passes `--activity`, `--horizon-days`
-- and `--model` from the job's params. ml confirmed the flag set: `--activity
-- {orders,all} --horizon-days 90 --model bgnbd`, NO `--rescore` (the `all`
-- leg is its own leg of the unique key (as_of, activity, horizon_days,
-- scope) and writes its own row), and horizon 90 to match the nightly
-- `orders` leg - 180 is not a promise for `all` (the only 180 pair that
-- exists is an HQ one on 2026-10-15).
--
-- `created_by` stays the tick's stamp (`scheduler` for the nightly tick, as it
-- is for every other nightly leg); the leg is identified by `activity`,
-- which is the column the reader filters on.
--
-- The `future_rows` guard now depends on `churn_all` too: the guard checks
-- the snapshot tables for rows dated after today, and the `all` leg writes
-- into `churn_snapshots`.
--
-- Every other pinned value is carried over verbatim from nightly v8 (051):
-- the segment modes and `lookback_weeks: 106` (044/046) are re-stated, not
-- re-decided - a re-stating migration owns every number in it.

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
      churn_all:
        engine: churn_snapshot
        depends_on:
        - apply
        params:
          activity: all
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
        - churn_all
        - clv
        - segments_purchase
        - segments_overall
        - segments_behavior
        - forecast
$yaml$,
         version = version + 1,
         updated_by = 'migration 055',
         updated_at = now()
   WHERE pipeline = 'nightly'
     -- Content, not a version number: a replayed cluster reaches this row
     -- with a version the live cluster never had (041/044/046/049/051 all
     -- bump it), so a numeric gate silently skips on one path and applies on
     -- the other. The job's absence is the condition that matters, and
     -- re-running with it present is a no-op.
     AND position('churn_all' in yaml) = 0;
END
$man$;

-- migrate:down
-- No rollback: removing the job entry would only put the table back to a
-- writer nobody calls. The rows it has written are real history and stay.
