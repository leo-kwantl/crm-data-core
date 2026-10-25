-- migrate:up
-- 059: manual RFM snapshots carry an honest window_key (data, [BREAKING] analytics).
--
-- A POSTed (v1) snapshot omitted window_key, so the DDL default stamped
-- 'current' on it -- the same key the nightly v2 rolling_365 rows carry, and
-- the same key two POSTed custom ranges share with each other. The movement
-- guard pairs same mode + rules_version + window_key only, so cross-range
-- pairs answered 200 with the window widening reported as movement
-- (mom -> lifetime read 252 both / 688 entered / 0 exited).
--
-- From here a v1 row never keeps 'current': a BEFORE INSERT trigger rewrites
-- it to manual:<window_from>:<window_to> derived from the row's own bounds,
-- so two freezes of the same range share a key and pair, while different
-- ranges (or a manual freeze beside the scheduled series) refuse at the
-- existing guard with snapshot_mode_mismatch. INSERT-only, so rows stamped
-- before 059 keep 'current' and their old pairing; v2 LD8 rows are gated
-- out by the rules_version check and keep the four named keys.
-- No writer changes: ml's v1 INSERT still omits the column and the app's
-- guard already compares window_key. REVOKEs and grains are untouched.

ALTER TABLE rfm_snapshots DROP CONSTRAINT rfm_snapshots_window_key_check;
ALTER TABLE rfm_snapshots ADD CONSTRAINT rfm_snapshots_window_key_check
  CHECK (window_key IN ('current', 'mom', 'yoy', 'lifetime')
         OR window_key LIKE 'manual:%');

CREATE OR REPLACE FUNCTION rfm_snapshots_stamp_manual_key()
RETURNS trigger LANGUAGE plpgsql AS $func$
BEGIN
  IF NEW.rules_version = 'v1' AND NEW.window_key = 'current' THEN
    NEW.window_key :=
      'manual:' || NEW.window_from::text || ':' || NEW.window_to::text;
  END IF;
  RETURN NEW;
END;
$func$;

CREATE TRIGGER rfm_snapshots_manual_key_bi
BEFORE INSERT ON rfm_snapshots
FOR EACH ROW
WHEN (NEW.rules_version = 'v1' AND NEW.window_key = 'current')
EXECUTE FUNCTION rfm_snapshots_stamp_manual_key();

-- migrate:down
-- Best-effort: fails while manual:* rows exist (the restored CHECK rejects
-- them); delete those rows first, newest-appended first.
DROP TRIGGER IF EXISTS rfm_snapshots_manual_key_bi ON rfm_snapshots;
DROP FUNCTION IF EXISTS rfm_snapshots_stamp_manual_key();
ALTER TABLE rfm_snapshots DROP CONSTRAINT rfm_snapshots_window_key_check;
ALTER TABLE rfm_snapshots ADD CONSTRAINT rfm_snapshots_window_key_check
  CHECK (window_key IN ('current', 'mom', 'yoy', 'lifetime'));
