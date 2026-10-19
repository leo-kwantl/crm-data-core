-- 044_manifest_forecast_lookback.sql
--
-- The forecast lookback widens from 52 to 104 weeks. Measured reason (manager
-- ruling, ml `ee271ca`): `ml_engine/forecast.py` fits the seasonal candidate
-- only when the window holds `SEASON_MIN_CYCLES = 2` observations per season
-- position, i.e. `lookback_weeks >= 2 * season_period`. The manifests pinned
-- 52 weeks with the annual `season_period` of 52, so `n < 2 * 52` held for
-- every series in every production tick: the seasonal branch was unreachable
-- and each batch reported `seasonality` unmeasured while looking healthy.
-- 104 = exactly `2 * season_period` -- enough to identify the cycle without
-- spending history the cluster does not have.
--
-- This CHANGES PUBLISHED NUMBERS: a re-run tick republishes forecast batches
-- whose `seasonality` flips from unmeasured to measured. `method` and the
-- scope grain are untouched, so no reader has to change shape.
--
-- Additive-only: `041` is published, so this is a new file. Targeted text
-- update, not a re-statement of the whole manifest, and version-gated on
-- `version < 5` so a later admin edit (which bumps the version) stands.
-- The pair (window, cycles) is tied by a test, not by memory:
-- `services/api/tests/test_forecast_manifest.py` reads this pinned value and
-- ml's `SEASON_MIN_CYCLES` and asserts the inequality holds, so either side
-- moving alone turns red.

-- migrate:up
UPDATE pipeline_manifests
SET yaml = replace(yaml, 'lookback_weeks: 52', 'lookback_weeks: 104'),
    version = 5,
    updated_by = '044_manifest_forecast_lookback'
WHERE version < 5
  AND yaml LIKE '%lookback_weeks: 52%';

-- migrate:down
UPDATE pipeline_manifests
SET yaml = replace(yaml, 'lookback_weeks: 104', 'lookback_weeks: 52'),
    version = 4,
    updated_by = '044_manifest_forecast_lookback (down)'
WHERE version < 6
  AND yaml LIKE '%lookback_weeks: 104%';
