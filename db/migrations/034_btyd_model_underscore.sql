-- migrate:up
-- 034: BTYD model strings hyphen -> underscore (data, [ADDITIVE]
-- analytics, joint w/ ml; supersedes the 028 CHECK, 028 itself is
-- history and stays untouched).
--
-- Fallout from the LM3/LM4 repoint: 028 declared
-- model IN ('bg-nbd','pareto-nbd') but ml's writer + both readers +
-- regress guard ship 'bg_nbd' / 'pareto_nbd' — every real fit-write
-- bounced on btyd_params_model_check while data's proof stayed green
-- on the hyphen it seeded itself. ml is consistent in three places,
-- data in one: data moves. Canonical strings from here on are
-- underscore (Python-identifier form, matches the engine names).
-- No live hyphen rows can exist (all hyphen writes... there were
-- none outside proofs; every production-shaped write bounced), so
-- the CHECK swap needs no backfill. 033 is app-local — skipped.

ALTER TABLE btyd_params DROP CONSTRAINT btyd_params_model_check;
ALTER TABLE btyd_params ALTER COLUMN model
  SET DEFAULT 'bg_nbd';
ALTER TABLE btyd_params ADD CONSTRAINT btyd_params_model_check
  CHECK (model IN ('bg_nbd', 'pareto_nbd'));

-- migrate:down
ALTER TABLE btyd_params DROP CONSTRAINT btyd_params_model_check;
ALTER TABLE btyd_params ALTER COLUMN model
  SET DEFAULT 'bg-nbd';
ALTER TABLE btyd_params ADD CONSTRAINT btyd_params_model_check
  CHECK (model IN ('bg-nbd', 'pareto-nbd'));
