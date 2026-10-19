-- 037_backtest_outcomes.sql
--
-- The reportable-number store for the backtest (ml spec §5.3-1 / §9.6-F5):
-- one row per customer per model per holdout window, holding the prediction
-- and the realized outcome that scored it. ml writes it; ml's own number does
-- not depend on it, so a missing table is a reported gap, not a fatal one.
--
-- ml requested this body (crm-cdp-ml master `a53da38`, no DDL there) and
-- pins the column order below for its positional INSERT:
--   model, freq, anchor_as_of, holdout_days, window_from, window_to,
--   brand_id, group_id, customer_id, predicted, realized_orders,
--   realized_cents, created_by
-- One reshape from the request: the group FK points at `groups` (011), not a
-- `customer_groups` table -- no such table exists in this schema.
--
-- Append-only by intent: no updated_at, the unique key is the window
-- identity, and the grant is revoked at the end of the up-section (038
-- re-states that revoke for any cluster that recorded this version before
-- it was added).

-- migrate:up
CREATE TABLE IF NOT EXISTS backtest_outcomes (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    brand_id uuid REFERENCES brands(id) ON DELETE CASCADE,
    group_id uuid REFERENCES groups(id) ON DELETE CASCADE,
    model text NOT NULL,
    freq text NOT NULL,
    anchor_as_of date NOT NULL,
    holdout_days int NOT NULL,
    window_from timestamptz NOT NULL,
    window_to timestamptz NOT NULL,
    customer_id uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    predicted real NOT NULL,
    realized_orders int NOT NULL DEFAULT 0,
    realized_cents bigint NOT NULL DEFAULT 0,
    created_by text,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS backtest_outcomes_window_uidx
    ON backtest_outcomes (
        COALESCE(brand_id, '00000000-0000-0000-0000-000000000000'::uuid),
        COALESCE(group_id, '00000000-0000-0000-0000-000000000000'::uuid),
        model, anchor_as_of, holdout_days, customer_id);

CREATE INDEX IF NOT EXISTS backtest_outcomes_scope_idx
    ON backtest_outcomes (brand_id, group_id, anchor_as_of DESC);

-- Identity is not immutability: the unique key stops a duplicate window row,
-- this stops an UPDATE/DELETE on one (027/029 shape). ml only inserts
-- (`INSERT ... ON CONFLICT DO NOTHING`, ml_engine/backtest.py:117-123).
REVOKE UPDATE, DELETE ON backtest_outcomes FROM PUBLIC;

-- migrate:down
DROP INDEX IF EXISTS backtest_outcomes_scope_idx;
DROP INDEX IF EXISTS backtest_outcomes_window_uidx;
DROP TABLE IF EXISTS backtest_outcomes;
