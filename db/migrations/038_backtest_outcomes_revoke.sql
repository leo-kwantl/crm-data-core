-- 038_backtest_outcomes_revoke.sql
--
-- 037 shipped without the grant revocation its own "append-only by intent"
-- header implies: `REVOKE UPDATE, DELETE ... FROM PUBLIC`, the shape 027
-- (landing_receipts) and 029 (cluster_feature_matrix) already use. Identity
-- is not immutability -- the unique key stops a duplicate window row, only
-- the revoke stops an edit of one.
--
-- The line is now in 037 too, so a fresh database is correct at 037. It is
-- re-stated here because this cluster recorded version 037 while that text
-- was still the old one (dbmate skips a version already in
-- schema_migrations) -- the same reason 035 re-states 014's columns. REVOKE
-- is idempotent, so applying both is harmless.
--
-- ml only inserts into this table (`INSERT ... ON CONFLICT DO NOTHING`,
-- ml_engine/backtest.py:117-123), so nothing loses a privilege it uses.

-- migrate:up
REVOKE UPDATE, DELETE ON backtest_outcomes FROM PUBLIC;

-- migrate:down
GRANT UPDATE, DELETE ON backtest_outcomes TO PUBLIC;
