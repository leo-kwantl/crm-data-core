-- 057_manifest_scopes_brands_hq.sql
--
-- The manifest's `scopes:` block promised a group leg the tick never
-- dispatched. Manager ruling (A114, work order 22.5, 2026-10-22): the group
-- stops being a scope, and the block is restated to say what the tick really
-- dispatches -- every brand, then the `(NULL, NULL)` hq roll-up.
--
-- Why the promise was false: `tick_scopes` (`pipelines/schedule.py:97`)
-- returns one `(brand_id, NULL)` scope per brand and then the hq roll-up, and
-- the runner fires each due pipeline once per scope it is given
-- (`scripts/run_pipeline.py:253-255`). No group scope is ever offered to any
-- engine, so the block named a scope no writer has ever been asked for. ml's
-- census found no group-alone row in any scope-shaped table, so no reader
-- has ever served one either.
--
-- Why "no group" is the honest text and not a hole: the only writable group
-- scope is the PAIR `(brand_id, group_id)` -- every writer's predicate is
-- brand-scoped (`b.group_id` AND `c.group_id`) -- while the scope shape the
-- session promised, `(NULL, group_id)`, is a scope no writer can produce. A
-- group cut therefore needs the pair AND a session scope able to promise it,
-- and it comes back as a priced, measured change (the tick's wall time
-- before/after, the row count). That is the next decision, not this one.
--
-- What this migration does NOT do: no group leg, no per-group pricing, no
-- row-visibility change, no reader predicate change (the scope-exact form
-- `group_id IS NOT DISTINCT FROM $2` stays), and no substitution of the
-- global row for a group row. `tick_scopes` is untouched, and so is every
-- writer.
--
-- Why a new file and not an edit to `041`: `041` is applied and pushed, and an
-- applied migration's text is immutable -- rewriting it after it ran leaves a
-- live database whose recorded text never produced its schema.
--
-- Targeted text update, not a re-statement of the whole manifest (as `055`
-- does): the block was the only thing that was wrong, so every other pinned
-- value stays exactly as it is, and an admin's later edit to a job's params is
-- not overwritten by this file. The rewrite is one atomic `regexp_replace` per
-- row, and it is line-ending agnostic (the stored yaml's own ending is
-- captured and reused), so a checkout's CRLF does not silently skip the
-- rewrite.
--
-- The rewrite is gated on the old block's presence, so a re-run is a no-op,
-- and a row whose `scopes:` block was hand-edited into another shape is NOT
-- silently restated: the migration names it in a NOTICE and leaves the
-- hand-edited row alone, because a hand-edited manifest is a decision a
-- human has to make.
--
-- DATA-ONLY, like `046`: the ledger-gap check cannot see this file, so the
-- `schema_migrations` row is the only evidence it ran.
--
-- app reads this block as raw text (`apps/web/lib/manifestForm.ts:45-60`
-- `extractScopes` passes the `    scopes:` lines through verbatim) and parses
-- no key inside it, so the new block shows as written and no app change is
-- needed.

-- migrate:up
DO $man$
DECLARE
  -- The old block, as a pattern: the four-space `scopes:` key, the brand
  -- list, the group list. The endings are captured, not assumed, so the
  -- rewrite keeps whatever line ending the stored yaml already has.
  v_pat constant text :=
    $re$(    scopes:\r?\n      brands:\r?\n      - '\*')(\r?\n)      groups:\r?\n      - '\*'(\r?\n)$re$;
  v_rep constant text := $re$\1\2      hq:\2      - '*'\3$re$;
  v_restated int := 0;
  v_left text;
BEGIN
  UPDATE pipeline_manifests
     SET yaml = regexp_replace(yaml, v_pat, v_rep, 'g'),
         version = version + 1,
         updated_by = 'migration 057',
         updated_at = now()
   WHERE yaml ~ v_pat;
  GET DIAGNOSTICS v_restated = ROW_COUNT;

  SELECT string_agg(pipeline, ', ') INTO v_left
    FROM pipeline_manifests
   WHERE position('groups:' in yaml) > 0;
  IF v_left IS NOT NULL THEN
    RAISE NOTICE
      '057: % still declares a group scope -- this migration restates only'
      ' the block text 041/055 wrote, so a hand-edited manifest is left for a'
      ' human to decide', v_left;
  END IF;

  RAISE NOTICE '057: scopes block restated to brands + hq in % manifest row(s)',
    v_restated;
END
$man$;

-- migrate:down
-- Restores the previous TEXT, not the previous reality: the old block
-- promised a group leg the tick never dispatched, and putting it back would
-- state a promise again. The block is rewritten in the same atomic,
-- line-ending-agnostic way, so a re-run is a no-op.
DO $man$
DECLARE
  v_pat constant text :=
    $re$(    scopes:\r?\n      brands:\r?\n      - '\*')(\r?\n)      hq:\r?\n      - '\*'(\r?\n)$re$;
  v_rep constant text := $re$\1\2      groups:\2      - '*'\3$re$;
BEGIN
  UPDATE pipeline_manifests
     SET yaml = regexp_replace(yaml, v_pat, v_rep, 'g'),
         version = version + 1,
         updated_by = 'migration 057 (down)',
         updated_at = now()
   WHERE yaml ~ v_pat;
END
$man$;
