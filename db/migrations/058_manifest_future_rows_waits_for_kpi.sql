-- 058_manifest_future_rows_waits_for_kpi.sql
--
-- 049's `future_rows` guard exists to fail naming `table: count` when a
-- writer left a row dated ahead of business today: "a new writer shows up as
-- a red tick on the night it lands rather than as a number nobody can
-- explain". 049's own header states the design -- "`future_rows_guard` runs
-- last (it depends on every other nightly job)".
--
-- 051 then added the `kpi` writer to the nightly manifest (the `kpi_daily`
-- engine, which writes `kpi_daily_snapshots`) and did not add it to the
-- guard's `depends_on` list. 055 remembered to add `churn_all` to the list
-- when it added that leg; 051 did not.
--
-- The gap is a hole in the DETECTION, not a cosmetic one: the guard runs
-- after the jobs it waits for, so a writer it does not wait for can leave a
-- future-dated row in `kpi_daily_snapshots` AFTER the guard has already
-- passed -- and the guard stays green. That is the one failure 049 was
-- written to end.
--
-- Found by app's contract test (`services/api/tests/test_pipelines.py::
-- test_get_shape_and_edges`), which derives the contract instead of
-- hardcoding it: `set(future_rows.depends_on) == set(jobs) - {"future_rows"}`.
--
-- What this migration does: one targeted text update -- `kpi` is added to the
-- guard's `depends_on` list, in the manifest's own job order (apply, kpi,
-- rfm, ...), because the guard waits for every other nightly job.
--
-- No new leg and no extra cost: the `kpi` job already runs nightly (051), so
-- the list only decides WHEN the guard runs, not what the tick does.
--
-- Targeted text update, not a re-statement of the whole manifest (as `055`
-- does): the guard's list was the only thing that was wrong, so every other
-- pinned value stays exactly as it is -- including 057's `scopes:` block
-- (brands + hq, no group) and an admin's later edit to a job's params. The
-- rewrite is one atomic `regexp_replace`, and it is line-ending agnostic
-- (the stored yaml's own ending is captured and reused), so a checkout's
-- CRLF does not silently skip the rewrite.
--
-- Gated on the guard's list still being the shape 055 left (so a re-run is a
-- no-op), and a guard block that is NOT that shape is named in a NOTICE and
-- left alone: a hand-edited manifest is a decision a human has to make.
--
-- DATA-ONLY, like `046` and `057`: the ledger-gap check cannot see this
-- file, so the `schema_migrations` row is the only evidence it ran.
--
-- app reads the manifest's shape verbatim (its `/jobs` page renders the
-- guard's list), so the page shows the corrected list with no app change;
-- app vendors this file byte-for-byte.

-- migrate:up
DO $man$
DECLARE
  -- The guard's block, as a pattern: the job name, its engine, its
  -- `depends_on` key and its FIRST entry (`apply`), then the second entry
  -- (`rfm`). The ending is captured, not assumed, so the rewrite keeps
  -- whatever line ending the stored yaml already has.
  v_pat constant text :=
    $re$(      future_rows:\r?\n        engine: future_rows_guard\r?\n        depends_on:\r?\n        - apply)(\r?\n)(        - rfm)$re$;
  v_rep constant text := $re$\1\2        - kpi\2\3$re$;
  v_restated int := 0;
  v_left text;
BEGIN
  UPDATE pipeline_manifests
     SET yaml = regexp_replace(yaml, v_pat, v_rep, 'g'),
         version = version + 1,
         updated_by = 'migration 058',
         updated_at = now()
   WHERE yaml ~ v_pat;
  GET DIAGNOSTICS v_restated = ROW_COUNT;

  IF v_restated = 0 THEN
    SELECT string_agg(pipeline, ', ') INTO v_left
      FROM pipeline_manifests
     WHERE position('future_rows:' in yaml) > 0
       AND position('future_rows_guard' in yaml) > 0;
    IF v_left IS NOT NULL THEN
      RAISE NOTICE
        '058: % either already waits for kpi, or its guard block is not the'
        ' shape 055 left -- the list was left for a human to decide', v_left;
    END IF;
  END IF;

  RAISE NOTICE '058: the future_rows guard now waits for kpi in % manifest row(s)',
    v_restated;
END
$man$;

-- migrate:down
-- Restores the previous TEXT, not the previous behaviour: the guard stops
-- waiting for the `kpi` writer, so a future-dated `kpi_daily_snapshots` row
-- can land after the guard has passed. Written for completeness -- a
-- downgrade is a decision, not a repair.
DO $man$
DECLARE
  v_pat constant text :=
    $re$(      future_rows:\r?\n        engine: future_rows_guard\r?\n        depends_on:\r?\n        - apply\r?\n        - kpi)(\r?\n)(        - rfm)$re$;
  v_rep constant text := $re$\1\2\3$re$;
BEGIN
  UPDATE pipeline_manifests
     SET yaml = regexp_replace(yaml, v_pat, v_rep, 'g'),
         version = version + 1,
         updated_by = 'migration 058 (down)',
         updated_at = now()
   WHERE yaml ~ v_pat;
END
$man$;
