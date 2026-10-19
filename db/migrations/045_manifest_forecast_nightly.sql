-- 045_manifest_forecast_nightly.sql
--
-- Per-brand forecast joins the nightly cron. Measured reason: `forecast` sat
-- only in `weekly`, so the demo brand's newest batch aged for up to seven days
-- while `churn`/`clv`/`rfm` refreshed nightly. Re-measured at `2026-10-20`
-- after ml's five-brand writers landed: six brands, one `forecast_snapshot`
-- run each, ~1.8 s per run (~11 s total) and the brand series are *less*
-- heterogeneous than the pool -- DEMO 13.4818/15.2182 (skill 0.1141 at 11
-- paired weeks) and KVLN 11.2436/13.2182 (0.1494) both beat the global pooled
-- 16.54/32.4 on absolute error. So the per-brand pass earns its eleven seconds.
--
-- The `--brand-id`-only rule: a brand-scoped run must pass `--brand-id` and
-- never `--group-id`. A `(brand, group)` stamp is invisible to the demo
-- session, which resolves `brand_id = X AND group_id IS NULL`; a run that
-- stamps both is a row no reader can see.
--
-- One process looping all six brands is not available on this side: ml's CLI
-- takes a single `--brand-id` (`ml_jobs/run_forecast_snapshot.py:60`), so the
-- tick fans out one subprocess per brand and ~98 % of each ~1.8 s is
-- interpreter start. Accepted at nightly cadence (11 s); collapsing to a loop
-- needs ml to accept a brand set, and is their call.
--
-- The lookback is pinned to the value `046` settled on (106), not to 041's
-- 52: re-stating 041's text here would have silently reverted the window
-- widening that makes the seasonal candidate reachable, and the guard in
-- `services/api/tests/test_manifests.py` caught exactly that (nightly 52 vs
-- weekly 106) on the first run. A manifest that re-states a whole document
-- owns every number in it.
--
-- Version-gated on `version < 6` -- `044` owns version 5 -- so a later admin
-- edit is never overwritten.

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
$$, 6, '045')
ON CONFLICT (pipeline) DO UPDATE
  SET yaml = EXCLUDED.yaml, version = EXCLUDED.version,
      updated_by = EXCLUDED.updated_by, updated_at = now()
  WHERE pipeline_manifests.version < 6;

-- migrate:down
-- Back to 041's nightly (the six segment-bearing jobs, no forecast), and the
-- version stamp returns to 4 so a re-apply of this file still fires.
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
$$, version = 4, updated_by = '045-down', updated_at = now()
 WHERE pipeline = 'nightly'
   AND updated_by = '045';
