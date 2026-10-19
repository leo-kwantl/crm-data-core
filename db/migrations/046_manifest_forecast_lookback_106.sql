-- 046_manifest_forecast_lookback_106.sql
--
-- DATA-ONLY: this file moves a manifest value and declares no schema object,
-- so `services/api/tests/test_migration_drift.py`'s ledger-gap check cannot
-- see it -- for this migration the `schema_migrations` row is the only
-- evidence it ran. Stated here so the guard's limit is documented at the one
-- file it applies to, not only in the test.
--
-- The forecast lookback widens 104 -> 106 weeks. 044's 104 was one week short
-- of its own goal: the window always loses its own last week (the one `as_of`
-- falls in, usually observed for a day), so 104 hands the engine 103 fitted
-- weeks = 1.98 cycles against `season_period 52`, and
-- `SEASON_MIN_CYCLES = 2` still refuses the candidate. ml named that constant
-- `LOST_WINDOW_WEEKS = 1` (`ml_engine/forecast.py`) and moved its own default
-- to 106 (`DEFAULT_LOOKBACK_WEEKS`, `8c02eb2`/`df6af78`).
--
-- Why a new file and not an edit to 044: 044 is applied and pushed, and an
-- applied migration's text is immutable -- rewriting it after it ran leaves a
-- live database whose recorded text never produced its schema.
--
-- This CHANGES PUBLISHED NUMBERS again: re-running the tick republishes
-- forecast batches whose `seasonality` flips to measured for the first time
-- (at 104 the annual candidate was never reachable). `method`, the scope grain
-- and every reader's shape are untouched.
--
-- The bar is tied by a test, not by memory:
-- `services/api/tests/test_forecast_manifest.py` reads this pinned value and
-- ml's `SEASON_MIN_CYCLES` + `LOST_WINDOW_WEEKS` and asserts
-- `lookback_weeks >= SEASON_MIN_CYCLES * season_period + LOST_WINDOW_WEEKS`.

-- migrate:up
UPDATE pipeline_manifests
SET yaml = replace(yaml, 'lookback_weeks: 104', 'lookback_weeks: 106'),
    version = 6,
    updated_by = '046_manifest_forecast_lookback_106'
WHERE version < 6
  AND yaml LIKE '%lookback_weeks: 104%';

-- migrate:down
UPDATE pipeline_manifests
SET yaml = replace(yaml, 'lookback_weeks: 106', 'lookback_weeks: 104'),
    version = 5,
    updated_by = '046_manifest_forecast_lookback_106 (down)'
WHERE version < 7
  AND yaml LIKE '%lookback_weeks: 106%';
